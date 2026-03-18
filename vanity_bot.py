"""
vanity_bot.py — Vanity Ethereum Address Generator Telegram Bot
Runs on Railway. Uses threads via vanity_worker.py.
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
POLL_INTERVAL  = 0.2    # check queue every 0.2s for snappier stats
EDIT_INTERVAL  = 3.0    # seconds between Telegram message edits
EXTRACT_CHARS  = 4      # 4-char prefix + 4-char suffix matched simultaneously
NUM_WORKERS    = 64     # 64 workers for maximum 4+4 search throughput

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_executor      = ThreadPoolExecutor(max_workers=NUM_WORKERS)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.DEBUG,
)
logger = logging.getLogger(__name__)

active_jobs: dict[int, dict] = {}
result_store: dict[str, str] = {}  # short_id → value


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


def drain_queue(q: queue.Queue) -> list:
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    return items


def kill_job(chat_id: int) -> None:
    """Stop workers and cancel poll task for a chat."""
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
    prefix: str,
    suffix: str,
    result_queue: queue.Queue,
    stop_event: threading.Event,
    start_time: float,
) -> None:
    last_edit = time.monotonic() - EDIT_INTERVAL  # fires after first interval once total > 0
    display   = f"0x{prefix}...{suffix}"
    total     = 0

    async def send_found(addr: str, key: str) -> None:
        """Edit the status message with the found result, then auto-send prefix/suffix."""
        elapsed = time.monotonic() - start_time
        rate    = int(total / elapsed) if elapsed > 0 else 0
        # Remove from active jobs without cancelling ourselves
        active_jobs.pop(chat_id, None)
        stop_event.set()
        # Telegram limit: callback_data max 64 bytes
        # Store full values in result_store, use short IDs in buttons
        addr_id = f"a{chat_id}"
        key_id  = f"k{chat_id}"
        result_store[addr_id] = addr
        result_store[key_id]  = key

        # Extract first-4 prefix and last-4 suffix from the found address (skip 0x)
        body          = addr[2:].lower()
        auto_prefix   = body[:4]
        auto_suffix   = body[-4:]

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Copy Address",      callback_data=f"copy:{addr_id}")],
            [InlineKeyboardButton("📋 Copy Private Key",  callback_data=f"copy:{key_id}")],
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

        # Auto-send the first-4 prefix and last-4 suffix for easy copying
        await status_msg.reply_text(
            "📋 *Auto\\-copied patterns from your new address:*\n\n"
            f"🔵 *Prefix \\(first 4\\):*\n`{auto_prefix}`\n\n"
            f"🟣 *Suffix \\(last 4\\):*\n`{auto_suffix}`\n\n"
            "_Tap and hold either value to copy\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)

            # Drain everything currently in the queue
            items = drain_queue(result_queue)

            for item in items:
                if item[0] == "progress":
                    total += item[2]

                elif item[0] == "found":
                    _, _wid, attempts, addr, key = item
                    total += attempts
                    await send_found(addr, key)
                    return  # done

            # Sync total back to job dict so refresh handler always has latest count
            if chat_id in active_jobs:
                active_jobs[chat_id]["total"] = total

            logger.debug("Poll chat=%s total=%d queue_items=%d", chat_id, total, len(items))

            # Progress edit — only update if we have real data and interval has passed
            now = time.monotonic()
            if total > 0 and now - last_edit >= EDIT_INTERVAL:
                last_edit = now
                elapsed = now - start_time
                rate    = int(total / elapsed) if elapsed > 0 else 0
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
                except Exception as e:
                    logger.warning("Poll edit failed: %s", e)

    except asyncio.CancelledError:
        # Final drain — result might already be sitting in the queue
        try:
            items = drain_queue(result_queue)
            for item in items:
                if item[0] == "found":
                    _, _wid, attempts, addr, key = item
                    total += attempts
                    await send_found(addr, key)
                    return
        except Exception as e:
                    logger.warning("Poll cancelled drain failed: %s", e)
        logger.info("Poll cancelled for chat %s", chat_id)
        raise


# ══════════════════════════════════════════════════════════════════════════════
#  SEARCH LAUNCHER
# ══════════════════════════════════════════════════════════════════════════════

async def launch_search(chat_id: int, prefix: str, suffix: str, reply_fn) -> None:
    if chat_id in active_jobs:
        kill_job(chat_id)
        await asyncio.sleep(0.3)

    display  = f"0x{prefix}\\.\\.\\.{suffix}"
    expected = estimate_attempts(len(prefix) + len(suffix))

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

    # Start worker threads — pass prefix+suffix, mode="both"
    for i in range(NUM_WORKERS):
        loop.run_in_executor(_executor, _worker, i, prefix, suffix, result_queue, stop_event)

    logger.info("Chat %s | mode=both prefix='%s' suffix='%s' | %d workers", chat_id, prefix, suffix, NUM_WORKERS)

    task = asyncio.create_task(
        _poll(
            chat_id=chat_id,
            status_msg=status_msg,
            prefix=prefix,
            suffix=suffix,
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
        "prefix":       prefix,
        "suffix":       suffix,
        "total":        0,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔐 *Vanity Ethereum Address Generator*\n\n"
        "Paste any Ethereum address and I'll extract the first *4* and last *4* characters, "
        "then search for an address matching *both* simultaneously\\.\n\n"
        "*How to use:*\n"
        "1\\. Paste a full Ethereum address\n"
        "2\\. Tap *Search* to start\n"
        "3\\. Wait for your vanity address\\!\n"
        "4\\. The prefix and suffix are auto\\-copied for you 🎉\n\n"
        "*Difficulty:*\n"
        "4\\+4 chars \\= \\~4\\.3B attempts \\(\\~5\\-30 min with fast libs\\)\n\n"
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
        [InlineKeyboardButton(f"🎯 Search  0x{prefix}...{suffix}", callback_data=f"search:{prefix}:{suffix}")],
    ])

    await update.message.reply_text(
        "✅ *Address received\\!*\n\n"
        f"`{esc(text)}`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Extracted patterns:\n\n"
        f"🔵 *Prefix \\(first 4\\):* `0x{esc(prefix)}...`\n"
        f"🟣 *Suffix \\(last 4\\):* `0x...{esc(suffix)}`\n\n"
        f"🎯 *Combined:* `0x{esc(prefix)}...{esc(suffix)}`\n\n"
        "💡 _Searches for an address matching both at once\\._\n\n"
        "👇 *Tap to start searching:*",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb,
    )


async def handle_search_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()

    parts = query.data.split(":")
    logger.info("DEBUG search: raw_data='%s' parts=%s", query.data, parts)

    # Reject old-format buttons: search:prefix:xxx or search:suffix:xxx
    if parts[1] in ("prefix", "suffix"):
        await query.message.reply_text(
            "⚠️ *This button is outdated\\.*\n\n"
            "Please paste your Ethereum address again to get a fresh search button\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # New format: search:abcd:f9e2
    _, prefix, suffix = parts
    logger.info("DEBUG search: prefix='%s' suffix='%s'", prefix, suffix)

    async def send_status(text: str):
        return await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

    await launch_search(chat_id, prefix, suffix, send_status)


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

    elapsed = time.monotonic() - job["start_time"]
    total   = job.get("total", 0)
    rate    = int(total / elapsed) if elapsed > 0 else 0
    display = f"0x{job['prefix']}...{job['suffix']}"

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
        short_id = query.data.split("copy:", 1)[1]
        value    = result_store.get(short_id, short_id)  # fallback to raw if not found
        label    = "address" if short_id.startswith("a") else "private key"
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
