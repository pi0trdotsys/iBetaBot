# 🤖 iBetaBot

**iBetaBot** is a lightweight automation bot designed to monitor the latest Apple beta and Release Candidate firmware, including **iOS, iPadOS, macOS, tvOS, and visionOS**.

<div align="center">
    <img src="img/telegram_screenshot_conversation.jpg" alt="Application Screenshot 1" width="240" style="display: inline-block;"/>
</div>

The bot periodically fetches data from **IPSW.dev**, detects newly published builds, and sends structured notifications to a specified **Telegram chat**. To prevent duplicate alerts, it persists the last known release state and only reports changes when new versions appear.

Built for reliability and low operational overhead, iBetaBot is **cron-ready**, dependency-minimal, and ideal for developers, testers, and Apple platform enthusiasts who want timely beta release intelligence without manual tracking.
