from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
import requests

import ibeta_bot as bot


@pytest.fixture(autouse=True)
def isolate_state_files(tmp_path, monkeypatch):
    """Redirect every state file to a throwaway temp dir so tests never touch
    (or get confused by) the real production state."""
    monkeypatch.setattr(bot, "STATE_FILE", str(tmp_path / "state.txt"))
    monkeypatch.setattr(bot, "HEARTBEAT_FILE", str(tmp_path / "heartbeat.txt"))
    monkeypatch.setattr(bot, "PARSE_ERROR_FLAG_FILE", str(tmp_path / "parse_error.txt"))


def rss_date(day):
    return day.strftime("%a, %d %b %Y 13:00:00 PST")


def entry(release, days_ago=0):
    """A raw (title, pubDate) feed entry for a (system, version, label, build, _) tuple."""
    system, version, label, build, _ = release
    title = f"{system} {version} {label} ({build})".replace("  ", " ")
    return title, rss_date(datetime.now(timezone.utc).date() - timedelta(days=days_ago))


def entries_for(releases, days_ago=0):
    return [entry(r, days_ago) for r in releases]


def feed_xml(entries):
    items = "".join(f"<item><title>{t}</title><pubDate>{d}</pubDate></item>" for t, d in entries)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Releases</title>{items}</channel></rss>'.encode()


def response(status=200, content=b""):
    return Mock(status_code=status, content=content)


A = ("iOS", "27.0", "beta 5", "24A5408d", "")
B = ("iPadOS", "27.0", "beta 5", "24A5408d", "")
C = ("macOS", "26.6.2", "RC", "25G82", "")
D_NEW = ("tvOS", "27.0", "beta 6", "24J5350a", "")


def sending(sent_list):
    """A send_telegram stand-in that records the message and always succeeds."""
    def _send(message):
        sent_list.append(message)
        return True
    return _send


def run_with(sent, entries=None, effect=None):
    """Runs the bot once. The feed is either `entries` (returned every time) or
    `effect` (a load_feed_entries side effect: an exception, or a list of
    per-call results/exceptions). Everything that would be sent lands in `sent`."""
    source = {"side_effect": effect} if effect is not None else {"return_value": entries}
    with patch.object(bot.time, "sleep"), \
         patch.object(bot, "load_feed_entries", **source), \
         patch.object(bot, "send_telegram", side_effect=sending(sent)), \
         patch.object(bot, "send_heartbeat_if_due"):
        bot.run()


class TestStateFiles:
    def test_state_roundtrip(self):
        assert bot.get_last_state() == ""
        bot.save_state("hello")
        assert bot.get_last_state() == "hello"

    def test_heartbeat_date_roundtrip(self):
        assert bot.get_last_heartbeat_date() == ""
        bot.save_heartbeat_date("2026-01-01")
        assert bot.get_last_heartbeat_date() == "2026-01-01"

    def test_parse_error_flag_roundtrip(self):
        assert bot.get_parse_error_flag() is False
        bot.set_parse_error_flag(True)
        assert bot.get_parse_error_flag() is True
        bot.set_parse_error_flag(False)
        assert bot.get_parse_error_flag() is False

    def test_release_keys_roundtrip_and_sorted(self):
        bot.save_release_keys({bot.release_key(*B), bot.release_key(*A)})
        assert bot.get_last_release_keys() == {bot.release_key(*A), bot.release_key(*B)}
        assert bot.get_last_state().split("\n") == sorted(bot.get_last_state().split("\n"))


class TestLoadFeedEntries:
    def test_returns_title_and_date_for_every_item(self):
        xml = feed_xml([
            ("iOS 27.3 beta 2 (24C5049d)", "Mon, 16 Nov 2026 13:00:00 PST"),
            ("Xcode 27 (27A266a)", "Mon, 14 Sep 2026 10:00:00 PST"),
        ])
        with patch.object(bot.session, "get", return_value=response(200, xml)):
            assert bot.load_feed_entries() == [
                ("iOS 27.3 beta 2 (24C5049d)", "Mon, 16 Nov 2026 13:00:00 PST"),
                ("Xcode 27 (27A266a)", "Mon, 14 Sep 2026 10:00:00 PST"),
            ]

    def test_http_error_status_is_a_source_error_with_the_status(self):
        """The GitHub runner got a Cloudflare 403 page from the previous source,
        and it was parsed as if nothing were wrong."""
        page = b"<html><title>Just a moment...</title></html>"
        with patch.object(bot.session, "get", return_value=response(403, page)):
            with pytest.raises(bot.SourceError, match="HTTP 403"):
                bot.load_feed_entries()

    def test_html_instead_of_xml_is_a_source_error(self):
        with patch.object(bot.session, "get", return_value=response(200, b"<html><body><p>oops</body>")):
            with pytest.raises(bot.SourceError, match="not valid XML"):
                bot.load_feed_entries()

    def test_feed_without_items_is_a_source_error(self):
        with patch.object(bot.session, "get", return_value=response(200, feed_xml([]))):
            with pytest.raises(bot.SourceError, match="no items"):
                bot.load_feed_entries()


class TestParseReleases:
    TODAY = date(2026, 11, 28)

    def parse(self, *entries):
        return bot.parse_releases(list(entries), today=self.TODAY)

    def test_keeps_beta_and_rc_builds_of_apple_operating_systems(self):
        result = self.parse(
            ("iOS 27.3 beta 2 v2 (24C5049f)", "Thu, 19 Nov 2026 13:00:00 PST"),
            ("macOS 26.7 RC 3 (25G227)", "Mon, 16 Nov 2026 13:00:00 PST"),
        )
        assert result == [
            ("iOS", "27.3", "beta 2 v2", "24C5049f", "Nov 19, 2026"),
            ("macOS", "26.7", "RC 3", "25G227", "Nov 16, 2026"),
        ]

    @pytest.mark.parametrize("title", [
        "iOS 27.2.1 (24B156)",                 # final release
        "Xcode 27.3 beta 2 (27C5035i)",        # not an operating system
        "App Store Connect API 4.6.1",         # no build at all
        "TestFlight Update",
        "AirPods beta (9A292e)",               # a beta, but no version and not an OS
        "HomeView Software 27.2.1 (24V553)",
        "audioOS 27.3 beta 2 (24K5298d)",      # tracked from ipsw.dev instead - never twice
    ])
    def test_ignores_everything_that_is_not_an_os_prerelease(self, title):
        assert self.parse((title, "Thu, 19 Nov 2026 13:00:00 PST")) == []

    def test_drops_entries_older_than_max_age(self):
        too_old = self.TODAY - timedelta(days=bot.MAX_AGE_DAYS + 1)
        at_the_limit = self.TODAY - timedelta(days=bot.MAX_AGE_DAYS)
        result = self.parse(
            ("tvOS 27.2 beta 3 (24K5103f)", rss_date(too_old)),
            ("iOS 27.3 beta 1 (24C1)", rss_date(at_the_limit)),
        )
        assert [r[3] for r in result] == ["24C1"]

    def test_unreadable_date_keeps_the_entry_with_the_raw_text(self):
        assert self.parse(("iOS 27.3 beta 2 (24C5049d)", "sometime soon")) == [
            ("iOS", "27.3", "beta 2", "24C5049d", "sometime soon")
        ]


class TestReleaseKey:
    def test_key_format(self):
        assert bot.release_key(*A) == "iOS 27.0 beta 5 (24A5408d)"

    def test_key_without_suffix(self):
        assert bot.release_key("iOS", "18.0", "", "22A100", "Jan 1, 2026") == "iOS 18.0 (22A100)"

    def test_date_is_not_part_of_the_identity(self):
        assert bot.release_key("iOS", "27.0", "beta 5", "24A5408d", "August 10, 2026") == \
            bot.release_key("iOS", "27.0", "beta 5", "24A5408d", "Nov 19, 2026")

    def test_legacy_state_lines_with_dates_still_match(self):
        bot.save_state("iOS 27.0 beta 5 (24A5408d) | August 10, 2026\niPadOS 27.0 beta 5 (24A5408d) | August 10, 2026")
        assert bot.get_last_release_keys() == {bot.release_key(*A), bot.release_key(*B)}


class TestBuildReleaseMessage:
    def test_groups_by_version_and_sorts_systems(self):
        message = bot.build_release_message([B, A])  # iPadOS before iOS in the input
        assert message.index("<b>iOS</b>") < message.index("<b>iPadOS</b>")

    def test_escapes_html_special_characters(self):
        malicious = ("iOS", "27.0", "<script>", "24A<b>", "Aug 1, 2026")
        message = bot.build_release_message([malicious])
        assert "<script>" not in message
        assert "&lt;script&gt;" in message

    def test_summary_is_singular_for_one_release(self):
        message = bot.build_release_message([A])
        assert "1 new build<" in message and "1 new builds" not in message

    def test_summary_counts_builds_and_versions(self):
        assert "3 new builds · 2 versions" in bot.build_release_message([A, B, C])

    def test_links_to_apples_release_page(self):
        assert bot.DETAILS_URL in bot.build_release_message([A])


class TestRunNewReleaseDetection:
    def test_first_run_announces_everything_as_new(self):
        sent = []
        run_with(sent, entries_for([A, B, C]))

        assert len(sent) == 1
        assert bot.get_last_release_keys() == {bot.release_key(*A), bot.release_key(*B), bot.release_key(*C)}

    def test_unchanged_feed_sends_nothing(self):
        bot.save_release_keys({bot.release_key(*A), bot.release_key(*B), bot.release_key(*C)})
        sent = []
        run_with(sent, entries_for([A, B, C]))
        assert sent == []

    def test_feed_reordering_alone_sends_nothing(self):
        bot.save_release_keys({bot.release_key(*A), bot.release_key(*B), bot.release_key(*C)})
        sent = []
        run_with(sent, entries_for([C, A, B]))
        assert sent == []

    def test_new_release_not_first_in_feed_is_still_detected_and_reported_alone(self):
        bot.save_release_keys({bot.release_key(*A), bot.release_key(*B), bot.release_key(*C)})
        sent = []
        run_with(sent, entries_for([A, D_NEW, B, C]))

        assert len(sent) == 1
        assert "tvOS" in sent[0] and "beta 6" in sent[0]
        assert "beta 5" not in sent[0] and "RC" not in sent[0]

    def test_state_keeps_releases_that_dropped_out_of_the_feed(self):
        """Apple removes superseded builds from the feed; one that blips out
        and back must not be announced twice."""
        bot.save_release_keys({bot.release_key(*A), bot.release_key(*B)})
        sent = []
        run_with(sent, entries_for([D_NEW]))  # A and B are gone from the feed now
        assert bot.get_last_release_keys() == {bot.release_key(*A), bot.release_key(*B), bot.release_key(*D_NEW)}

        sent.clear()
        run_with(sent, entries_for([A, D_NEW]))  # A is back
        assert sent == []

    def test_stale_entries_are_not_announced(self):
        sent = []
        run_with(sent, entries_for([A], days_ago=bot.MAX_AGE_DAYS + 5))
        assert sent == []

    def test_feed_with_no_beta_or_rc_is_not_an_error(self):
        """Between beta cycles the feed can legitimately hold only final releases."""
        sent = []
        today = datetime.now(timezone.utc).date()
        run_with(sent, [("iOS 27.2.1 (24B156)", rss_date(today))])
        assert sent == []
        assert bot.get_parse_error_flag() is False

    def test_failed_send_does_not_persist_state(self):
        with patch.object(bot, "load_feed_entries", return_value=entries_for([A])), \
             patch.object(bot, "send_telegram", return_value=False), \
             patch.object(bot, "send_heartbeat_if_due"):
            bot.run()
        assert bot.get_last_release_keys() == set()


class TestFeedFailureHandling:
    def test_transient_bad_response_recovers_via_retry(self):
        sent = []
        run_with(sent, effect=[bot.SourceError("HTTP 503"), entries_for([A])])
        assert len(sent) == 1 and "1 new build" in sent[0]
        assert bot.get_parse_error_flag() is False

    def test_persistent_failure_alerts_once_with_the_reason(self):
        sent = []
        run_with(sent, effect=bot.SourceError("HTTP 403"))
        run_with(sent, effect=bot.SourceError("HTTP 403"))

        assert len(sent) == 1
        assert "HTTP 403" in sent[0]
        assert bot.get_parse_error_flag() is True

    def test_failure_reason_is_html_escaped_in_the_alert(self):
        sent = []
        run_with(sent, effect=bot.SourceError("<b>boom</b>"))
        assert "&lt;b&gt;boom&lt;/b&gt;" in sent[0]

    def test_network_error_is_skipped_without_alert_or_flag(self):
        sent = []
        run_with(sent, effect=requests.ConnectionError("dns"))
        assert sent == [] and bot.get_parse_error_flag() is False

    def test_recovery_after_failure_with_same_release_sends_nothing(self):
        """Regression test for the real production bug seen 14-16 Aug 2026: a
        recovery re-announced a release that hadn't changed, because the
        failure marker used to clobber the real state."""
        sent = []
        run_with(sent, entries_for([A]))                              # baseline: A announced
        assert len(sent) == 1

        run_with(sent, effect=bot.SourceError("HTTP 500"))            # outage -> alert
        assert len(sent) == 2

        run_with(sent, entries_for([A]))                              # recovers with the SAME release
        assert len(sent) == 2, f"duplicate re-announcement sent: {sent}"

        run_with(sent, effect=bot.SourceError("HTTP 500"))            # a second outage must alert again
        assert len(sent) == 3


class TestHeartbeat:
    def test_fires_once_when_due(self):
        bot.save_release_keys({bot.release_key(*A)})
        sent = []
        with patch.object(bot, "get_last_heartbeat_date", return_value="2000-01-01"), \
             patch.object(bot, "send_telegram", side_effect=sending(sent)):
            bot.send_heartbeat_if_due()

        assert len(sent) == 1
        assert "Tracking <b>1</b> known build(s)" in sent[0]

    def test_does_not_resend_same_day(self):
        today = bot.datetime.now(bot.timezone.utc).strftime("%Y-%m-%d")
        bot.save_heartbeat_date(today)
        sent = []
        with patch.object(bot, "send_telegram", side_effect=sending(sent)):
            bot.send_heartbeat_if_due()
        assert sent == []

    def test_warns_instead_of_claiming_all_is_fine_while_the_feed_is_failing(self):
        """A heartbeat saying "fine" while nothing can be read hid a two-month outage."""
        bot.set_parse_error_flag(True)
        sent = []
        with patch.object(bot, "get_last_heartbeat_date", return_value="2000-01-01"), \
             patch.object(bot, "send_telegram", side_effect=sending(sent)):
            bot.send_heartbeat_if_due()

        assert len(sent) == 1
        assert "Failing" in sent[0] and "Running fine" not in sent[0]

    def test_says_fine_when_the_feed_is_healthy(self):
        sent = []
        with patch.object(bot, "get_last_heartbeat_date", return_value="2000-01-01"), \
             patch.object(bot, "send_telegram", side_effect=sending(sent)):
            bot.send_heartbeat_if_due()
        assert "Running fine" in sent[0]


class TestMessageStyle:
    def test_has_banner_summary_and_platform_emoji(self):
        message = bot.build_release_message([A, B])
        assert message.startswith(bot.SEPARATOR)
        assert "🍎 <b>iBETABOT</b>" in message
        assert "📱 <b>iOS</b>" in message and "📲 <b>iPadOS</b>" in message

    def test_group_header_shows_the_stage_of_its_builds(self):
        assert "🧪 <b>27.0</b>" in bot.build_release_message([A])              # beta
        assert "🏁 <b>26.6.2</b>" in bot.build_release_message([C])            # RC
        assert "📦 <b>27.2.1</b>" in bot.build_release_message(
            [("HomeView Software", "27.2.1", "", "24V553", "Nov 18, 2026")])  # plain release

    def test_a_group_mixing_stages_gets_the_neutral_header(self):
        mixed = [("iOS", "27.0", "beta 5", "1", ""), ("macOS", "27.0", "RC", "2", "")]
        assert "📦 <b>27.0</b>" in bot.build_release_message(mixed)

    def test_public_beta_note_only_accompanies_betas(self):
        assert "<blockquote>" in bot.build_release_message([A])
        assert "<blockquote>" not in bot.build_release_message([C])            # RC only
        assert "<blockquote>" not in bot.build_release_message([A], note=None)

    def test_details_link_is_configurable(self):
        message = bot.build_release_message([A], details=("https://ipsw.dev/", "ipsw.dev"))
        assert '<a href="https://ipsw.dev/">ipsw.dev</a>' in message

    def test_build_is_monospaced_and_the_current_year_is_dropped_from_dates(self):
        today = datetime.now(timezone.utc).date()
        stamp = f"{bot.MONTHS[today.month - 1]} {today.day}, {today.year}"
        message = bot.build_release_message([("iOS", "27.0", "beta 5", "24A5408d", stamp)])
        assert "<code>24A5408d</code>" in message
        assert f", {today.year}" not in message

    def test_short_date(self):
        today = date(2026, 11, 28)
        assert bot.short_date("Nov 19, 2026", today) == "Nov 19"
        assert bot.short_date("November 19, 2025", today) == "Nov 19, 2025"
        assert bot.short_date("sometime soon", today) == "sometime soon"

    def test_release_stage(self):
        assert bot.release_stage("beta 2 v2") == "🧪"
        assert bot.release_stage("RC 3") == "🏁"
        assert bot.release_stage("") == "📦"


class TestPerTrackerState:
    """ibeta_ipsw.py keeps its own state with the same helpers - files must not mix."""

    def test_helpers_use_the_given_path_and_leave_the_defaults_alone(self, tmp_path):
        other = str(tmp_path / "other_state.txt")
        bot.save_release_keys({"a (1)"}, other)
        assert bot.get_last_release_keys(other) == {"a (1)"}
        assert bot.get_last_release_keys() == set()

        flag = str(tmp_path / "other_flag.txt")
        bot.set_parse_error_flag(True, flag)
        assert bot.get_parse_error_flag(flag) is True and bot.get_parse_error_flag() is False

        beat = str(tmp_path / "other_beat.txt")
        bot.save_heartbeat_date("2026-01-01", beat)
        assert bot.get_last_heartbeat_date(beat) == "2026-01-01" and bot.get_last_heartbeat_date() == ""

    def test_failure_alert_uses_its_own_flag(self, tmp_path):
        flag = str(tmp_path / "flag.txt")
        sent = []
        with patch.object(bot, "send_telegram", side_effect=sending(sent)):
            bot.report_source_failure("ipsw.dev", "https://ipsw.dev/", "HTTP 403", flag_file=flag)
            bot.report_source_failure("ipsw.dev", "https://ipsw.dev/", "HTTP 403", flag_file=flag)
        assert len(sent) == 1 and "ipsw.dev" in sent[0] and "HTTP 403" in sent[0]
        assert bot.get_parse_error_flag(flag) is True and bot.get_parse_error_flag() is False

    def test_heartbeat_names_its_source_and_reads_its_own_files(self, tmp_path):
        state, beat, flag = (str(tmp_path / n) for n in ("s.txt", "b.txt", "f.txt"))
        bot.save_release_keys({"x (1)", "y (2)"}, state)
        sent = []
        with patch.object(bot, "send_telegram", side_effect=sending(sent)):
            bot.send_heartbeat_if_due("ipsw.dev (Mac agent)", beat, state, flag)
        assert "ipsw.dev (Mac agent)" in sent[0] and "Tracking <b>2</b>" in sent[0]
        assert bot.get_last_heartbeat_date(beat) != "" and bot.get_last_heartbeat_date() == ""


class TestEscaping:
    def test_apostrophes_are_not_turned_into_hex_entities(self):
        sent = []
        with patch.object(bot, "send_telegram", side_effect=sending(sent)):
            bot.report_source_failure("Apple's releases feed", "https://x.test/feed", "it's down")
        assert "Apple's releases feed" in sent[0] and "it's down" in sent[0]
        assert "&#x27;" not in sent[0]

    def test_angle_brackets_and_ampersands_are_still_escaped(self):
        sent = []
        with patch.object(bot, "send_telegram", side_effect=sending(sent)):
            bot.report_source_failure("a", "https://x.test/?a=1&b=2", "<b>&</b>")
        assert "&lt;b&gt;&amp;&lt;/b&gt;" in sent[0]
        assert 'href="https://x.test/?a=1&amp;b=2"' in sent[0]

