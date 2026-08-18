"""
Shopify / Stripe Site Hunter Telegram Bot.

Sources:
    1. BuiltWith Change API
    2. Certificate Transparency via crt.sh

The bot:
    - discovers Shopify / Stripe related domains
    - removes duplicates
    - stores results in SQLite
    - forwards new domains to Telegram
    - supports manual scans and searches
"""


import asyncio
import logging
import re
import sqlite3
import time

from datetime import (
    datetime,
    timedelta,
    timezone,
)

from urllib.parse import urlparse


from pyrogram import (
    Client,
    filters,
)

from pyrogram.enums import ParseMode

from pyrogram.types import Message


import config

from ratelimit import (
    RateLimiter,
    RateLimitError,
    request_with_backoff,
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger(
    "sitehunter"
)


# ============================================================
# RATE LIMITERS
# ============================================================

BUILTWITH_LIMITER = RateLimiter(
    "BuiltWith",
    config.BUILTWITH_RPM,
)

CRTSH_LIMITER = RateLimiter(
    "crt.sh",
    config.CRTSH_RPM,
)


# ============================================================
# DOMAIN VALIDATION
# ============================================================

DOMAIN_RE = re.compile(
    r"^(?:"
    r"[a-z0-9]"
    r"(?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"\.)+"
    r"[a-z]{2,}$"
)


# These are platforms / large services.
# We don't want their own domains in our results.
#
# IMPORTANT:
# myshopify.com is intentionally NOT here.
# We want to allow:
# example.myshopify.com
#
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


def clean_domain(
    value: str,
) -> str | None:

    if not value:
        return None

    value = value.strip().lower()

    # Remove protocol
    if "://" in value:
        value = (
            urlparse(value).netloc
            or value
        )

    # Remove path/query/port
    value = (
        value
        .split("/")[0]
        .split("?")[0]
        .split(":")[0]
    )

    # Remove www
    if value.startswith("www."):
        value = value[4:]

    # Remove wildcard
    if value.startswith("*."):
        value = value[2:]

    # Validate domain
    if not DOMAIN_RE.match(value):
        return None

    # Skip unwanted platforms
    if any(
        value == host
        or value.endswith("." + host)
        for host in SKIP_HOSTS
    ):
        return None

    return value


# ============================================================
# DATABASE
# ============================================================

class DB:

    def __init__(
        self,
        path: str,
    ):

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
            CREATE INDEX IF NOT EXISTS
            idx_first_seen
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
                (
                    domain,
                    technology,
                    first_seen,
                    source
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    domain,
                    technology,
                    datetime.now(
                        timezone.utc
                    ).isoformat(
                        timespec="seconds"
                    ),
                    source,
                ),
            )

            self.conn.commit()

            return True

        except sqlite3.IntegrityError:

            return False


    def exists(
        self,
        domain: str,
    ) -> bool:

        result = self.conn.execute(
            """
            SELECT 1
            FROM sites
            WHERE domain = ?
            """,
            (domain,),
        )

        return result.fetchone() is not None


    def stats(self) -> dict:

        total = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM sites
            """
        ).fetchone()[0]


        by_tech = dict(
            self.conn.execute(
                """
                SELECT
                    technology,
                    COUNT(*)
                FROM sites
                GROUP BY technology
                """
            ).fetchall()
        )


        by_source = dict(
            self.conn.execute(
                """
                SELECT
                    source,
                    COUNT(*)
                FROM sites
                GROUP BY source
                """
            ).fetchall()
        )


        cutoff = (
            datetime.now(
                timezone.utc
            )
            - timedelta(days=1)
        ).isoformat(
            timespec="seconds"
        )


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
            datetime.now(
                timezone.utc
            )
            - timedelta(
                hours=hours
            )
        ).isoformat(
            timespec="seconds"
        )


        return self.conn.execute(
            """
            SELECT *
            FROM sites
            WHERE first_seen >= ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (
                cutoff,
                limit,
            ),
        ).fetchall()


    def search(
        self,
        query: str,
        limit: int = 30,
    ) -> list[sqlite3.Row]:

        like = (
            "%"
            + query.lower()
            + "%"
        )


        return self.conn.execute(
            """
            SELECT *
            FROM sites
            WHERE
                lower(domain) LIKE ?
                OR
                lower(technology) LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (
                like,
                like,
                limit,
            ),
        ).fetchall()


db = DB(
    config.DB_PATH
)


# ============================================================
# BUILTWITH
# ============================================================

def fetch_builtwith(
    technology: str,
    since: str | None = None,
) -> list[str]:

    if not config.BUILTWITH_KEY:

        log.info(
            "BuiltWith skipped: "
            "BUILTWITH_KEY not configured"
        )

        return []


    since = (
        since
        or config.BUILTWITH_SINCE
    )


    try:

        response = request_with_backoff(
            BUILTWITH_LIMITER,
            "GET",
            "https://api.builtwith.com/change1/api.json",
            params={
                "KEY": config.BUILTWITH_KEY,
                "LOOKUP": (
                    f"{technology}.com"
                ),
                "SINCE": (
                    since.replace(
                        " ",
                        "+"
                    )
                ),
            },
            timeout=45,
            max_retries=config.MAX_RETRIES,
        )


        data = response.json()


    except (
        RateLimitError,
        Exception,
    ) as exc:

        log.warning(
            "BuiltWith %s failed: %s",
            technology,
            exc,
        )

        return []


    found = []


    def walk(node):

        if isinstance(
            node,
            dict,
        ):

            for key in (
                "Domain",
                "D",
                "domain",
            ):

                value = node.get(
                    key
                )


                if isinstance(
                    value,
                    str,
                ):

                    domain = clean_domain(
                        value
                    )

                    if domain:
                        found.append(
                            domain
                        )


            for value in node.values():
                walk(value)


        elif isinstance(
            node,
            list,
        ):

            for value in node:
                walk(value)


    walk(data)


    result = list(
        dict.fromkeys(
            found
        )
    )


    log.info(
        "BuiltWith %s — %s domains",
        technology,
        len(result),
    )


    return result[
        :config.MAX_PER_SOURCE
    ]


# ============================================================
# CRT.SH
# ============================================================

CRTSH_QUERIES = {

    "shopify": [
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


                for name in (
                    names.splitlines()
                ):

                    domain = clean_domain(
                        name
                    )


                    if domain:

                        found.append(
                            domain
                        )


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
        dict.fromkeys(
            found
        )
    )


    log.info(
        "crt.sh %s — %s domains",
        technology,
        len(result),
    )


    return result[
        :config.MAX_PER_SOURCE
    ]


# ============================================================
# SOURCE RUNNER
# ============================================================

def run_sources(
    technology: str,
) -> list[tuple[str, str]]:

    results = []


    sources = (
        (
            "BuiltWith",
            fetch_builtwith,
        ),
        (
            "crt.sh",
            fetch_crtsh,
        ),
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
                "Source %s crashed: %s",
                source,
                exc,
            )


    return results


# ============================================================
# TELEGRAM CLIENT
# ============================================================

app = Client(
    "sitehunter",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
)


# ============================================================
# TELEGRAM MESSAGE
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

        f"🧩 **Technology:** "
        f"{technology.capitalize()}\n"

        f"📅 **Detected:** "
        f"{first_seen.replace('T', ' ')} UTC\n"

        f"🔎 **Source:** {source}\n"

        f"🔗 https://{domain}"
    )


# ============================================================
# SCAN
# ============================================================

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


    for technology in (
        config.TECHNOLOGIES
    ):

        hits = await asyncio.to_thread(
            run_sources,
            technology,
        )


        for domain, source in hits:

            # Already stored?
            if db.exists(
                domain
            ):
                continue


            # Insert
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
                        parse_mode=(
                            ParseMode.MARKDOWN
                        ),
                        disable_web_page_preview=True,
                    )


                except Exception as exc:

                    log.warning(
                        "Send to %s failed: %s",
                        target,
                        exc,
                    )


            # Avoid Telegram flood
            await asyncio.sleep(
                1.2
            )


    log.info(
        "scan complete — %s new domains",
        new_count,
    )


    return new_count


# ============================================================
# AUTO SCANNER
# ============================================================

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


# ============================================================
# HELP
# ============================================================

HELP = (
    "💻 **Site Hunter Bot**\n\n"

    "Finds Shopify & Stripe related "
    "domains using public discovery sources.\n\n"

    "**Commands**\n"

    "/start — Help\n"

    "/stats — Statistics\n"

    "/search `<query>` — Search database\n"

    "/latest — Last 24 hours\n"

    "/scan — Run scan now\n\n"

    "**Sources**\n"

    "• BuiltWith\n"
    "• crt.sh / Certificate Transparency\n\n"

    "Auto-scan: every "
    f"{max(1, config.SCAN_INTERVAL // 60)} "
    "minutes."
)


# ============================================================
# /START
# ============================================================

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


# ============================================================
# /STATS
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
            f"  • {key.capitalize()}: {value}"

            for key, value
            in stats[
                "by_tech"
            ].items()
        )

        or "  • none yet"
    )


    source = (
        "\n".join(
            f"  • {key}: {value}"

            for key, value
            in stats[
                "by_source"
            ].items()
        )

        or "  • none yet"
    )


    await message.reply_text(
        f"📊 **Stats**\n\n"

        f"Total sites: "
        f"**{stats['total']}**\n"

        f"Last 24 hours: "
        f"**{stats['last24']}**\n\n"

        f"**By technology**\n"
        f"{tech}\n\n"

        f"**By source**\n"
        f"{source}",

        parse_mode=ParseMode.MARKDOWN,
    )


# ============================================================
# /LATEST
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
            "No sites found in the last 24 hours yet."
        )

        return


    lines = [
        f"🕒 **Last 24 hours** "
        f"({len(rows)})\n"
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
# /SEARCH
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
            "Usage:\n\n"
            "`/search shopify`\n"
            "`/search stripe`\n"
            "`/search example.com`",

            parse_mode=ParseMode.MARKDOWN,
        )

        return


    query = parts[1].strip()


    rows = db.search(
        query
    )


    if not rows:

        await message.reply_text(
            f"🔎 No stored results for "
            f"`{query}`.",

            parse_mode=ParseMode.MARKDOWN,
        )

        return


    lines = [
        f"🔎 **Results for** `{query}`\n"
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
# /SCAN
# ============================================================

@app.on_message(
    filters.command("scan")
)
async def cmd_scan(
    _,
    message: Message,
):

    status = await message.reply_text(
        "🚀 Scanning BuiltWith + crt.sh..."
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


    asyncio.create_task(
        auto_loop()
    )


    await asyncio.Event().wait()


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())
