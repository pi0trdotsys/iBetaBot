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


def html_for(items):
    """Builds a minimal ipsw.dev-shaped HTML snippet for the given releases.
    items: list of (system, version, suffix, build, date_text) tuples."""
    blocks = []
    for system, version, suffix, build, date_text in items:
        blocks.append(f'''
<h3 class="font-semibold text-gray-900 dark:text-gray-100">{system} {version} {suffix}</h3>
<p class="font-mono text-sm">{build}</p>
<p class="text-gray-800/60 dark:text-gray-200/60"><span class="text-sm">{date_text}</span></p>
''')
    return "\n".join(blocks)


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
    def test_release_pattern_matches_real_markup(self):
        matches = bot.RELEASE_PATTERN.findall(html_for([A]))
        assert len(matches) == 1
        system, version, suffix, build, date_text = matches[0]
        assert system == "iOS"
        assert version == "27.0"
        assert "beta 5" in suffix
        assert build == "24A5408d"
        assert "August 10, 2026" in date_text

    def test_release_key_format(self):
        assert bot.release_key(*A) == "iOS 27.0 beta 5 (24A5408d) | August 10, 2026"

    def test_release_key_without_suffix(self):
        no_suffix = ("iOS", "18.0", "", "22A100", "Jan 1, 2026")
        assert bot.release_key(*no_suffix) == "iOS 18.0 (22A100) | Jan 1, 2026"


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
