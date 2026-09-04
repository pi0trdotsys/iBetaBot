import os

# Must run before ibeta_bot is imported anywhere (it raises at import time if
# these are missing). os.environ.setdefault() never overrides a real .env
# value a developer might have locally - it only fills in the gap for CI/tests.
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("CHAT_ID", "test-chat-id")
