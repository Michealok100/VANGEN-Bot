"""
vanity_bot.py — Vanity Ethereum Address Generator Telegram Bot

Address generation uses the proven pattern from:
  https://www.arthurkoziel.com/generating-ethereum-addresses-in-python/

  coincurve  → libsecp256k1 C bindings, releases the GIL ✓
  pysha3     → keccak_256 C bindings, releases the GIL ✓

Because both libraries release the GIL, plain threads work perfectly.
Each thread runs at full CPU speed without starving asyncio.

Requirements (pip install):
  coincurve pysha3 python-telegram-bot python-dotenv
  (eth-account kept as fallback if coincurve/pysha3 unavailable)
"""

import asyncio
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

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
    raise SystemExit("❌ ERROR: TELEGRAM_BOT_TOKEN is not set.")

# ── Config ────────────────────────────────────────────────────────────────────
POLL_INTERVAL  = 2.0    # seconds between asyncio poll ticks
EDIT_INTERVAL  = 4.0    # seconds between Telegram message edits
EXTRACT_CHARS  = 4      # chars to extract from prefix/suffix of pasted address
REPORT_EVERY   = 200    # worker flushes attempt count every N iterations
NUM_WORKERS    = os.cpu_count() or 4

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

_executor = ThreadPoolExecutor(max_workers=NUM_WORKERS)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

active_jobs: dict[int, dict] = {}


# ══════════════════════════════════════════════════════════════════════════════
#  ADDRESS GENERATOR
#  Proven pattern: coincurve + pysha3 (both C extensions, both release GIL)
#  Fallback: eth_account
# ══════════════════════════════════════════════════════════════════════════════

def _build_generator():
    # ── Option 1: coincurve + pysha3 (fastest, GIL-free) ─────────────────────
    try:
        from coincurve import PublicKey
        from sha3 import keccak_256

        def _gen(token_bytes=os.urandom) -> tuple[str, str] | None:
            priv_bytes = token_bytes(32)
            try:
                pub_bytes = PublicKey.from_valid_secret(priv_bytes).format(compressed=False)[1:]
                addr = "0x" + keccak_256(pub_bytes).digest()[-20:].hex()
                return addr, "0x" + priv_bytes.hex()
            except Exception:
                return None

        logger.info("Generator: coincurve + pysha3 | %d threads", NUM_WORKERS)
        return _gen

    except ImportError:
        pass

    # ── Option 2: coincurve + pycryptodome ───────────────────────────────────
    try:
        from coincurve import PublicKey
        from Crypto.Hash import keccak as _kmod

        def _gen(token_bytes=os.urandom) -> tuple[str, str] | None:
            priv_bytes = token_bytes(32)
            try:
                pub_bytes = PublicKey.from_valid_secret(priv_bytes).format(compressed=False)[1:]
                k = _kmod.new(digest_bits=256)
                k.update(pub_bytes)
                addr = "0x" + k.digest()[-20:].hex()
                return addr, "0x" + priv_bytes.hex()
            except Exception:
                return None

        logger.info("Generator: coincurve + pycryptodome | %d threads", NUM_WORKERS)
        return _gen

    except ImportError:
        pass

    # ── Option 3: eth_account (slowest, but always works) ────────────────────
    try:
        from eth_account import Account

        def _gen(token_bytes=os.urandom) -> tuple[str, str] | None:
            priv_bytes = token_bytes(32)
            try:
                key  = "0x" + priv_bytes.hex()
                addr = Account.from_key(key).address
                return addr, key
            except Exception:
                return None

        logger.warning(
            "Generator: eth_account (slow). Install coincurve+pysha3 for best performance."
        )
        return _gen

    except ImportError:
        pass

    raise SystemExit(
        "❌ No crypto library found.\n"
        "Run: pip install coincurve pysha3\n"
        "Or:  pip install eth-account"
    )


_generate = _build_generator()


# ══════════════════════════════════════════════════════════════════════════════
#  WORKER THREAD
#  coincurve + pysha3 release the GIL → threads run truly in parallel
#  shared dict is written infrequently, no lock needed for progress counter
# ══════════════════════════════════════════════════════════════════════════════

def _search_worker(
    pattern: str,
    mode: str,
    stop_event: threading.Event,
    shared: dict,
) -> None:
    local_attempts = 0
    last_flush     = 0

    while not stop_event.is_set():
        result = _generate()
        if result is None:
            continue

        addr, key      = result
        local_attempts += 1

        # Flush progress counter periodically (no lock — minor inaccuracy is fine)
        if local_attempts - last_flush >= REPORT_EVERY:
            shared["attempts"] = shared.get("attempts", 0) + (local_attempts - last_flush)
            last_flush = local_attempts

        # Pattern match
        body    = addr[2:].lower()
        matched = body.startswith(pattern) if mode == "prefix" else body.endswith(pattern)

        if matched:
            shared["attempts"]    = shared.get("attempts", 0) + (local_attempts - last_flush)
            shared["address"]     = addr
            shared["private_key"] = key
            stop_event.set()
            return

    # Flush remainder on exit
    remainder = local_attempts - last_flush
    if remainder:
        shared["attempts"] = shared.get("attempts", 0) + remainder


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
#  ASYNCIO POLLING TASK
# ══════════════════════════════════════════════════════════════════════════════

async def _poll(
    chat_id: int,
    status_msg,
    pattern: str,
    mode: str,
    stop_event: threading.Event,
    shared: dict,
    start_time: float,
) -> None:
    last_edit = time.monotonic()
    display   = f"0x{pattern}..." if mode == "prefix" else f"0x...{pattern}"

    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)

            total   = shared.get("attempts", 0)
            elapsed = time.monotonic() - start_time
            rate    = int(total / elapsed) if elapsed > 0 else 0

            # ── Found ─────────────────────────────────────────────────────────
            if stop_event.is_set() and shared.get("address"):
                addr = shared["address"]
                key  = shared["private_key"]
                kill_job(chat_id)

                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Copy Address",      callback_data=f"copy:{addr}")],
                    [InlineKeyboardButton("📋 Copy Private Key",  callback_data=f"copy:{key}")],
                    [InlineKeyboardButton("🔗 View on Etherscan", url=f"https://etherscan.io/address/{addr}")],
                ])
                await status_msg.edit_text(
                    "✅ *Vanity Address Found\\!*\n\n"
                    f"🎯 *Pattern:* `{esc(display)}`\n\n"
                    f"📬 *Address:*\n`{addr}`\n\n"
                    f"🔑 *Private Key:*\n`{key}`\n\n"
                    "─────────────────────────\n"
                    f"🔁 *Attempts:* {esc(f'{total:,}')}\n"
                    f"⏱ *Time:* {esc(f'{elapsed:.1f}s')}\n"
                    f"⚡ *Speed:* {esc(f'{rate:,}')} addr/s\n\n"
                    "⚠️ _Keep your private key secret\\. Never share it\\._",
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=kb,
                )
                return

            # ── Progress update ───────────────────────────────────────────────
            now = time.monotonic()
            if now - last_edit >= EDIT_INTERVAL:
                last_edit = now
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh Progress", callback_data=f"refresh:{chat_id}")],
                    [InlineKeyboardButton("🛑 Cancel Search",    callback_data=f"canceljob:{chat_id}")],
                ])
                try:
                    await status_msg.edit_text(
                        "🔄 *Searching for vanity address…*\n\n"
                        f"🎯 *Pattern:* `{esc(display)}`\n\n"
                        f"🔁 Attempts: *{esc(f'{total:,}')}*\n"
                        f"⚡ Speed: *{esc(f'{rate:,}')}* addr/s\n"
                        f"⏱ Elapsed: *{esc(f'{elapsed:.0f}s')}*\n\n"
                        "_Tap Refresh to update stats\\._",
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_markup=kb,
                    )
                except Exception:
                    pass

    except asyncio.CancelledError:
        logger.info("Poll cancelled for chat %s", chat_id)
        raise


# ══════════════════════════════════════════════════════════════════════════════
#  SEARCH LAUNCHER
# ══════════════════════════════════════════════════════════════════════════════

async def launch_search(chat_id: int, pattern: str, mode: str, reply_fn) -> None:
    if chat_id in active_jobs:
        kill_job(chat_id)
        await asyncio.sleep(0.3)

    display  = f"0x{pattern}..." if mode == "prefix" else f"0x...{pattern}"
    expected = estimate_attempts(len(pattern))

    status_msg = await reply_fn(
        "🔄 *Starting vanity search…*\n\n"
        f"🎯 *Pattern:* `{esc(display)}`\n"
        f"📊 *Expected:* \\~{esc(expected)} attempts\n"
        f"⚙️ *Workers:* {NUM_WORKERS} threads\n\n"
        "_Spinning up workers\\.\\.\\._"
    )

    stop_event = threading.Event()
    shared     = {"attempts": 0}
    start_time = time.monotonic()
    loop       = asyncio.get_event_loop()

    # Launch one worker thread per CPU core
    for _ in range(NUM_WORKERS):
        loop.run_in_executor(_executor, _search_worker, pattern, mode, stop_event, shared)

    logger.info("Chat %s | search started | %s '%s' | %d workers", chat_id, mode, pattern, NUM_WORKERS)

    task = asyncio.create_task(
        _poll(
            chat_id=chat_id,
            status_msg=status_msg,
            pattern=pattern,
            mode=mode,
            stop_event=stop_event,
            shared=shared,
            start_time=start_time,
        )
    )

    active_jobs[chat_id] = {
        "stop_event": stop_event,
        "task":       task,
        "shared":     shared,
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
        "Paste any Ethereum address and I extract the prefix and suffix automatically.\n\n"
        "How to use:\n"
        "1. Paste an Ethereum address\n"
        "2. I extract the first and last 4 characters\n"
        "3. Tap Match Prefix or Match Suffix to start\n\n"
        "Difficulty Guide:\n"
        "4 chars = ~65K attempts (seconds)\n"
        "5 chars = ~1M attempts (1-2 min)\n"
        "6 chars = ~16M attempts (30-60 min)\n\n"
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

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔵 Match Prefix → 0x{prefix}…", callback_data=f"search:prefix:{prefix}")],
        [InlineKeyboardButton(f"🟣 Match Suffix → 0x…{suffix}", callback_data=f"search:suffix:{suffix}")],
    ])

    await update.message.reply_text(
        "✅ *Address received\\!*\n\n"
        f"`{esc(text)}`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Extracted patterns:\n\n"
        f"🔵 *Prefix:* `0x{esc(prefix)}…`\n"
        f"🟣 *Suffix:* `0x…{esc(suffix)}`\n\n"
        "👇 *Which would you like to search for?*",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb,
    )


async def handle_search_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()
    parts   = query.data.split(":", 2)

    async def send_status(text: str):
        return await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

    await launch_search(chat_id, parts[1], parts[2], send_status)


async def handle_refresh_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    chat_id = int(query.data.split(":")[1])
    await query.answer("Refreshed!")

    job = active_jobs.get(chat_id)
    if not job:
        await query.message.reply_text("No active search. It may have already completed.")
        return

    shared  = job["shared"]
    total   = shared.get("attempts", 0)
    elapsed = time.monotonic() - job["start_time"]
    rate    = int(total / elapsed) if elapsed > 0 else 0
    pattern = job["pattern"]
    mode    = job["mode"]
    display = f"0x{pattern}..." if mode == "prefix" else f"0x...{pattern}"

    kb = InlineKeyboardMarkup([
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
            reply_markup=kb,
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
    logger.info("Starting Vanity ETH Bot | %d worker threads", NUM_WORKERS)

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

    logger.info("Bot is running.")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
