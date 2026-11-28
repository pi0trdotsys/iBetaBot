from unittest.mock import patch

import pytest

import ibeta_bot as bot


@pytest.fixture(autouse=True)
def isolate_state_files(tmp_path, monkeypatch):
    """Redirect every state file to a throwaway temp dir so tests never touch
    (or get confused by) the real production state."""
    monkeypatch.setattr(bot, "STATE_FILE", str(tmp_path / "state.txt"))
    monkeypatch.setattr(bot, "HEARTBEAT_FILE", str(tmp_path / "heartbeat.txt"))
    monkeypatch.setattr(bot, "PARSE_ERROR_FLAG_FILE", str(tmp_path / "parse_error.txt"))


def card(title, build, date_text=None, extra_classes="group shrink-0"):
    """One ipsw.dev release card in the shape the site uses today. Cards of the
    stable-release section have no date paragraph."""
    date_p = f'<p class="text-sm text-gray-800/60">{date_text}</p>' if date_text else ""
    return f'''
<a href="/build/{build}" class="{extra_classes}">
  <div class="h-full rounded-3xl p-5">
    <picture><img src="/img/os-logo/x.png" alt="" width="56" height="56"></picture>
    <p class="mt-4 font-semibold text-gray-900">{title}</p>
    <p class="text-sm font-mono text-pink-600">{build}</p>
    {date_p}
  </div>
</a>
'''


def html_for(items):
    """Builds a minimal ipsw.dev-shaped page for the given releases.
    items: list of (system, version, suffix, build, date_text) tuples."""
    return "\n".join(
        card(f"{system} {version} {suffix}".strip(), build, date_text)
        for system, version, suffix, build, date_text in items
    )


A = ("iOS", "27.0", "beta 5", "24A5408d", "August 10, 2026")
B = ("iPadOS", "27.0", "beta 5", "24A5408d", "August 10, 2026")
C = ("macOS", "26.6.2", "RC", "25G82", "August 10, 2026")
D_NEW = ("tvOS", "27.0", "beta 6", "24J5350a", "August 20, 2026")


def sending(sent_list):
    """A send_telegram stand-in that records the message and always succeeds."""
    def _send(message):
        sent_list.append(message)
        return True
    return _send


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
        # persisted as sorted lines, not just an arbitrary set dump
        assert bot.get_last_state().split("\n") == sorted(bot.get_last_state().split("\n"))


class TestReleaseParsing:
    def test_parses_a_dated_card(self):
        assert bot.parse_releases(html_for([A])) == [A]

    def test_multiword_system_and_missing_suffix(self):
        page = card("HomeView Software 27.2.1", "24V553", "Nov 18, 2026")
        assert bot.parse_releases(page) == [("HomeView Software", "27.2.1", "", "24V553", "Nov 18, 2026")]

    def test_ignores_css_classes_entirely(self):
        """The parser broke twice on restyles; a class change must not matter."""
        page = card("iOS 27.3 beta 2", "24C5049d", "Nov 16, 2026", extra_classes="totally different classes")
        assert bot.parse_releases(page) == [("iOS", "27.3", "beta 2", "24C5049d", "Nov 16, 2026")]

    def test_skips_undated_stable_release_cards(self):
        page = html_for([A]) + card("iOS 27.2", "24B119")
        assert bot.parse_releases(page) == [A]

    def test_skips_card_whose_build_does_not_match_its_link(self):
        page = '<a href="/build/AAA"><p>iOS 27.0 beta 1</p><p>BBB</p><p>Nov 1, 2026</p></a>'
        assert bot.parse_releases(page) == []

    def test_unescapes_html_entities(self):
        page = card("iOS 27.0 beta &amp; more", "24A1", "Nov 1, 2026")
        assert bot.parse_releases(page)[0][2] == "beta & more"

    def test_empty_or_unrelated_page_yields_nothing(self):
        assert bot.parse_releases("<html><body>nothing here</body></html>") == []


class TestReleaseKey:
    def test_key_format(self):
        assert bot.release_key(*A) == "iOS 27.0 beta 5 (24A5408d)"

    def test_key_without_suffix(self):
        assert bot.release_key("iOS", "18.0", "", "22A100", "Jan 1, 2026") == "iOS 18.0 (22A100)"

    def test_date_is_not_part_of_the_identity(self):
        reformatted = A[:4] + ("10 Aug 2026",)
        assert bot.release_key(*A) == bot.release_key(*reformatted)

    def test_legacy_state_lines_with_dates_still_match(self):
        bot.save_state("iOS 27.0 beta 5 (24A5408d) | August 10, 2026\niPadOS 27.0 beta 5 (24A5408d) | August 10, 2026")
        assert bot.get_last_release_keys() == {bot.release_key(*A), bot.release_key(*B)}


class TestBuildReleaseMessage:
    def test_groups_by_version_and_sorts_systems(self):
        # iPadOS passed before iOS - SYSTEM_ORDER should still put iOS first
        message = bot.build_release_message([B, A])
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


class TestRunNewReleaseDetection:
    def test_first_run_baselines_everything_as_new(self):
        sent = []
        with patch.object(bot, "fetch_html", return_value=html_for([A, B, C])), \
             patch.object(bot, "send_telegram", side_effect=sending(sent)), \
             patch.object(bot, "send_heartbeat_if_due"):
            bot.run()

        assert len(sent) == 1
        assert bot.get_last_release_keys() == {
            bot.release_key(*A), bot.release_key(*B), bot.release_key(*C),
        }

    def test_unchanged_page_sends_nothing(self):
        bot.save_release_keys({bot.release_key(*A), bot.release_key(*B), bot.release_key(*C)})
        sent = []
        with patch.object(bot, "fetch_html", return_value=html_for([A, B, C])), \
             patch.object(bot, "send_telegram", side_effect=sending(sent)), \
             patch.object(bot, "send_heartbeat_if_due"):
            bot.run()

        assert sent == []

    def test_page_reorder_alone_sends_nothing(self):
        """State tracks the full set, not just the first entry - reordering
        the page must never be mistaken for a change."""
        bot.save_release_keys({bot.release_key(*A), bot.release_key(*B), bot.release_key(*C)})
        sent = []
        with patch.object(bot, "fetch_html", return_value=html_for([C, A, B])), \
             patch.object(bot, "send_telegram", side_effect=sending(sent)), \
             patch.object(bot, "send_heartbeat_if_due"):
            bot.run()

        assert sent == []

    def test_new_release_not_first_in_page_order_is_still_detected(self):
        """The historical design only compared the page's first entry, so a
        new release landing anywhere else in the list was silently missed."""
        bot.save_release_keys({bot.release_key(*A), bot.release_key(*B), bot.release_key(*C)})
        sent = []
        with patch.object(bot, "fetch_html", return_value=html_for([A, D_NEW, B, C])), \
             patch.object(bot, "send_telegram", side_effect=sending(sent)), \
             patch.object(bot, "send_heartbeat_if_due"):
            bot.run()

        assert len(sent) == 1
        assert "tvOS" in sent[0] and "beta 6" in sent[0]
        # only the new item is reported, not a resend of the whole listing
        assert "beta 5" not in sent[0]
        assert "RC" not in sent[0]
        assert bot.get_last_release_keys() == {
            bot.release_key(*A), bot.release_key(*B), bot.release_key(*C), bot.release_key(*D_NEW),
        }

    def test_failed_send_does_not_persist_state(self):
        with patch.object(bot, "fetch_html", return_value=html_for([A])), \
             patch.object(bot, "send_telegram", return_value=False), \
             patch.object(bot, "send_heartbeat_if_due"):
            bot.run()

        assert bot.get_last_release_keys() == set()


class TestParseFailureHandling:
    def test_transient_empty_parse_recovers_via_retry(self):
        sent = []
        with patch.object(bot.time, "sleep"), \
             patch.object(bot, "fetch_html", side_effect=["<html></html>", html_for([A])]), \
             patch.object(bot, "send_telegram", side_effect=sending(sent)), \
             patch.object(bot, "send_heartbeat_if_due"):
            bot.run()

        assert len(sent) == 1 and "New beta release" in sent[0]

    def test_persistent_parse_failure_alerts_once_not_twice(self):
        sent = []
        with patch.object(bot.time, "sleep"), \
             patch.object(bot, "fetch_html", return_value="<html></html>"), \
             patch.object(bot, "send_telegram", side_effect=sending(sent)), \
             patch.object(bot, "send_heartbeat_if_due"):
            bot.run()
            bot.run()

        assert len(sent) == 1
        assert bot.get_parse_error_flag() is True

    def test_recovery_after_failure_with_same_release_sends_nothing(self):
        """Regression test for the real production bug seen 14-16 Aug 2026: a
        parse-error recovery re-announced a release that hadn't actually
        changed, because the failure sentinel used to clobber the real state."""
        sent = []
        with patch.object(bot.time, "sleep"), patch.object(bot, "send_heartbeat_if_due"):
            with patch.object(bot, "fetch_html", return_value=html_for([A])), \
                 patch.object(bot, "send_telegram", side_effect=sending(sent)):
                bot.run()  # baseline: A announced
            assert len(sent) == 1

            with patch.object(bot, "fetch_html", return_value="<html></html>"), \
                 patch.object(bot, "send_telegram", side_effect=sending(sent)):
                bot.run()  # parse error -> alert
            assert len(sent) == 2

            with patch.object(bot, "fetch_html", return_value=html_for([A])), \
                 patch.object(bot, "send_telegram", side_effect=sending(sent)):
                bot.run()  # recovers with the SAME release - must stay quiet
            assert len(sent) == 2, f"duplicate re-announcement sent: {sent}"

            with patch.object(bot, "fetch_html", return_value="<html></html>"), \
                 patch.object(bot, "send_telegram", side_effect=sending(sent)):
                bot.run()  # a second outage must still alert (flag correctly reset)
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

    def test_warns_instead_of_claiming_all_is_fine_while_scraper_is_failing(self):
        bot.set_parse_error_flag(True)
        sent = []
        with patch.object(bot, "get_last_heartbeat_date", return_value="2000-01-01"), \
             patch.object(bot, "send_telegram", side_effect=sending(sent)):
            bot.send_heartbeat_if_due()

        assert len(sent) == 1
        assert "failing" in sent[0] and "running fine" not in sent[0]

    def test_says_fine_when_scraper_is_healthy(self):
        sent = []
        with patch.object(bot, "get_last_heartbeat_date", return_value="2000-01-01"), \
             patch.object(bot, "send_telegram", side_effect=sending(sent)):
            bot.send_heartbeat_if_due()

        assert "running fine" in sent[0]
