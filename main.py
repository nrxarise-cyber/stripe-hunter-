"""
Shopify / Stripe Site Hunter Telegram Bot.

Sources:
    - BuiltWith Change API

Telegram:
    - /start
    - /stats
    - /latest
    - /search <query>
    - /scan

The bot stores unique domains in SQLite and forwards new
discoveries to TARGET_CHANNEL.
"""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from pyrogram import Client, filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.types import Message

import config
from ratelimit import RateLimiter, RateLimitError, request_with_backoff


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("sitehunter")


# ============================================================
# RATE LIMITER
# ============================================================

BUILTWITH_LIMITER = RateLimiter(
    "BuiltWith",
    config.BUILTWITH_RPM,
)


# ============================================================
# DOMAIN FILTER
# ============================================================

DOMAIN_RE = re.compile(
    r"^(?:"
    r"[a-z0-9]"
    r"(?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"\.)+"
    r"[a-z]{2,}$"
)

SKIP_HOSTS = (
    "shopify.com",
    "stripe.com",
    "google.com",
    "youtube.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "reddit.com",
    "wikipedia.org",
    "github.com",
    "linkedin.com",
    "instagram.com",
    "pinterest.com",
)


def clean_domain(value: str) -> str | None:
    if not value:
        return None

    value = value.strip().lower()

    if "://" in value:
        value = urlparse(value).netloc or value

    value = (
        value
        .split("/")[0]
        .split("?")[0]
        .split(":")[0]
    )

    if value.startswith("www."):
        value = value[4:]

    if value.startswith("*."):
        value = value[2:]

    if not DOMAIN_RE.match(value):
        return None

    if any(
        value == host or value.endswith("." + host)
        for host in SKIP_HOSTS
    ):
        return None

    return value


# ============================================================
# DATABASE
# ============================================================

class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(
            path,
            check_same_thread=False,
        )

        self.conn.row_factory = sqlite3.Row

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL UNIQUE,
                technology TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                source TEXT NOT NULL
            )
            """
        )

        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_first_seen
            ON sites(first_seen)
            """
        )

        self.conn.commit()

    def add(
        self,
        domain: str,
        technology: str,
        source: str,
    ) -> bool:
        try:
            self.conn.execute(
                """
                INSERT INTO sites
                (domain, technology, first_seen, source)
                VALUES (?, ?, ?, ?)
                """,
                (
                    domain,
                    technology,
                    datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    source,
                ),
            )

            self.conn.commit()
            return True

        except sqlite3.IntegrityError:
            return False

    def exists(self, domain: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1
            FROM sites
            WHERE domain = ?
            """,
            (domain,),
        ).fetchone()

        return row is not None

    def get_first_seen(self, domain: str) -> str:
        row = self.conn.execute(
            """
            SELECT first_seen
            FROM sites
            WHERE domain = ?
            """,
            (domain,),
        ).fetchone()

        return row["first_seen"] if row else ""

    def stats(self) -> dict:
        total = self.conn.execute(
            "SELECT COUNT(*) FROM sites"
        ).fetchone()[0]

        by_tech = dict(
            self.conn.execute(
                """
                SELECT technology, COUNT(*)
                FROM sites
                GROUP BY technology
                """
            ).fetchall()
        )

        by_source = dict(
            self.conn.execute(
                """
                SELECT source, COUNT(*)
                FROM sites
                GROUP BY source
                """
            ).fetchall()
        )

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=1)
        ).isoformat(timespec="seconds")

        last24 = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM sites
            WHERE first_seen >= ?
            """,
            (cutoff,),
        ).fetchone()[0]

        return {
            "total": total,
            "by_tech": by_tech,
            "by_source": by_source,
            "last24": last24,
        }

    def latest(
        self,
        hours: int = 24,
        limit: int = 50,
    ) -> list[sqlite3.Row]:

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(hours=hours)
        ).isoformat(timespec="seconds")

        return self.conn.execute(
            """
            SELECT *
            FROM sites
            WHERE first_seen >= ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()

    def search(
        self,
        query: str,
        limit: int = 30,
    ) -> list[sqlite3.Row]:

        like = f"%{query.lower()}%"

        return self.conn.execute(
            """
            SELECT *
            FROM sites
            WHERE
                lower(domain) LIKE ?
                OR lower(technology) LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (like, like, limit),
        ).fetchall()


db = DB(config.DB_PATH)


# ============================================================
# BUILTWITH
# ============================================================

def fetch_builtwith(
    technology: str,
    since: str | None = None,
) -> list[str]:

    if not config.BUILTWITH_KEY:
        log.warning(
            "BUILTWITH_KEY is not configured"
        )
        return []

    since = since or config.BUILTWITH_SINCE

    try:
        response = request_with_backoff(
            BUILTWITH_LIMITER,
            "GET",
            "https://api.builtwith.com/change1/api.json",
            params={
                "KEY": config.BUILTWITH_KEY,
                "LOOKUP": f"{technology}.com",
                "SINCE": since.replace(" ", "+"),
            },
            timeout=45,
            max_retries=config.MAX_RETRIES,
        )

        data = response.json()

    except (RateLimitError, Exception) as exc:
        log.warning(
            "BuiltWith %s failed: %s",
            technology,
            exc,
        )
        return []

    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):

            for key in ("Domain", "D", "domain"):
                value = node.get(key)

                if isinstance(value, str):
                    domain = clean_domain(value)

                    if domain:
                        found.append(domain)

            for value in node.values():
                walk(value)

        elif isinstance(node, list):

            for value in node:
                walk(value)

    walk(data)

    result = list(dict.fromkeys(found))

    log.info(
        "BuiltWith %s — %s domains",
        technology,
        len(result),
    )

    return result[:config.MAX_PER_SOURCE]


# ============================================================
# SOURCE SCANNER
# ============================================================

def run_sources(
    technology: str,
) -> list[tuple[str, str]]:

    results: list[tuple[str, str]] = []

    try:
        domains = fetch_builtwith(technology)

        for domain in domains:
            results.append(
                (domain, "BuiltWith")
            )

    except Exception as exc:
        log.exception(
            "BuiltWith crashed for %s: %s",
            technology,
            exc,
        )

    return results


# ============================================================
# TELEGRAM
# ============================================================

app = Client(
    "sitehunter",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
)


# ============================================================
# TARGET CHAT
# ============================================================

async def resolve_target_chat():
    """
    Resolve TARGET_CHANNEL/TARGET_GROUP at startup.

    This catches invalid private-group IDs before scanning.
    """

    if not config.TARGET_CHANNEL:
        log.info(
            "TARGET_CHANNEL not configured."
        )
        return None

    try:
        chat = await app.get_chat(
            config.TARGET_CHANNEL
        )

        log.info(
            "Target resolved | title=%s | id=%s | type=%s",
            chat.title or chat.first_name or "unknown",
            chat.id,
            chat.type,
        )

        return chat.id

    except Exception as exc:
        log.error(
            "TARGET_CHANNEL cannot be resolved: %s",
            exc,
        )

        return None


# ============================================================
# FORMAT
# ============================================================

def format_hit(
    domain: str,
    technology: str,
    first_seen: str,
    source: str,
) -> str:

    return (
        "🆕 **New site detected**\n\n"
        f"🌐 **Domain:** `{domain}`\n"
        f"🧩 **Technology:** {technology.capitalize()}\n"
        f"📅 **Detected:** "
        f"{first_seen.replace('T', ' ')} UTC\n"
        f"🔎 **Source:** {source}\n"
        f"🔗 https://{domain}"
    )


# ============================================================
# SEND SAFELY
# ============================================================

async def safe_send(
    chat_id,
    text: str,
) -> bool:

    try:
        await app.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )

        return True

    except Exception as exc:
        log.error(
            "Telegram send failed | chat=%s | error=%s",
            chat_id,
            exc,
        )

        return False


# ============================================================
# SCAN
# ============================================================

async def scan_and_forward(
    notify_chat=None,
) -> int:

    new_count = 0

    target_ids: list[int | str] = []

    # Configured target
    if config.TARGET_CHANNEL:
        target_ids.append(
            config.TARGET_CHANNEL
        )

    # If /scan was executed in a group/private chat,
    # also send the results there.
    if notify_chat and notify_chat not in target_ids:
        target_ids.append(
            notify_chat
        )

    log.info(
        "Scan started | targets=%s",
        target_ids,
    )

    for technology in config.TECHNOLOGIES:

        log.info(
            "Scanning %s...",
            technology,
        )

        hits = await asyncio.to_thread(
            run_sources,
            technology,
        )

        log.info(
            "%s returned %s results",
            technology,
            len(hits),
        )

        for domain, source in hits:

            if db.exists(domain):
                continue

            if not db.add(
                domain,
                technology,
                source,
            ):
                continue

            new_count += 1

            first_seen = db.get_first_seen(
                domain
            )

            text = format_hit(
                domain,
                technology,
                first_seen,
                source,
            )

            for target in target_ids:
                await safe_send(
                    target,
                    text,
                )

            await asyncio.sleep(1.0)

    log.info(
        "scan complete — %s new domains",
        new_count,
    )

    return new_count


# ============================================================
# START
# ============================================================

@app.on_message(
    filters.private & filters.command("start")
)
async def cmd_start(
    _,
    message: Message,
):

    log.info(
        "START received | user=%s | chat=%s",
        message.from_user.id
        if message.from_user
        else "unknown",
        message.chat.id,
    )

    await message.reply_text(
        "✅ **Site Hunter is online.**\n\n"
        "🔎 Shopify + Stripe discovery is active.\n\n"
        "**Commands**\n"
        "/scan — start a scan\n"
        "/stats — statistics\n"
        "/latest — latest sites\n"
        "/search `<query>` — search database",
        parse_mode=ParseMode.MARKDOWN,
    )


# ============================================================
# GROUP MESSAGE LOGGER
# ============================================================

@app.on_message(
    filters.group
)
async def group_activity(
    _,
    message: Message,
):

    log.info(
        "GROUP UPDATE | title=%s | chat_id=%s",
        message.chat.title,
        message.chat.id,
    )


# ============================================================
# STATS
# ============================================================

@app.on_message(
    filters.command("stats")
)
async def cmd_stats(
    _,
    message: Message,
):

    stats = db.stats()

    tech = (
        "\n".join(
            f"• {k.capitalize()}: {v}"
            for k, v in stats["by_tech"].items()
        )
        or "• none"
    )

    source = (
        "\n".join(
            f"• {k}: {v}"
            for k, v in stats["by_source"].items()
        )
        or "• none"
    )

    await message.reply_text(
        f"📊 **Stats**\n\n"
        f"Total: **{stats['total']}**\n"
        f"Last 24h: **{stats['last24']}**\n\n"
        f"**Technology**\n{tech}\n\n"
        f"**Source**\n{source}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ============================================================
# LATEST
# ============================================================

@app.on_message(
    filters.command("latest")
)
async def cmd_latest(
    _,
    message: Message,
):

    rows = db.latest(
        hours=24,
        limit=40,
    )

    if not rows:
        await message.reply_text(
            "No sites found in the last 24 hours."
        )
        return

    lines = [
        f"🕒 **Last 24 hours ({len(rows)})**\n"
    ]

    for row in rows:
        lines.append(
            f"• `{row['domain']}` — "
            f"{row['technology'].capitalize()} · "
            f"{row['source']}"
        )

    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


# ============================================================
# SEARCH
# ============================================================

@app.on_message(
    filters.command("search")
)
async def cmd_search(
    _,
    message: Message,
):

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:
        await message.reply_text(
            "Usage:\n"
            "/search shopify\n"
            "/search stripe\n"
            "/search example.com"
        )
        return

    query = parts[1].strip()

    rows = db.search(query)

    if not rows:
        await message.reply_text(
            f"No stored results for `{query}`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = [
        f"🔎 **Results for `{query}`**\n"
    ]

    for row in rows:
        lines.append(
            f"• `{row['domain']}` — "
            f"{row['technology'].capitalize()} · "
            f"{row['source']} · "
            f"{row['first_seen'][:10]}"
        )

    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


# ============================================================
# SCAN COMMAND
# ============================================================

@app.on_message(
    filters.command("scan")
)
async def cmd_scan(
    _,
    message: Message,
):

    # Immediate confirmation
    status = await message.reply_text(
        "🚀 **Scan started!**\n\n"
        "🔎 Checking Shopify + Stripe...\n"
        "⏳ Please wait...",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:

        count = await scan_and_forward(
            notify_chat=message.chat.id
        )

        await status.edit_text(
            "✅ **Scan finished!**\n\n"
            f"🆕 New sites: **{count}**",
            parse_mode=ParseMode.MARKDOWN,
        )

    except Exception as exc:

        log.exception(
            "Manual scan failed"
        )

        await status.edit_text(
            "❌ **Scan failed.**\n\n"
            "Check Railway logs for details.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    missing = config.validate()

    if missing:
        raise SystemExit(
            "Missing required environment variables: "
            + ", ".join(missing)
        )

    await app.start()

    me = await app.get_me()

    log.info(
        "Bot started as @%s",
        me.username,
    )

    # Resolve target immediately.
    target = await resolve_target_chat()

    if target is None and config.TARGET_CHANNEL:
        log.warning(
            "Configured TARGET_CHANNEL could not be resolved."
        )

    log.info(
        "Telegram update handlers are active."
    )

    # Background scanner
    asyncio.create_task(
        auto_loop()
    )

    # Keep process alive
    await asyncio.Event().wait()


# ============================================================
# AUTO LOOP
# ============================================================

async def auto_loop():

    while True:

        try:

            await scan_and_forward()

        except Exception:

            log.exception(
                "Automatic scan cycle failed"
            )

        await asyncio.sleep(
            config.SCAN_INTERVAL
        )


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())
