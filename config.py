"""Environment configuration for the Shopify/Stripe site-hunter Telegram bot."""

import os


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Telegram
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = _int("API_ID")
API_HASH = os.environ.get("API_HASH", "")
TARGET_CHANNEL = os.environ.get("TARGET_CHANNEL", "")


# Data sources
BUILTWITH_KEY = os.environ.get("BUILTWITH_KEY", "")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CX = os.environ.get("GOOGLE_CX", "")


# Runtime
DB_PATH = os.environ.get("DB_PATH", "sites.db")
SCAN_INTERVAL = _int("SCAN_INTERVAL", 3600)
BUILTWITH_SINCE = os.environ.get("BUILTWITH_SINCE", "last day")
MAX_PER_SOURCE = _int("MAX_PER_SOURCE", 40)


# Rate limits
BUILTWITH_RPM = _int("BUILTWITH_RPM", 10)
GOOGLE_RPM = _int("GOOGLE_RPM", 30)
CRTSH_RPM = _int("CRTSH_RPM", 10)

MAX_RETRIES = _int("MAX_RETRIES", 5)


TECHNOLOGIES = ("shopify", "stripe")


def validate() -> list[str]:
    missing = []

    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")

    if not API_ID:
        missing.append("API_ID")

    if not API_HASH:
        missing.append("API_HASH")

    return missing
