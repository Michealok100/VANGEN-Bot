"""
vanity_bot.py — Vanity Ethereum Address Generator Telegram Bot
==============================================================
Fast address generation using coincurve (secp256k1) + pycryptodome (Keccak-256).
This bypasses the slow eth_account wrapper and generates addresses at full speed.

HOW ETH ADDRESS DERIVATION WORKS:
  1. secrets.token_bytes(32)        → secure random 32-byte private key
  2. coincurve.PrivateKey(bytes)     → secp256k1 public key (fast C library)
  3. public_key.format(compressed=False)[1:]  → 64-byte uncompressed pubkey
  4. Keccak-256(pubkey)             → 32-byte hash (pycryptodome, fast C)
  5. last 20 bytes → "0x" prefix   → Ethereum address
"""

import asyncio
import logging
import os
import re
import secrets
import threading
import time

import coincurve
from Crypto.Hash import keccak as _keccak
from dotenv import load_dotenv
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
NUM_WORKERS   = max(2, (os.cpu_count() or 2))
POLL_INTERVAL = 1.5
EDIT_INTERVAL = 3.0
EXTRACT_CHARS = 4

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

active_jobs: dict[int, dict] = {}


# ══════════════════════════════════════════════════════════════════════════════
#  FAST ADDRESS DERIVATION  (coincurve + pycryptodome)
# ══════════════════════════════════════════════════════════════════════════════

def _keccak256(data: bytes) -> bytes:
    """Fast Keccak-256 via pycryptodome C extension."""
    k = _keccak.new(digest_bits=256)
    k.update(data)
    return k.digest()


def generate_address() -> tuple[str, str]:
    """
    Generate a random private key and derive its Ethereum address.
    Returns (address, private_key_hex).

    Steps:
    1. Generate 32 secure random bytes as private key
    2. Use coincurve (libsecp256k1 C library) to get the public key
    3. Strip the 0x04 uncompressed prefix → 64-byte pubkey
    4. Keccak-256 hash the pubkey
    5. Take the last 20 bytes → Ethereum address
    """
    # Step 1: secure random private key
    priv_bytes = secrets.token_bytes(32)

    # Step 2: secp256k1 public key via coincurve (fast C library)
    priv_key = coincurve.PrivateKey(priv_bytes)
    pub_bytes = priv_key.public_key.format(compressed=False)[1:]  # strip 0x04

    # Step 3: Keccak-256 of the 64-byte public key
    digest = _keccak256(pub_bytes)

    # Step 4: last 20 bytes = Ethereum address
    address     = "0x" + digest[-20:].hex()
    private_key = "0x" + priv_bytes.hex()

    return address, private_key


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def esc(text: str) -> str:
    specials = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in specials else c for c in text)


def extract_patterns(address: str) -> tuple[str, str]:
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
    job = active_jobs.pop(chat_id, None)
    if not job:
        return
    job["stop_event"].set()
    task = job.get("task")
    if task and not task.done():
        task.cancel()


# ══════════════════════════════════════════════════════════════════════════════
#  WORKER THREAD
# ══════════════════════════════════════════════════════════════════════════════

def search_worker(
    worker_id: int,
    pattern: str,
    mode: str,
    stop_event: threading.Event,
    result: dict,
    counters: list,
) -> None:
    """
    Thread worker — generates addresses at maximum speed using
    coincurve + pycryptodome C extensions. Updates counters[worker_id]
    on every attempt so the progress display is always accurate.
    """
    attempts = 0

    while not stop_event.is_set():
        address, private_key = generate_address()
        attempts += 1
        counters[worker_id] = attempts

        # Check match against lowercase address body (strip 0x)
        body = address[2:].lower()
        matched = body.startswith(pattern) if mode == "prefix" else body.endswith(pattern)

        if matched:
            result["address"]     = address
            result["private_key"] = private_key
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
    result     = {}
    counters   = [0] * NUM_WORKERS
    start_time = time.monotonic()

    for wid in range(NUM_WORKERS):
        t = threading.Thread(
            target=search_worker,
            args=(wid, pattern, mode, stop_event, result, counters),
            daemon=True,
        )
        t.start()

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
        "stop_event": stop_event,
        "task":       task,
        "counters":   counters,
        "start_time": start_time,
        "pattern":    pattern,
        "mode":       mode,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  HANDLERS
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
    text = update.message.text.strip()

    if not ETH_ADDRESS_RE.match(text):
        await update.message.reply_text(
            "⚠️ That doesn't look like a valid Ethereum address\\.\n\n"
            "Please paste a full 42\\-character address starting with `0x`\\.\n\n"
            "_Example:_\n`0xDeaD1234567890AbCd1234567890abcdEF123456`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    prefix, suffix = extract_patterns(text)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔵 Match Prefix → 0x{prefix}…", callback_data=f"search:prefix:{prefix}")],
        [InlineKeyboardButton(f"🟣 Match Suffix → 0x…{suffix}", callback_data=f"search:suffix:{suffix}")],
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
    query   = update.callback_query
    chat_id = int(query.data.split(":")[1])
    await query.answer("Refreshed!")

    job = active_jobs.get(chat_id)
    if not job:
        await query.message.reply_text("No active search found. It may have already completed.")
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
        await update.message.reply_text("No active search to cancel.\n\nPaste an Ethereum address to start.")
        return
    kill_job(chat_id)
    await update.message.reply_text("🛑 Search cancelled.\n\nPaste an Ethereum address to start a new search.")


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
