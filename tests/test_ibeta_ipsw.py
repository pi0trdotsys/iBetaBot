from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
import requests

import ibeta_bot as bot
import ibeta_ipsw as ipsw


@pytest.fixture(autouse=True)
def isolate_state_files(tmp_path, monkeypatch):
    """Both trackers' files go to a temp dir - and the bot's own files are
    redirected too, so a test can prove the agent never touches them."""
    monkeypatch.setattr(ipsw, "STATE_FILE", str(tmp_path / "ipsw_state.txt"))
    monkeypatch.setattr(ipsw, "HEARTBEAT_FILE", str(tmp_path / "ipsw_heartbeat.txt"))
    monkeypatch.setattr(ipsw, "FLAG_FILE", str(tmp_path / "ipsw_flag.txt"))
    monkeypatch.setattr(bot, "STATE_FILE", str(tmp_path / "bot_state.txt"))
    monkeypatch.setattr(bot, "HEARTBEAT_FILE", str(tmp_path / "bot_heartbeat.txt"))
    monkeypatch.setattr(bot, "PARSE_ERROR_FLAG_FILE", str(tmp_path / "bot_flag.txt"))


def stamp(days_ago=0):
    day = datetime.now(timezone.utc).date() - timedelta(days=days_ago)
    return f"{bot.MONTHS[day.month - 1]} {day.day}, {day.year}"


def card(title, build, date_text=None, extra_classes="group shrink-0"):
    """One ipsw.dev release card in the shape the site uses today. Cards of the
    stable-release section have no date paragraph."""
    date_p = f'<p class="text-sm text-gray-800/60">{date_text}</p>' if date_text else ""
    return f"""
<a href="/build/{build}" class="{extra_classes}">
  <div class="h-full rounded-3xl p-5">
    <picture><img src="/img/os-logo/x.png" alt="" width="56" height="56"></picture>
    <p class="mt-4 font-semibold text-gray-900">{title}</p>
    <p class="text-sm font-mono text-pink-600">{build}</p>
    {date_p}
  </div>
</a>
"""


AUDIO = ("audioOS", "27.3", "beta 2", "24K5298d")
HOME = ("HomeView Software", "27.2.1", "", "24V553")


def page(*cards_html):
    return "<html><head><title>IPSW.dev</title></head><body>" + "".join(cards_html) + "</body></html>"


def dated(release, days_ago=0):
    system, version, label, build = release
    return card(f"{system} {version} {label}".strip(), build, stamp(days_ago))


def response(status=200, text=""):
    return Mock(status_code=status, text=text, content=text.encode())


def run_with(sent, html_text=None, effect=None):
    source = {"side_effect": effect} if effect is not None else {"return_value": response(200, html_text)}
    with patch.object(bot.time, "sleep"), \
         patch.object(ipsw.core.session, "get", **source), \
         patch.object(bot, "send_telegram", side_effect=lambda m: (sent.append(m), True)[1]), \
         patch.object(bot, "send_heartbeat_if_due"):
        ipsw.run()


class TestParseCards:
    def test_parses_dated_cards_of_any_product(self):
        cards = ipsw.parse_cards(page(card("iOS 27.3 beta 2 v2", "24C5049f", "Nov 19, 2026"), dated(AUDIO)))
        assert cards[0] == ("iOS", "27.3", "beta 2 v2", "24C5049f", "Nov 19, 2026")
        assert cards[1][0] == "audioOS"

    def test_multiword_product_without_label(self):
        cards = ipsw.parse_cards(page(card("HomeView Software 27.2.1", "24V553", "Nov 18, 2026")))
        assert cards == [("HomeView Software", "27.2.1", "", "24V553", "Nov 18, 2026")]

    def test_ignores_css_classes_entirely(self):
        cards = ipsw.parse_cards(page(card("audioOS 27.3 beta 2", "24K5298d", "Nov 16, 2026", extra_classes="whatever else")))
        assert len(cards) == 1

    def test_skips_undated_stable_release_cards(self):
        assert ipsw.parse_cards(page(card("iOS 27.2", "24B119"))) == []

    def test_skips_a_card_whose_build_does_not_match_its_link(self):
        assert ipsw.parse_cards('<a href="/build/AAA"><p>audioOS 27.0 beta 1</p><p>BBB</p><p>Nov 1, 2026</p></a>') == []


class TestSelectReleases:
    TODAY = date(2026, 11, 28)

    def test_keeps_only_the_products_the_apple_feed_lacks(self):
        cards = [("iOS", "27.3", "beta 2", "1", "Nov 19, 2026"), ("audioOS", "27.3", "beta 2", "2", "Nov 16, 2026"),
                 ("HomeView Software", "27.2.1", "", "3", "Nov 18, 2026"), ("watchOS", "27.3", "beta 2", "4", "Nov 16, 2026")]
        assert [c[0] for c in ipsw.select_releases(cards, self.TODAY)] == ["audioOS", "HomeView Software"]

    def test_the_two_trackers_never_overlap(self):
        assert not set(bot.FEED_SYSTEMS) & set(bot.IPSW_SYSTEMS)

    def test_drops_releases_older_than_max_age(self):
        cards = [("audioOS", "27.0", "beta 1", "1", "Sep 1, 2026"), ("audioOS", "27.3", "beta 2", "2", "Nov 16, 2026")]
        assert [c[3] for c in ipsw.select_releases(cards, self.TODAY)] == ["2"]

    def test_a_page_with_cards_but_none_of_ours_is_just_empty(self):
        assert ipsw.select_releases([("iOS", "27.3", "beta 2", "1", "Nov 19, 2026")], self.TODAY) == []


class TestLoadCards:
    def test_cloudflare_block_is_reported_with_status_and_page_title(self):
        blocked = "<html><head><title>Just a moment...</title></head></html>"
        with patch.object(ipsw.core.session, "get", return_value=response(403, blocked)):
            with pytest.raises(bot.SourceError, match=r"HTTP 403.*Just a moment"):
                ipsw.load_cards()

    def test_page_without_any_card_is_a_source_error(self):
        with patch.object(ipsw.core.session, "get", return_value=response(200, "<html><title>IPSW</title></html>")):
            with pytest.raises(bot.SourceError, match="no release cards"):
                ipsw.load_cards()

    def test_returns_the_cards(self):
        with patch.object(ipsw.core.session, "get", return_value=response(200, page(dated(AUDIO)))):
            assert ipsw.load_cards()[0][3] == "24K5298d"


class TestRun:
    def test_announces_new_audioos_and_homeview_from_ipsw_without_the_public_beta_note(self):
        sent = []
        run_with(sent, page(dated(AUDIO), dated(HOME)))

        assert len(sent) == 1
        assert "audioOS" in sent[0] and "HomeView Software" in sent[0]
        assert "2 new builds" in sent[0]
        assert '<a href="https://ipsw.dev/">ipsw.dev</a>' in sent[0]
        assert "<blockquote>" not in sent[0]

    def test_second_run_with_the_same_page_sends_nothing(self):
        sent = []
        run_with(sent, page(dated(AUDIO)))
        run_with(sent, page(dated(AUDIO)))
        assert len(sent) == 1

    def test_ignores_products_the_apple_feed_already_covers(self):
        sent = []
        run_with(sent, page(card("iOS 27.3 beta 2", "24C5049d", stamp())))
        assert sent == []

    def test_never_touches_the_actions_bots_state_files(self, tmp_path):
        run_with([], page(dated(AUDIO)))
        assert bot.get_last_release_keys() == set()
        assert (tmp_path / "ipsw_state.txt").read_text().strip() == "audioOS 27.3 beta 2 (24K5298d)"

    def test_cloudflare_block_alerts_once_with_its_own_flag(self):
        sent = []
        blocked = response(403, "<html><head><title>Just a moment...</title></head></html>")
        run_with(sent, effect=[blocked] * 8)
        run_with(sent, effect=[blocked] * 8)

        assert len(sent) == 1 and "HTTP 403" in sent[0] and "ipsw.dev" in sent[0]
        assert bot.get_parse_error_flag(ipsw.FLAG_FILE) is True
        assert bot.get_parse_error_flag() is False

    def test_recovery_with_the_same_release_stays_quiet_and_clears_the_flag(self):
        sent = []
        run_with(sent, page(dated(AUDIO)))                                         # announced
        run_with(sent, effect=[response(403, "x")] * 8)                            # outage -> alert
        assert len(sent) == 2
        run_with(sent, page(dated(AUDIO)))                                         # back, nothing new
        assert len(sent) == 2
        assert bot.get_parse_error_flag(ipsw.FLAG_FILE) is False

    def test_network_error_is_skipped_silently(self):
        sent = []
        run_with(sent, effect=requests.ConnectionError("dns"))
        assert sent == [] and bot.get_parse_error_flag(ipsw.FLAG_FILE) is False

    def test_heartbeat_comes_from_the_mac_agent_and_is_separate_from_the_bots(self, tmp_path):
        sent = []
        with patch.object(bot.time, "sleep"), \
             patch.object(ipsw.core.session, "get", return_value=response(200, page(dated(AUDIO)))), \
             patch.object(bot, "send_telegram", side_effect=lambda m: (sent.append(m), True)[1]):
            ipsw.run()

        beats = [m for m in sent if "heartbeat" in m]
        assert len(beats) == 1 and "ipsw.dev (Mac agent)" in beats[0] and "Tracking <b>1</b>" in beats[0]
        assert (tmp_path / "ipsw_heartbeat.txt").exists() and not (tmp_path / "bot_heartbeat.txt").exists()
