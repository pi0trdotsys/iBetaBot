# ⚡ iBetaBot

**Real-time Apple beta & RC firmware watchdog — iOS, iPadOS, macOS, tvOS & visionOS builds pushed straight to Telegram the moment they drop.**

[![Bot status](https://img.shields.io/github/actions/workflow/status/pi0trdotsys/iBetaBot/ibeta_bot.yml?branch=main&label=bot%20status)](https://github.com/pi0trdotsys/iBetaBot/actions/workflows/ibeta_bot.yml)
[![Tests](https://img.shields.io/github/actions/workflow/status/pi0trdotsys/iBetaBot/tests.yml?branch=main&label=tests)](https://github.com/pi0trdotsys/iBetaBot/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/github/license/pi0trdotsys/iBetaBot)](LICENSE)
[![Last commit](https://img.shields.io/github/last-commit/pi0trdotsys/iBetaBot)](https://github.com/pi0trdotsys/iBetaBot/commits/main)

<div align="center">
    <img src="img/telegram_screenshot_conversation.jpg" alt="iBetaBot Telegram notification" width="280" style="display: inline-block;"/>
</div>

## Why iBetaBot

No refreshing Apple's release pages, no missed builds, no noise. iBetaBot watches Apple's beta pipeline for you and only speaks up when something actually changes — cron-ready, dependency-light, and running for free on GitHub's own infrastructure.

- 🔭 **Continuous monitoring** — reads Apple's official [developer releases feed](https://developer.apple.com/news/releases/) on a 30-minute schedule for new iOS, iPadOS, macOS, tvOS, visionOS and watchOS beta / RC builds. (It used to scrape IPSW.dev, which now blocks GitHub's servers with a Cloudflare challenge.)
- 🎯 **Signal, not spam** — tracks the *full* set of known releases (not just the page's first entry), so a new build is never missed just because it isn't listed first, and notifications call out only what's actually new instead of resending the whole listing every time; a transient page hiccup gets one retry before it's ever treated as a real break.
- 💬 **Telegram alerts that are a pleasure to read** — a banner and a one-line summary, builds grouped by version with a stage emoji (🧪 beta · 🏁 RC · 📦 release), a platform emoji per line (📱 iOS · 📲 iPadOS · 💻 macOS · 📺 tvOS · 🥽 visionOS · ⌚ watchOS · 🔊 audioOS · 🏠 HomeView), monospaced build numbers and short dates. The same look is used for the heartbeat and for source-down alerts.
- 🧭 **Public Beta context** — alerts that contain a beta carry a historical note on when the Public Beta typically follows a given Developer Beta.
- 🔊 **audioOS & HomeView too** — Apple's feed doesn't carry them, so a small [Mac agent](#mac-agent-audioos--homeview) reads them from ipsw.dev.
- 💓 **Honest daily heartbeat** — one quiet ping every day confirms the bot is alive even when nothing new has shipped, and it says so loudly if the feed can't be read, instead of claiming everything is fine.
- ☁️ **Zero-maintenance hosting** — runs on GitHub Actions, so it keeps working while your machine is off or asleep.

## How it works

1. Fetches Apple's releases feed and keeps the beta / RC builds of Apple's operating systems (releases older than 30 days are never announced).
2. Compares them against the full set of previously known releases (`ibeta_last_state.txt`).
3. On any genuinely new release, sends a Telegram message naming just what's new and updates the state.
4. Once per UTC day, sends a heartbeat regardless of whether anything changed.

## Running via GitHub Actions (recommended)

The included [`.github/workflows/ibeta_bot.yml`](.github/workflows/ibeta_bot.yml) runs the bot every 30 minutes on GitHub's infrastructure — no server, no always-on laptop required. It commits `ibeta_last_state.txt` and `ibeta_heartbeat_state.txt` back to the repo after each run so state survives between the ephemeral runners.

To enable it, add two repository secrets under **Settings → Secrets and variables → Actions**:

| Secret | Description |
|---|---|
| `TELEGRAM_TOKEN` | Your bot token from [@BotFather](https://t.me/BotFather) |
| `CHAT_ID` | The target chat ID to notify |

You can also trigger a run on demand from the **Actions** tab (`workflow_dispatch`).

## Mac agent (audioOS & HomeView)

[`ibeta_ipsw.py`](ibeta_ipsw.py) covers the two products Apple's feed lacks, using [ipsw.dev](https://ipsw.dev/). ipsw.dev answers GitHub's datacenter IPs with a Cloudflare challenge but serves a normal home connection fine, so this part runs on a Mac via a LaunchAgent rather than on Actions. It reuses the bot's formatting, Telegram sending, one-alert-per-outage flag and daily heartbeat, but keeps its own **machine-local, git-ignored** state (`ibeta_ipsw_*.txt`), so it never collides with what the workflow commits. The two trackers cover disjoint product lists, so nothing is announced twice. Its heartbeat is labelled *ipsw.dev (Mac agent)* so you can tell the two daily pings apart.

1. Create `.env` next to the scripts (it's git-ignored) from [`.env.example`](.env.example) — the same `TELEGRAM_TOKEN` / `CHAT_ID` as the repository secrets.
2. Save this as `~/Library/LaunchAgents/pl.ibetabot.ipsw.plist`, adjusting the two paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>pl.ibetabot.ipsw</string>
  <key>ProgramArguments</key><array>
    <string>/path/to/python3</string>
    <string>/path/to/iBetaBot/ibeta_ipsw.py</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/iBetaBot</string>
  <key>StartInterval</key><integer>1800</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/Users/you/Library/Logs/iBetaBot/ipsw.out.log</string>
  <key>StandardErrorPath</key><string>/Users/you/Library/Logs/iBetaBot/ipsw.err.log</string>
</dict></plist>
```

3. `mkdir -p ~/Library/Logs/iBetaBot && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/pl.ibetabot.ipsw.plist` (add `launchctl kickstart -k gui/$(id -u)/pl.ibetabot.ipsw` to run it right away).

While the Mac sleeps nothing runs; launchd fires a missed tick once after wake.

## Running locally

```bash
git clone https://github.com/pi0trdotsys/iBetaBot.git
cd iBetaBot
pip install -r requirements.txt

# create a .env file with:
# TELEGRAM_TOKEN=your-bot-token
# CHAT_ID=your-chat-id

python ibeta_bot.py
```

Pair it with your own cron/launchd schedule if you'd rather self-host than use GitHub Actions.

## Testing

```bash
pip install -r requirements-dev.txt
pytest -v
```

The [`tests/`](tests/) suite covers both trackers: state persistence, the retry/source-failure handling, heartbeat cadence, message formatting, and — as a standing regression test — the exact duplicate-announcement bug that once shipped in production. A separate [`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs it on every push and pull request, independent of the bot's own scheduled workflow.

## License

[MIT](LICENSE) © Piotr Rosiński
