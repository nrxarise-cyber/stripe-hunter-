"""
Shopify / Stripe Site Hunter Telegram Bot (Improved & Robust)
"""

import asyncio
import html
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from pyrogram import Client, filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.errors import FloodWait, RPCError
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
    getattr(config, "BUILTWITH_RPM", 10),
)

# ============================================================
# DOMAIN FILTER
# ============================================================

DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$"
)

SKIP_HOSTS = {
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
}


def clean_domain(value: str) -> str | None:
    if not value or not isinstance(value, str):
        return None

    value = value.strip().lower()

    if "://" in value:
        value = urlparse(value).netloc or value

    # Strip paths, queries, ports
    value = value.split("/")[0].split("?")[0].split(":")[0]

    if value.startswith("www."):
        value = value[4:]
    if value.startswith("*."):
        value = value[2:]

    if not DOMAIN_RE.match(value):
        return None

    if any(value == host or value.endswith("." + host) for host in SKIP_HOSTS):
        return None

    return value


# ============================================================
# THREAD-SAFE DATABASE HANDLER
# ============================================================

class DB:
    def __init__(self, path: str):
        self.path = path
        self.lock = asyncio.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute(
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
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_first_seen
                ON sites(first_seen)
                """
            )
            conn.commit()

    async def add(self, domain: str, technology: str, source: str) -> bool:
        async with self.lock:
            def _query():
                try:
                    with self._get_connection() as conn:
                        conn.execute(
                            """
                            INSERT INTO sites (domain, technology, first_seen, source)
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                domain,
                                technology,
                                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                source,
                            ),
                        )
                        conn.commit()
                        return True
                except sqlite3.IntegrityError:
                    return False

            return await asyncio.to_thread(_query)

    async def exists(self, domain: str) -> bool:
        def _query():
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM sites WHERE domain = ?", (domain,)
                ).fetchone()
                return row is not None

        return await asyncio.to_thread(_query)

    async def get_first_seen(self, domain: str) -> str:
        def _query():
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT first_seen FROM sites WHERE domain = ?", (domain,)
                ).fetchone()
                return row["first_seen"] if row else ""

        return await asyncio.to_thread(_query)

    async def stats(self) -> dict:
        def _query():
            with self._get_connection() as conn:
                total = conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0]

                by_tech = dict(
                    conn.execute(
                        "SELECT technology, COUNT(*) FROM sites GROUP BY technology"
                    ).fetchall()
                )

                by_source = dict(
                    conn.execute(
                        "SELECT source, COUNT(*) FROM sites GROUP BY source"
                    ).fetchall()
                )

                cutoff = (
                    datetime.now(timezone.utc) - timedelta(days=1)
                ).isoformat(timespec="seconds")

                last24 = conn.execute(
                    "SELECT COUNT(*) FROM sites WHERE first_seen >= ?", (cutoff,)
                ).fetchone()[0]

                return {
                    "total": total,
                    "by_tech": by_tech,
                    "by_source": by_source,
                    "last24": last24,
                }

        return await asyncio.to_thread(_query)

    async def latest(self, hours: int = 24, limit: int = 30) -> list[dict]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat(timespec="seconds")

        def _query():
            with self._get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT domain, technology, source, first_seen
                    FROM sites
                    WHERE first_seen >= ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (cutoff, limit),
                ).fetchall()
                return [dict(r) for r in rows]

        return await asyncio.to_thread(_query)

    async def search(self, query: str, limit: int = 25) -> list[dict]:
        like = f"%{query.lower()}%"

        def _query():
            with self._get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT domain, technology, source, first_seen
                    FROM sites
                    WHERE lower(domain) LIKE ? OR lower(technology) LIKE ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (like, like, limit),
                ).fetchall()
                return [dict(r) for r in rows]

        return await asyncio.to_thread(_query)


db = DB(config.DB_PATH)


# ============================================================
# BUILTWITH CLIENT
# ============================================================

def fetch_builtwith(technology: str, since: str | None = None) -> list[str]:
    if not getattr(config, "BUILTWITH_KEY", None):
        log.warning("BUILTWITH_KEY is not configured")
        return []

    since = since or getattr(config, "BUILTWITH_SINCE", "yesterday")

    try:
        response = request_with_backoff(
            BUILTWITH_LIMITER,
            "GET",
            "https://api.builtwith.com/change1/api.json",
            params={
                "KEY": config.BUILTWITH_KEY,
                "TECH": technology,
                "SINCE": since.replace(" ", "+"),
            },
            timeout=45,
            max_retries=getattr(config, "MAX_RETRIES", 3),
        )
        data = response.json()

    except (RateLimitError, Exception) as exc:
        log.warning("BuiltWith %s failed: %s", technology, exc)
        return []

    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key in ("Domain", "D", "domain"):
                val = node.get(key)
                if isinstance(val, str):
                    cleaned = clean_domain(val)
                    if cleaned:
                        found.append(cleaned)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)

    unique_domains = list(dict.fromkeys(found))
    max_domains = getattr(config, "MAX_PER_SOURCE", 100)
    log.info("BuiltWith %s — %d domains", technology, len(unique_domains))
    return unique_domains[:max_domains]


def run_sources(technology: str) -> list[tuple[str, str]]:
    results = []
    try:
        domains = fetch_builtwith(technology)
        for d in domains:
            results.append((d, "BuiltWith"))
    except Exception as exc:
        log.exception("BuiltWith crashed for %s: %s", technology, exc)
    return results


# ============================================================
# TELEGRAM BOT CLIENT
# ============================================================

app = Client(
    "sitehunter",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
)


def escape_markdown(text: str) -> str:
    """Escapes Telegram legacy markdown reserved characters."""
    return re.sub(r"([_*`\[\]])", r"\\\1", text)


async def safe_send(chat_id: int | str, text: str) -> bool:
    try:
        await app.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        return True
    except FloodWait as e:
        log.warning("Telegram FloodWait: sleeping for %d seconds", e.value)
        await asyncio.sleep(e.value + 1)
        try:
            await app.send_message(
                chat_id,
                text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            return True
        except Exception:
            return False
    except RPCError as exc:
        log.error("Telegram RPCError | chat=%s | error=%s", chat_id, exc)
        return False
    except Exception as exc:
        log.error("Telegram unexpected send failed | chat=%s | error=%s", chat_id, exc)
        return False


def format_hit(domain: str, technology: str, first_seen: str, source: str) -> str:
    # Safely escape text values in case they contain markdown syntax
    clean_first_seen = first_seen.replace("T", " ")
    return (
        "🆕 **New site detected**\n\n"
        f"🌐 **Domain:** `{domain}`\n"
        f"🧩 **Technology:** {escape_markdown(technology.capitalize())}\n"
        f"📅 **Detected:** `{clean_first_seen} UTC`\n"
        f"🔎 **Source:** {escape_markdown(source)}\n"
        f"🔗 https://{domain}"
    )


# ============================================================
# SCAN LOGIC
# ============================================================

scan_lock = asyncio.Lock()

async def scan_and_forward(notify_chat: int | str | None = None) -> int:
    if scan_lock.locked():
        log.info("A scan is already in progress. Skipping concurrent run.")
        return 0

    async with scan_lock:
        new_count = 0
        target_ids: list[int | str] = []

        if getattr(config, "TARGET_CHANNEL", None):
            target_ids.append(config.TARGET_CHANNEL)

        if notify_chat and notify_chat not in target_ids:
            target_ids.append(notify_chat)

        log.info("Scan started | targets=%s", target_ids)

        for technology in getattr(config, "TECHNOLOGIES", ["shopify", "stripe"]):
            log.info("Scanning %s...", technology)
            hits = await asyncio.to_thread(run_sources, technology)

            for domain, source in hits:
                if await db.exists(domain):
                    continue

                if not await db.add(domain, technology, source):
                    continue

                new_count += 1
                first_seen = await db.get_first_seen(domain)
                text = format_hit(domain, technology, first_seen, source)

                for target in target_ids:
                    await safe_send(target, text)
                    await asyncio.sleep(0.5)

        log.info("Scan complete — %d new domains", new_count)
        return new_count


# ============================================================
# TELEGRAM COMMAND HANDLERS
# ============================================================

@app.on_message(filters.private & filters.command("start"))
async def cmd_start(_, message: Message):
    await message.reply_text(
        "✅ **Site Hunter is online.**\n\n"
        "🔎 Active technology discovery.\n\n"
        "**Commands**\n"
        "• `/scan` — Trigger scan immediately\n"
        "• `/stats` — View stored database stats\n"
        "• `/latest` — View recent discoveries\n"
        "• `/search <query>` — Search stored domains",
        parse_mode=ParseMode.MARKDOWN,
    )


@app.on_message(filters.command("stats"))
async def cmd_stats(_, message: Message):
    stats = await db.stats()

    tech = "\n".join(
        f"• {escape_markdown(k.capitalize())}: **{v}**" for k, v in stats["by_tech"].items()
    ) or "• None"

    source = "\n".join(
        f"• {escape_markdown(k)}: **{v}**" for k, v in stats["by_source"].items()
    ) or "• None"

    await message.reply_text(
        f"📊 **Database Statistics**\n\n"
        f"Total Sites: **{stats['total']}**\n"
        f"Added in last 24h: **{stats['last24']}**\n\n"
        f"**By Technology:**\n{tech}\n\n"
        f"**By Source:**\n{source}",
        parse_mode=ParseMode.MARKDOWN,
    )


@app.on_message(filters.command("latest"))
async def cmd_latest(_, message: Message):
    rows = await db.latest(hours=24, limit=25)
    if not rows:
        await message.reply_text("No sites discovered in the last 24 hours.")
        return

    lines = [f"🕒 **Last 24 Hours ({len(rows)} sites)**\n"]
    for row in rows:
        lines.append(
            f"• `{row['domain']}` — {escape_markdown(row['technology'].capitalize())} ({escape_markdown(row['source'])})"
        )

    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


@app.on_message(filters.command("search"))
async def cmd_search(_, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text(
            "**Usage:** `/search <domain or technology>`\n*Example:* `/search shopify`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    query = parts[1].strip()
    rows = await db.search(query, limit=25)

    if not rows:
        await message.reply_text(
            f"No records found matching `{escape_markdown(query)}`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = [f"🔎 **Results for `{escape_markdown(query)}` ({len(rows)})**\n"]
    for row in rows:
        first_date = row["first_seen"][:10] if row.get("first_seen") else "Unknown"
        lines.append(
            f"• `{row['domain']}` — {escape_markdown(row['technology'].capitalize())} (`{first_date}`)"
        )

    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


@app.on_message(filters.command("scan"))
async def cmd_scan(_, message: Message):
    if scan_lock.locked():
        await message.reply_text("⚠️ A scan is already running. Please wait for it to finish.")
        return

    status = await message.reply_text(
        "🚀 **Scan initiated!**\n⏳ Searching configured sources...",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        count = await scan_and_forward(notify_chat=message.chat.id)
        await status.edit_text(
            f"✅ **Scan finished!**\n🆕 New sites added: **{count}**",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        log.exception("Manual scan error: %s", exc)
        await status.edit_text(
            "❌ **Scan failed.** Check application server logs.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ============================================================
# RUNNER & BACKGROUND TASKS
# ============================================================

background_tasks = set()

async def auto_loop():
    interval = getattr(config, "SCAN_INTERVAL", 3600)
    while True:
        try:
            await scan_and_forward()
        except Exception:
            log.exception("Automatic periodic scan failed")
        await asyncio.sleep(interval)


async def main():
    if hasattr(config, "validate"):
        missing = config.validate()
        if missing:
            raise SystemExit("Missing configuration keys: " + ", ".join(missing))

    await app.start()
    me = await app.get_me()
    log.info("Bot started successfully as @%s", me.username)

    # Maintain a strong reference to prevent GC
    loop_task = asyncio.create_task(auto_loop())
    background_tasks.add(loop_task)
    loop_task.add_done_callback(background_tasks.discard)

    # Keep alive
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
