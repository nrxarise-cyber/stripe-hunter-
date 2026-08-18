"""Environment configuration for the Shopify/Stripe site-hunter Telegram bot."""

import os


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ============================================================
# TELEGRAM
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = _int("API_ID")
API_HASH = os.environ.get("API_HASH", "")

# Example:
# @mychannel
# or
# -1001234567890
TARGET_CHANNEL = os.environ.get("TARGET_CHANNEL", "")


# ============================================================
# DATA SOURCES
# ============================================================

# BuiltWith Change API
BUILTWITH_KEY = os.environ.get("BUILTWITH_KEY", "")


# ============================================================
# RUNTIME
# ============================================================

# SQLite database
DB_PATH = os.environ.get(
    "DB_PATH",
    "sites.db",
)

# Automatic scan interval.
# 3600 = 1 hour
SCAN_INTERVAL = _int(
    "SCAN_INTERVAL",
    3600,
)

# BuiltWith lookback window
BUILTWITH_SINCE = os.environ.get(
    "BUILTWITH_SINCE",
    "last day",
)

# Maximum results collected from each source
MAX_PER_SOURCE = _int(
    "MAX_PER_SOURCE",
    40,
)


# ============================================================
# RATE LIMITS
# ============================================================

BUILTWITH_RPM = _int(
    "BUILTWITH_RPM",
    10,
)

CRTSH_RPM = _int(
    "CRTSH_RPM",
    10,
)

MAX_RETRIES = _int(
    "MAX_RETRIES",
    5,
)


# ============================================================
# TARGET TECHNOLOGIES
# ============================================================

TECHNOLOGIES = (
    "shopify",
    "stripe",
)


# ============================================================
# VALIDATION
# ============================================================

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
