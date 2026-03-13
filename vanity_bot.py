"""
vanity_bot.py — Vanity Ethereum Address Generator Telegram Bot
==============================================================
Uses threading instead of multiprocessing — works on Railway and all
cloud platforms. Each search runs worker threads that share memory
directly, making progress tracking fast and reliable.
"""

import asyncio
import logging
import os
import re
import secrets
import threading
import time

from dotenv import load_dotenv
from eth_account import Account
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Environment ───────────────────────────────────────────────────────────────
load_dotenv()

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit(
        "\n❌ ERROR: TELEGRAM_BOT_TOKEN is not set.\n"
        "  • Local: add it to your .env file\n"
        "  • Railway: add it in the Variables tab\n"
    )

# ── Config ────────────────────────────────────────────────────────────────────
NUM_WORKERS   = max(2, (os.cpu_count() or 2))   # threads — more is fine
POLL_INTERVAL = 1.5     # seconds between asyncio queue checks
EDIT_INTERVAL = 3.0     # seconds between Telegram message edits
EXTRACT_CHARS = 4       # chars to auto-extract from pasted address

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# chat_id → job dict
active_jobs: dict[int, dict] = {}


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def esc(text: str) -> str:
    """Escape string for Telegram MarkdownV2."""
    specials = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in specials else c for c in text)


def extract_patterns(address: str) -> tuple[str, str]:
    """Extract first/last EXTRACT_CHARS hex chars from an ETH address."""
    body = address[2:].lower()
    return body[:EXTRACT_CHARS], body[-EXTRACT_CHARS:]


def estimate_attempts(n: int) -> str:
    val = 16 ** n
    if val >= 1_000_000_000:
        return f"{val / 1_000_000_000:.1f}B"
    if val >= 1_000_000:
        return f"{val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"{val / 1_000:.1f}K"
    return str(val)


def kill_job(chat_id: int) -> None:
    """Stop all threads and cancel the asyncio task for a chat."""
    job = active_jobs.pop(chat_id, None)
    if not job:
        return
    # Signal threads to stop
    job["stop_event"].set()
    # Cancel asyncio polling task
    task = job.get("task")
    if task and not task.done():
        task.cancel()


# ══════════════════════════════════════════════════════════════════════════════
#  WORKER THREAD  (runs in background thread, not a separate process)
# ══════════════════════════════════════════════════════════════════════════════

def search_worker(
    worker_id: int,
    pattern: str,
    mode: str,
    stop_event: threading.Event,
    result: dict,           # shared dict — workers write result here
    counters: list,         # counters[worker_id] = attempt count
) -> None:
    """
    Thread worker. Generates random private keys, derives ETH addresses,
    checks for match. Writes result to shared dict and sets stop_event on find.

    HOW ETH ADDRESS IS DERIVED:
    1. secrets.token_bytes(32)  → secure random 32-byte private key
    2. Account.from_key()       → secp256k1 curve + Keccak-256 hash internally
    3. account.address          → last 20 bytes of hash, prefixed with 0x
    """
    attempts = 0

    while not stop_event.is_set():
        # Generate secure random private key
        private_key_hex = "0x" + secrets.token_bytes(32).hex()

        # Derive Ethereum address
        account = Account.from_key(private_key_hex)
        address = account.address

        attempts += 1
        counters[worker_id] = attempts   # update shared counter

        # Check match
        body = address[2:].lower()
        matched = (
            body.startswith(pattern) if mode == "prefix"
            else body.endswith(pattern)
        )

        if matched:
            # Write result and signal all threads to stop
            result["address"]     = address
            result["private_key"] = private_key_hex
            stop_event.set()
            return


# ══════════════════════════════════════════════════════════════════════════════
#  BACKGROUND ASYNCIO POLLING TASK
# ══════════════════════════════════════════════════════════════════════════════

async def poll_results(
    chat_id: int,
    status_message,
    pattern: str,
    mode: str,
    stop_event: threading.Event,
    result: dict,
    counters: list,
    start_time: float,
) -> None:
    """
    Asyncio task that monitors worker threads and updates the Telegram message.
    Runs until a match is found or cancelled.
    """
    last_edit = time.monotonic()
    display   = f"0x{pattern}..." if mode == "prefix" else f"0x...{pattern}"

    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)

            total   = sum(counters)
            elapsed = time.monotonic() - start_time
            rate    = int(total / elapsed) if elapsed > 0 else 0

            # ── Match found ───────────────────────────────────────────────────
            if stop_event.is_set() and result.get("address"):
                address     = result["address"]
                private_key = result["private_key"]
                kill_job(chat_id)

                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Copy Address",      callback_data=f"copy:{address}")],
                    [InlineKeyboardButton("📋 Copy Private Key",  callback_data=f"copy:{private_key}")],
                    [InlineKeyboardButton("🔗 View on Etherscan", url=f"https://etherscan.io/address/{address}")],
                ])

                await status_message.edit_text(
                    "✅ *Vanity Address Found\\!*\n\n"
                    f"🎯 *Pattern:* `{esc(display)}`\n\n"
                    f"📬 *Address:*\n`{address}`\n\n"
                    f"🔑 *Private Key:*\n`{private_key}`\n\n"
                    "─────────────────────────\n"
                    f"🔁 *Attempts:* {esc(f'{total:,}')}\n"
                    f"⏱ *Time:* {esc(f'{elapsed:.1f}s')}\n"
                    f"⚡ *Speed:* {esc(f'{rate:,}')} addr/s\n\n"
                    "⚠️ _Keep your private key secret\\. Never share it\\._",
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=keyboard,
                )
                return

            # ── Periodic progress update ──────────────────────────────────────
            now = time.monotonic()
            if now - last_edit >= EDIT_INTERVAL:
                last_edit = now
                refresh_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh Progress", callback_data=f"refresh:{chat_id}")],
                    [InlineKeyboardButton("🛑 Cancel Search",    callback_data=f"canceljob:{chat_id}")],
                ])
                try:
                    await status_message.edit_text(
                        "🔄 *Searching for vanity address…*\n\n"
                        f"🎯 *Pattern:* `{esc(display)}`\n\n"
                        f"🔁 Attempts: *{esc(f'{total:,}')}*\n"
                        f"⚡ Speed: *{esc(f'{rate:,}')}* addr/s\n"
                        f"⏱ Elapsed: *{esc(f'{elapsed:.0f}s')}*\n\n"
                        "_Tap Refresh to update stats\\._",
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_markup=refresh_kb,
                    )
                except Exception:
                    pass

    except asyncio.CancelledError:
        logger.info("Poll task cancelled for chat %s", chat_id)
        raise


# ══════════════════════════════════════════════════════════════════════════════
#  SEARCH LAUNCHER
# ══════════════════════════════════════════════════════════════════════════════

async def launch_search(chat_id: int, pattern: str, mode: str, reply_fn) -> None:
    """Kill any existing job, spawn worker threads, start polling task."""
    if chat_id in active_jobs:
        kill_job(chat_id)
        await asyncio.sleep(0.2)

    display  = f"0x{pattern}..." if mode == "prefix" else f"0x...{pattern}"
    expected = estimate_attempts(len(pattern))

    status_msg = await reply_fn(
        "🔄 *Starting vanity search…*\n\n"
        f"🎯 *Pattern:* `{esc(display)}`\n"
        f"📊 *Expected:* \\~{esc(expected)} attempts\n"
        f"⚙️ *Workers:* {esc(str(NUM_WORKERS))}\n\n"
        "_Spinning up workers\\.\\.\\._"
    )

    stop_event = threading.Event()
    result     = {}                          # shared dict for found address
    counters   = [0] * NUM_WORKERS           # per-thread attempt counter
    start_time = time.monotonic()

    # Spawn worker threads
    threads = []
    for wid in range(NUM_WORKERS):
        t = threading.Thread(
            target=search_worker,
            args=(wid, pattern, mode, stop_event, result, counters),
            daemon=True,
        )
        t.start()
        threads.append(t)

    logger.info("Chat %s | %d threads | %s %s", chat_id, NUM_WORKERS, mode, pattern)

    task = asyncio.create_task(
        poll_results(
            chat_id=chat_id,
            status_message=status_msg,
            pattern=pattern,
            mode=mode,
            stop_event=stop_event,
            result=result,
            counters=counters,
            start_time=start_time,
        )
    )

    active_jobs[chat_id] = {
        "threads":    threads,
        "stop_event": stop_event,
        "task":       task,
        "counters":   counters,
        "start_time": start_time,
        "pattern":    pattern,
        "mode":       mode,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  COMMAND & MESSAGE HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔐 Vanity Ethereum Address Generator\n\n"
        "Simply paste any Ethereum address and I will extract the prefix "
        "and suffix automatically for you to choose.\n\n"
        "How to use:\n"
        "1. Paste an Ethereum address\n"
        "2. I extract the first and last 4 characters\n"
        "3. Tap Match Prefix or Match Suffix to start\n\n"
        "Difficulty Guide:\n"
        "4 chars = ~65K attempts (seconds)\n"
        "5 chars = ~1M attempts (1-2 min)\n"
        "6 chars = ~16M attempts (30-60 min)\n\n"
        "Each extra character is 16x harder.\n\n"
        "Use /cancel to stop any active search."
    )


async def handle_address_message(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """User pastes ETH address — extract prefix/suffix and show choice buttons."""
    text = update.message.text.strip()

    if not ETH_ADDRESS_RE.match(text):
        await update.message.reply_text(
            "⚠️ That doesn't look like a valid Ethereum address\\.\n\n"
            "Please paste a full 42\\-character address starting with `0x`\\.\n\n"
            "_Example:_\n"
            "`0xDeaD1234567890AbCd1234567890abcdEF123456`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    prefix, suffix = extract_patterns(text)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🔵 Match Prefix → 0x{prefix}…",
            callback_data=f"search:prefix:{prefix}",
        )],
        [InlineKeyboardButton(
            f"🟣 Match Suffix → 0x…{suffix}",
            callback_data=f"search:suffix:{suffix}",
        )],
    ])

    await update.message.reply_text(
        "✅ *Address received\\!*\n\n"
        f"`{esc(text)}`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "I extracted these patterns:\n\n"
        f"🔵 *Prefix:* `0x{esc(prefix)}…`\n"
        f"🟣 *Suffix:* `0x…{esc(suffix)}`\n\n"
        "👇 *Which would you like to search for?*",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
    )


async def handle_search_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Match Prefix / Match Suffix button taps."""
    query   = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()

    parts   = query.data.split(":", 2)
    mode    = parts[1]
    pattern = parts[2]

    async def send_status(text: str):
        return await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

    await launch_search(chat_id, pattern, mode, send_status)


async def handle_refresh_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Refresh Progress button — pull latest stats instantly."""
    query   = update.callback_query
    chat_id = int(query.data.split(":")[1])
    await query.answer("Refreshed!")

    job = active_jobs.get(chat_id)
    if not job:
        await query.message.reply_text("No active search. It may have already completed.")
        return

    total   = sum(job["counters"])
    elapsed = time.monotonic() - job["start_time"]
    rate    = int(total / elapsed) if elapsed > 0 else 0
    pattern = job["pattern"]
    mode    = job["mode"]
    display = f"0x{pattern}..." if mode == "prefix" else f"0x...{pattern}"

    refresh_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Progress", callback_data=f"refresh:{chat_id}")],
        [InlineKeyboardButton("🛑 Cancel Search",    callback_data=f"canceljob:{chat_id}")],
    ])

    try:
        await query.message.edit_text(
            "🔄 *Searching for vanity address…*\n\n"
            f"🎯 *Pattern:* `{esc(display)}`\n\n"
            f"🔁 Attempts: *{esc(f'{total:,}')}*\n"
            f"⚡ Speed: *{esc(f'{rate:,}')}* addr/s\n"
            f"⏱ Elapsed: *{esc(f'{elapsed:.0f}s')}*\n\n"
            "_Tap Refresh to update stats\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=refresh_kb,
        )
    except Exception:
        pass


async def handle_cancelJob_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Cancel Search inline button."""
    query   = update.callback_query
    chat_id = int(query.data.split(":")[1])
    await query.answer("Cancelled!")

    if chat_id in active_jobs:
        kill_job(chat_id)

    try:
        await query.message.edit_text(
            "🛑 *Search cancelled\\.*\n\n"
            "_Paste an Ethereum address to start a new search\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception:
        pass


async def handle_copy_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Copy Address / Copy Private Key button taps."""
    query = update.callback_query
    await query.answer()

    if query.data and query.data.startswith("copy:"):
        value = query.data.split("copy:", 1)[1]
        label = "address" if len(value) == 42 else "private key"
        await query.message.reply_text(
            f"📋 *Tap and hold to copy {esc(label)}:*\n\n`{value}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def cmd_cancel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in active_jobs:
        await update.message.reply_text(
            "No active search to cancel.\n\nPaste an Ethereum address to start."
        )
        return
    kill_job(chat_id)
    await update.message.reply_text(
        "🛑 Search cancelled.\n\nPaste an Ethereum address to start a new search."
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    logger.info("Starting Vanity ETH Bot | %d threads per search", NUM_WORKERS)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_search_callback,    pattern=r"^search:"))
    app.add_handler(CallbackQueryHandler(handle_copy_callback,      pattern=r"^copy:"))
    app.add_handler(CallbackQueryHandler(handle_refresh_callback,   pattern=r"^refresh:"))
    app.add_handler(CallbackQueryHandler(handle_cancelJob_callback, pattern=r"^canceljob:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address_message))

    async def error_handler(update, context):
        logger.error("Unhandled exception: %s", context.error, exc_info=context.error)

    app.add_error_handler(error_handler)

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
