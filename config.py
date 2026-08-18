"""Environment configuration for the Shopify/Stripe site-hunter Telegram bot."""

import os


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- Telegram ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = _int("API_ID")
API_HASH = os.environ.get("API_HASH", "")
# Channel/chat where new sites are forwarded, e.g. "@my_channel" or "-1001234567890"
TARGET_CHANNEL = os.environ.get("TARGET_CHANNEL", "")

# --- Data sources ---
BUILTWITH_KEY = os.environ.get("BUILTWITH_KEY", "")
SHODAN_KEY = os.environ.get("SHODAN_KEY", "")

# Optional: Google Programmable Search (dorks). Without these the Google
# source is skipped instead of scraping google.com (which gets blocked).
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CX = os.environ.get("GOOGLE_CX", "")

# --- Runtime ---
DB_PATH = os.environ.get("DB_PATH", "sites.db")
# Seconds between automatic scan cycles (default 1 hour)
SCAN_INTERVAL = _int("SCAN_INTERVAL", 3600)
# BuiltWith lookback window: "last day" ... or "last 5 days"
BUILTWITH_SINCE = os.environ.get("BUILTWITH_SINCE", "last day")
# Max results forwarded per source per cycle (avoids Telegram flood limits)
MAX_PER_SOURCE = _int("MAX_PER_SOURCE", 40)

# --- Rate limiting / retries (per-source calls per minute) ---
BUILTWITH_RPM = _int("BUILTWITH_RPM", 10)
GOOGLE_RPM = _int("GOOGLE_RPM", 30)
SHODAN_RPM = _int("SHODAN_RPM", 60)
# Retry attempts for transient errors (429/5xx/network) before giving up
MAX_RETRIES = _int("MAX_RETRIES", 5)

TECHNOLOGIES = ("shopify", "stripe")


def validate() -> list[str]:
    """Return a list of missing required settings."""
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not API_ID:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    return missing
