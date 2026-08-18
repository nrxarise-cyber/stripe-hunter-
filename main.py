"""
Shopify / Stripe site-hunter Telegram bot.

Sources:
- BuiltWith Change API
- Google Programmable Search
- Certificate Transparency via crt.sh

Commands:
    /start
    /stats
    /search <query>
    /latest
    /scan
"""

import asyncio
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

import config
from ratelimit import (
    RateLimiter,
    RateLimitError,
    request_with_backoff,
)


# --------------------------------------------------------------------------
# Rate limiters
# --------------------------------------------------------------------------

BUILTWITH_LIMITER = RateLimiter(
    "BuiltWith",
    config.BUILTWITH_RPM,
)

GOOGLE_LIMITER = RateLimiter(
    "Google",
    config.GOOGLE_RPM,
)

CRTSH_LIMITER = RateLimiter(
    "crt.sh",
    config.CRTSH_RPM,
)


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("sitehunter")


# --------------------------------------------------------------------------
# Domain helpers
# --------------------------------------------------------------------------

DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$"
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
    "myshopify.com",
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


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

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
        cur = self.conn.execute(
            "SELECT 1 FROM sites WHERE domain = ?",
            (domain,),
        )

        return cur.fetchone() is not None

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
            WHERE lower(domain) LIKE ?
               OR lower(technology) LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (like, like, limit),
        ).fetchall()


db = DB(config.DB_PATH)


# --------------------------------------------------------------------------
# BuiltWith
# --------------------------------------------------------------------------

def fetch_builtwith(
    technology: str,
    since: str | None = None,
) -> list[str]:

    if not config.BUILTWITH_KEY:
        log.info(
            "BuiltWith skipped: BUILTWITH_KEY not configured"
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

    found = []

    def walk(node):

        if isinstance(node, dict):

            for key in (
                "Domain",
                "D",
                "domain",
            ):

                if isinstance(
                    node.get(key),
                    str,
                ):

                    domain = clean_domain(
                        node[key]
                    )

                    if domain:
                        found.append(domain)

            for value in node.values():
                walk(value)

        elif isinstance(node, list):

            for value in node:
                walk(value)

    walk(data)

    result = list(
        dict.fromkeys(found)
    )

    log.info(
        "BuiltWith %s — %s domains",
        technology,
        len(result),
    )

    return result[:config.MAX_PER_SOURCE]


# --------------------------------------------------------------------------
# Google
# --------------------------------------------------------------------------

GOOGLE_DORKS = {
    "shopify": [
        '"powered by shopify" -site:shopify.com',
        'inurl:"/collections/all" "add to cart"',
    ],
    "stripe": [
        '"checkout.stripe.com"',
        '"powered by stripe" inurl:checkout',
    ],
}


def fetch_google(
    technology: str,
    custom_query: str | None = None,
) -> list[str]:

    if not (
        config.GOOGLE_API_KEY
        and config.GOOGLE_CX
    ):
        log.info(
            "Google skipped: API credentials not configured"
        )
        return []

    queries = (
        [custom_query]
        if custom_query
        else GOOGLE_DORKS.get(
            technology,
            [],
        )
    )

    found = []

    for query in queries:

        try:

            response = request_with_backoff(
                GOOGLE_LIMITER,
                "GET",
                "https://www.googleapis.com/customsearch/v1",
                params={
                    "key": config.GOOGLE_API_KEY,
                    "cx": config.GOOGLE_CX,
                    "q": query,
                    "num": 10,
                    "dateRestrict": "w1",
                },
                timeout=30,
                max_retries=config.MAX_RETRIES,
            )

            for item in response.json().get(
                "items",
                [],
            ):

                domain = clean_domain(
                    item.get("link", "")
                )

                if domain:
                    found.append(domain)

        except (RateLimitError, Exception) as exc:

            log.warning(
                "Google dork failed (%s): %s",
                query,
                exc,
            )

        time.sleep(1)

    result = list(
        dict.fromkeys(found)
    )

    log.info(
        "Google %s — %s domains",
        technology,
        len(result),
    )

    return result[:config.MAX_PER_SOURCE]


# --------------------------------------------------------------------------
# crt.sh / Certificate Transparency
# --------------------------------------------------------------------------

CRTSH_QUERIES = {
    "shopify": [
        "%.shopify.com",
        "%.myshopify.com",
    ],
    "stripe": [
        "%.stripe.com",
    ],
}


def fetch_crtsh(
    technology: str,
    custom_query: str | None = None,
) -> list[str]:

    queries = (
        [custom_query]
        if custom_query
        else CRTSH_QUERIES.get(
            technology,
            [],
        )
    )

    if not queries:
        return []

    found = []

    for query in queries:

        try:

            response = request_with_backoff(
                CRTSH_LIMITER,
                "GET",
                "https://crt.sh/",
                params={
                    "q": query,
                    "output": "json",
                },
                timeout=45,
                max_retries=config.MAX_RETRIES,
            )

            data = response.json()

            if not isinstance(
                data,
                list,
            ):
                continue

            for certificate in data:

                if not isinstance(
                    certificate,
                    dict,
                ):
                    continue

                names = certificate.get(
                    "name_value",
                    "",
                )

                if not isinstance(
                    names,
                    str,
                ):
                    continue

                for name in names.splitlines():

                    domain = clean_domain(
                        name
                    )

                    if domain:
                        found.append(domain)

        except (
            RateLimitError,
            Exception,
        ) as exc:

            log.warning(
                "crt.sh %s failed: %s",
                technology,
                exc,
            )

        time.sleep(1)

    result = list(
        dict.fromkeys(found)
    )

    log.info(
        "crt.sh %s — %s domains",
        technology,
        len(result),
    )

    return result[:config.MAX_PER_SOURCE]


# --------------------------------------------------------------------------
# Source runner
# --------------------------------------------------------------------------

def run_sources(
    technology: str,
) -> list[tuple[str, str]]:

    results = []

    sources = (
        ("BuiltWith", fetch_builtwith),
        ("Google", fetch_google),
        ("crt.sh", fetch_crtsh),
    )

    for source, function in sources:

        try:

            domains = function(
                technology
            )

            for domain in domains:
                results.append(
                    (
                        domain,
                        source,
                    )
                )

        except Exception as exc:

            log.exception(
                "source %s crashed: %s",
                source,
                exc,
            )

    return results


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

app = Client(
    "sitehunter",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
)


def format_hit(
    domain: str,
    technology: str,
    first_seen: str,
    source: str,
) -> str:

    return (
        "🆕 **New site found**\n\n"
        f"🌐 **Domain:** `{domain}`\n"
        f"🧩 **Technology:** "
        f"{technology.capitalize()}\n"
        f"📅 **First seen:** "
        f"{first_seen.replace('T', ' ')} UTC\n"
        f"🔎 **Source:** {source}\n"
        f"🔗 https://{domain}"
    )


async def scan_and_forward(
    notify_chat: int | str | None = None,
) -> int:

    new_count = 0

    targets = [
        target
        for target in (
            config.TARGET_CHANNEL,
            notify_chat,
        )
        if target
    ]

    for technology in config.TECHNOLOGIES:

        hits = await asyncio.to_thread(
            run_sources,
            technology,
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

            row = db.conn.execute(
                """
                SELECT first_seen
                FROM sites
                WHERE domain = ?
                """,
                (domain,),
            ).fetchone()

            text = format_hit(
                domain,
                technology,
                row["first_seen"],
                source,
            )

            for target in targets:

                try:

                    await app.send_message(
                        target,
                        text,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=True,
                    )

                except Exception as exc:

                    log.warning(
                        "send to %s failed: %s",
                        target,
                        exc,
                    )

            await asyncio.sleep(1.2)

    log.info(
        "scan complete — %s new domains",
        new_count,
    )

    return new_count


async def auto_loop():

    while True:

        try:
            await scan_and_forward()

        except Exception as exc:

            log.exception(
                "scan cycle failed: %s",
                exc,
            )

        await asyncio.sleep(
            config.SCAN_INTERVAL
        )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

HELP = (
    "💻 **Site Hunter Bot**\n"
    "Finds Shopify & Stripe related sites "
    "and forwards new domains to your channel.\n\n"

    "**Commands**\n"
    "/start — this help menu\n"
    "/stats — total sites found\n"
    "/search `<query>` — search stored sites "
    "or run a live lookup\n"
    "/latest — sites found in the last 24 hours\n"
    "/scan — trigger a scan right now\n\n"

    "**Sources:** "
    "BuiltWith · Google Dorks · Certificate Transparency\n"

    "Auto-scan runs every "
    f"{max(1, config.SCAN_INTERVAL // 60)} minutes, 24/7."
)


@app.on_message(
    filters.command("start")
)
async def cmd_start(
    _,
    message: Message,
):

    await message.reply_text(
        HELP,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


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
            f"  • {key.capitalize()}: {value}"
            for key, value in stats["by_tech"].items()
        )
        or "  • none yet"
    )

    source = (
        "\n".join(
            f"  • {key}: {value}"
            for key, value in stats["by_source"].items()
        )
        or "  • none yet"
    )

    await message.reply_text(
        f"📊 **Stats**\n\n"
        f"Total sites: **{stats['total']}**\n"
        f"Last 24 hours: **{stats['last24']}**\n\n"
        f"**By technology**\n{tech}\n\n"
        f"**By source**\n{source}",
        parse_mode=ParseMode.MARKDOWN,
    )


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
            "No sites found in the last 24 hours yet."
        )

        return

    lines = [
        f"🕒 **Last 24 hours** ({len(rows)})\n"
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
            "Usage: `/search shopify` "
            "or `/search mystore.com`",
            parse_mode=ParseMode.MARKDOWN,
        )

        return

    query = parts[1].strip()

    rows = db.search(query)

    if rows:

        lines = [
            f"🔎 **Stored results for** "
            f"`{query}`\n"
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

        return

    status = await message.reply_text(
        f"🔎 No stored match. "
        f"Running a live search for `{query}`…",
        parse_mode=ParseMode.MARKDOWN,
    )

    tech = (
        "stripe"
        if "stripe" in query.lower()
        else "shopify"
    )

    live = await asyncio.to_thread(
        fetch_google,
        tech,
        query,
    )

    live += await asyncio.to_thread(
        fetch_crtsh,
        tech,
        query,
    )

    live = list(
        dict.fromkeys(live)
    )[:25]

    if not live:

        await status.edit_text(
            "Nothing found. "
            "Check your Google configuration "
            "or try another query."
        )

        return

    added = [
        domain
        for domain in live
        if db.add(
            domain,
            tech,
            "Manual",
        )
    ]

    lines = [
        f"🔎 **Live results for** "
        f"`{query}` "
        f"({len(live)} found, "
        f"{len(added)} new)\n"
    ]

    lines += [
        f"• `{domain}`"
        for domain in live
    ]

    await status.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


@app.on_message(
    filters.command("scan")
)
async def cmd_scan(
    _,
    message: Message,
):

    status = await message.reply_text(
        "🚀 Scanning all sources…"
    )

    count = await scan_and_forward(
        notify_chat=(
            message.chat.id
            if not config.TARGET_CHANNEL
            else None
        )
    )

    await status.edit_text(
        f"✅ Scan finished — "
        f"**{count}** new sites.",
        parse_mode=ParseMode.MARKDOWN,
    )


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------

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

    asyncio.create_task(
        auto_loop()
    )

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
