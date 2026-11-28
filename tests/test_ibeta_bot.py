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
        assert message.index("iOS beta 5") < message.index("iPadOS beta 5")

    def test_escapes_html_special_characters(self):
        malicious = ("iOS", "27.0", "<script>", "24A<b>", "Aug 1, 2026")
        message = bot.build_release_message([malicious])
        assert "<script>" not in message
        assert "&lt;script&gt;" in message

    def test_header_is_singular_for_one_release(self):
        assert "release detected" in bot.build_release_message([A])

    def test_header_is_plural_for_multiple_releases(self):
        assert "releases detected" in bot.build_release_message([A, B])

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
        assert len(sent) == 1 and "New beta release" in sent[0]
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
        assert "Tracking <b>1</b> known release(s)" in sent[0]

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
        assert "failing" in sent[0] and "running fine" not in sent[0]

    def test_says_fine_when_the_feed_is_healthy(self):
        sent = []
        with patch.object(bot, "get_last_heartbeat_date", return_value="2000-01-01"), \
             patch.object(bot, "send_telegram", side_effect=sending(sent)):
            bot.send_heartbeat_if_due()
        assert "running fine" in sent[0]
