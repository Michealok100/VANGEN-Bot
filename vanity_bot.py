"""
vanity_bot.py — Vanity Ethereum Address Generator Telegram Bot

Uses threads (not multiprocessing) for Railway compatibility.
coincurve + pysha3 release the GIL so threads run truly in parallel.
"""

import asyncio
import logging
import os
import queue
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

from vanity_worker import worker as _worker

# ── Environment ───────────────────────────────────────────────────────────────
load_dotenv()

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("❌ TELEGRAM_BOT_TOKEN not set.")

# ── Config ────────────────────────────────────────────────────────────────────
POLL_INTERVAL = 2.0
EDIT_INTERVAL = 5.0
EXTRACT_CHARS = 4
NUM_WORKERS   = max(2, os.cpu_count() or 2)

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

_executor = ThreadPoolExecutor(max_workers=NUM_WORKERS)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
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


def drain_queue(q: queue.Queue) -> list:
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    return items


# ══════════════════════════════════════════════════════════════════════════════
#  ASYNCIO POLLING TASK
# ══════════════════════════════════════════════════════════════════════════════

async def _poll(
    chat_id: int,
    status_msg,
    pattern: str,
    mode: str,
    result_queue: queue.Queue,
    stop_event: threading.Event,
    start_time: float,
) -> None:
    last_edit = time.monotonic()
    display   = f"0x{pattern}..." if mode == "prefix" else f"0x...{pattern}"
    total     = 0
    loop      = asyncio.get_event_loop()

    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)

            items   = await loop.run_in_executor(None, drain_queue, result_queue)
            elapsed = time.monotonic() - start_time
            rate    = int(total / elapsed) if elapsed > 0 else 0

            for item in items:
                kind = item[0]

                if kind == "progress":
                    total += item[2]

                elif kind == "found":
                    _, _wid, attempts, addr, key = item
                    total += attempts
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

            # ── Progress edit ─────────────────────────────────────────────────
            now = time.monotonic()
            if now - last_edit >= EDIT_INTERVAL:
                last_edit = now
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{chat_id}")],
                    [InlineKeyboardButton("🛑 Cancel",  callback_data=f"canceljob:{chat_id}")],
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

    result_queue = queue.Queue()
    stop_event   = threading.Event()
    start_time   = time.monotonic()
    loop         = asyncio.get_event_loop()

    logger.info("Chat %s | started | %s '%s' | %d workers", chat_id, mode, pattern, NUM_WORKERS)

    for i in range(NUM_WORKERS):
        loop.run_in_executor(_executor, _worker, i, pattern, mode, result_queue, stop_event)

    task = asyncio.create_task(
        _poll(
            chat_id=chat_id,
            status_msg=status_msg,
            pattern=pattern,
            mode=mode,
            result_queue=result_queue,
            stop_event=stop_event,
            start_time=start_time,
        )
    )

    active_jobs[chat_id] = {
        "stop_event":   stop_event,
        "task":         task,
        "result_queue": result_queue,
        "start_time":   start_time,
        "pattern":      pattern,
        "mode":         mode,
        "total":        0,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔐 *Vanity Ethereum Address Generator*\n\n"
        "Paste any Ethereum address and I'll extract the first and last 4 characters automatically\\.\n\n"
        "*How to use:*\n"
        "1\\. Paste a full Ethereum address\n"
        "2\\. Tap *Match Prefix* or *Match Suffix*\n"
        "3\\. Wait for your vanity address\\!\n\n"
        "*Difficulty Guide:*\n"
        "4 chars \\= \\~65K attempts \\(seconds\\)\n"
        "5 chars \\= \\~1M attempts \\(\\~15 sec\\)\n"
        "6 chars \\= \\~16M attempts \\(\\~3 min\\)\n\n"
        "Use /cancel to stop an active search\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
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
        [InlineKeyboardButton(f"🔵 Match Prefix  0x{prefix}…",  callback_data=f"search:prefix:{prefix}")],
        [InlineKeyboardButton(f"🟣 Match Suffix  0x…{suffix}", callback_data=f"search:suffix:{suffix}")],
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

    await launch_search(chat_id, parts[2], parts[1], send_status)


async def handle_refresh_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    chat_id = int(query.data.split(":")[1])
    await query.answer("Refreshed!")

    job = active_jobs.get(chat_id)
    if not job:
        await query.message.reply_text(
            "No active search\\. It may have already completed\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    loop    = asyncio.get_event_loop()
    items   = await loop.run_in_executor(None, drain_queue, job["result_queue"])
    elapsed = time.monotonic() - job["start_time"]
    total   = job.get("total", 0) + sum(i[2] for i in items if i[0] == "progress")
    job["total"] = total
    rate    = int(total / elapsed) if elapsed > 0 else 0
    display = f"0x{job['pattern']}..." if job["mode"] == "prefix" else f"0x...{job['pattern']}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{chat_id}")],
        [InlineKeyboardButton("🛑 Cancel",  callback_data=f"canceljob:{chat_id}")],
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
    if query.data.startswith("copy:"):
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
            "No active search to cancel\\.\n\nPaste an Ethereum address to start\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    kill_job(chat_id)
    await update.message.reply_text(
        "🛑 Search cancelled\\.\n\nPaste an Ethereum address to start a new search\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    logger.info("Starting VANGEN Bot | %d worker threads", NUM_WORKERS)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_search_callback,    pattern=r"^search:"))
    app.add_handler(CallbackQueryHandler(handle_copy_callback,      pattern=r"^copy:"))
    app.add_handler(CallbackQueryHandler(handle_refresh_callback,   pattern=r"^refresh:"))
    app.add_handler(CallbackQueryHandler(handle_cancelJob_callback, pattern=r"^canceljob:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address_message))

    async def error_handler(update, context):
        logger.error("Unhandled error: %s", context.error, exc_info=context.error)

    app.add_error_handler(error_handler)

    logger.info("Bot is running.")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
