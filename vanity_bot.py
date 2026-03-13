"""
vanity_bot.py — Vanity Ethereum Address Generator Telegram Bot
==============================================================
New smart flow:
  User pastes any Ethereum address → bot extracts the first 4 and last 4
  characters automatically and asks which they want to match.

Commands:
  /start   — welcome + usage guide
  /cancel  — cancel an in-progress search

Also handles plain text messages that look like Ethereum addresses.
"""

import asyncio
import logging
import multiprocessing
import os
import re
import time
from multiprocessing import Queue, Value

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

from vanity_worker import worker

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
NUM_WORKERS   = max(1, (os.cpu_count() or 2) - 1)
POLL_INTERVAL = 1.5
EDIT_INTERVAL = 3.0
EXTRACT_CHARS = 4   # chars to extract as prefix/suffix from pasted address

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

active_jobs: dict[int, dict] = {}


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def esc(text: str) -> str:
    specials = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in specials else c for c in text)


def extract_patterns(address: str) -> tuple[str, str]:
    """Return (prefix, suffix) — each EXTRACT_CHARS hex chars, lowercased."""
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
    try:
        job["stop_flag"].value = 1
    except Exception:
        pass
    for p in job.get("processes", []):
        try:
            p.terminate()
            p.join(timeout=1)
        except Exception:
            pass
    task = job.get("task")
    if task and not task.done():
        task.cancel()


# ══════════════════════════════════════════════════════════════════════════════
#  BACKGROUND POLLING TASK
# ══════════════════════════════════════════════════════════════════════════════

async def poll_results(
    chat_id: int,
    status_message,
    pattern: str,
    mode: str,
    result_queue: Queue,
    worker_attempts: dict,
    start_time: float,
) -> None:
    last_edit = time.monotonic()
    found     = None

    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)

            while not result_queue.empty():
                try:
                    msg = result_queue.get_nowait()
                except Exception:
                    break
                if msg[0] == "progress":
                    _, wid, attempts = msg
                    worker_attempts[wid] = attempts
                elif msg[0] == "found":
                    _, wid, attempts, address, private_key = msg
                    worker_attempts[wid] = attempts
                    found = (address, private_key)

            total   = sum(worker_attempts.values())
            elapsed = time.monotonic() - start_time
            rate    = int(total / elapsed) if elapsed > 0 else 0

            if found:
                address, private_key = found
                kill_job(chat_id)
                display  = f"0x{pattern}..." if mode == "prefix" else f"0x...{pattern}"
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Copy Address",     callback_data=f"copy:{address}")],
                    [InlineKeyboardButton("📋 Copy Private Key", callback_data=f"copy:{private_key}")],
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

            now = time.monotonic()
            if now - last_edit >= EDIT_INTERVAL:
                last_edit = now
                display   = f"0x{pattern}..." if mode == "prefix" else f"0x...{pattern}"
                try:
                    await status_message.edit_text(
                        "🔄 *Searching for vanity address…*\n\n"
                        f"🎯 *Pattern:* `{esc(display)}`\n\n"
                        f"🔁 Attempts: *{esc(f'{total:,}')}*\n"
                        f"⚡ Speed: *{esc(f'{rate:,}')}* addr/s\n"
                        f"⏱ Elapsed: *{esc(f'{elapsed:.0f}s')}*\n\n"
                        "_Use /cancel to stop the search\\._",
                        parse_mode=ParseMode.MARKDOWN_V2,
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

    result_queue: Queue = multiprocessing.Queue()
    stop_flag           = multiprocessing.Value("b", 0)
    processes           = []
    worker_attempts     = {i: 0 for i in range(NUM_WORKERS)}
    start_time          = time.monotonic()

    for wid in range(NUM_WORKERS):
        p = multiprocessing.Process(
            target=worker,
            args=(wid, pattern, mode, result_queue, stop_flag),
            daemon=True,
        )
        p.start()
        processes.append(p)

    logger.info("Chat %s | %d workers | %s %s", chat_id, NUM_WORKERS, mode, pattern)

    task = asyncio.create_task(
        poll_results(
            chat_id=chat_id,
            status_message=status_msg,
            pattern=pattern,
            mode=mode,
            result_queue=result_queue,
            worker_attempts=worker_attempts,
            start_time=start_time,
        )
    )

    active_jobs[chat_id] = {"processes": processes, "stop_flag": stop_flag, "task": task}


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
        "Use /cancel to stop any active search.",
    )


async def handle_address_message(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    User pastes an Ethereum address as a plain message.
    Extract prefix + suffix and show choice buttons.
    """
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

    # callback_data format: "search:prefix:dead" or "search:suffix:cafe"
    parts   = query.data.split(":", 2)
    mode    = parts[1]   # "prefix" or "suffix"
    pattern = parts[2]   # e.g. "dead"

    async def send_status(text: str):
        return await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

    await launch_search(chat_id, pattern, mode, send_status)


async def handle_copy_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 📋 Copy Address / Copy Private Key button taps."""
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
            "ℹ️ No active search to cancel\\.\n\n"
            "_Paste an Ethereum address to start a new search\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    kill_job(chat_id)
    await update.message.reply_text(
        "🛑 *Search cancelled\\.*\n\n"
        "_Paste an Ethereum address to start a new search\\._",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    logger.info("Starting Vanity ETH Bot | %d workers", NUM_WORKERS)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_search_callback, pattern=r"^search:"))
    app.add_handler(CallbackQueryHandler(handle_copy_callback,   pattern=r"^copy:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address_message))

    # Global error handler — logs any unhandled exceptions so they appear in Railway logs
    async def error_handler(update, context):
        logger.error("Unhandled exception: %s", context.error, exc_info=context.error)

    app.add_error_handler(error_handler)

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
