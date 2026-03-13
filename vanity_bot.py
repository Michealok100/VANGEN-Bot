"""
vanity_bot.py — Vanity Ethereum Address Generator Telegram Bot

Architecture:
- CPU workers run as separate PROCESSES (not threads) via multiprocessing.
  This escapes Python's GIL entirely — each process runs at full speed on
  its own core without competing with the asyncio event loop.
- Results are passed back via a multiprocessing.Queue.
- A single asyncio task polls the queue every POLL_INTERVAL seconds.
- The asyncio event loop is never blocked.
"""

import asyncio
import logging
import multiprocessing as mp
import os
import re
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
POLL_INTERVAL = 2.0
EDIT_INTERVAL = 4.0
EXTRACT_CHARS = 4
REPORT_EVERY  = 1000    # each worker reports attempts every N iterations

ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CURVE_ORDER   = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

# One worker process per CPU core
NUM_WORKERS = os.cpu_count() or 4

# Small thread pool just for the asyncio→queue bridge (very low load)
_bridge_executor = ThreadPoolExecutor(max_workers=1)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

active_jobs: dict[int, dict] = {}


# ══════════════════════════════════════════════════════════════════════════════
#  WORKER PROCESS — no asyncio, no GIL contention with the bot
# ══════════════════════════════════════════════════════════════════════════════

def _build_generator_in_process():
    """Called inside each worker process so imports are process-local."""
    try:
        import coincurve
        from Crypto.Hash import keccak as _kmod

        def _gen():
            priv = os.urandom(32)
            if int.from_bytes(priv, "big") in (0, _CURVE_ORDER):
                return None
            try:
                pub  = coincurve.PrivateKey(priv).public_key.format(compressed=False)[1:]
                k    = _kmod.new(digest_bits=256)
                k.update(pub)
                addr = "0x" + k.digest()[-20:].hex()
                return addr, "0x" + priv.hex()
            except Exception:
                return None

        return _gen
    except ImportError:
        pass

    try:
        from eth_account import Account

        def _gen():
            priv = os.urandom(32)
            if int.from_bytes(priv, "big") in (0, _CURVE_ORDER):
                return None
            try:
                key  = "0x" + priv.hex()
                addr = Account.from_key(key).address
                return addr, key
            except Exception:
                return None

        return _gen
    except ImportError:
        pass

    return None


def _worker_process(pattern: str, mode: str, result_queue: mp.Queue, stop_event: mp.Event) -> None:
    """
    Runs in a separate process. Puts either:
      ("progress", attempts)  — periodic heartbeat
      ("found", addr, key, attempts) — match found
    into result_queue.
    """
    generate = _build_generator_in_process()
    if generate is None:
        result_queue.put(("error", "No crypto library available"))
        return

    attempts    = 0
    last_report = 0

    while not stop_event.is_set():
        try:
            result = generate()
            if result is None:
                continue

            addr, key  = result
            attempts  += 1

            if attempts - last_report >= REPORT_EVERY:
                result_queue.put(("progress", attempts))
                last_report = attempts

            body    = addr[2:].lower()
            matched = body.startswith(pattern) if mode == "prefix" else body.endswith(pattern)

            if matched:
                result_queue.put(("found", addr, key, attempts))
                return

        except Exception:
            pass


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
        return f"{val/1_000_000_000:.1f}B"
    if val >= 1_000_000:
        return f"{val/1_000_000:.1f}M"
    if val >= 1_000:
        return f"{val/1_000:.1f}K"
    return str(val)


def kill_job(chat_id: int) -> None:
    job = active_jobs.pop(chat_id, None)
    if not job:
        return
    job["stop_event"].set()
    for proc in job.get("processes", []):
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2)
    task = job.get("task")
    if task and not task.done():
        task.cancel()


# ══════════════════════════════════════════════════════════════════════════════
#  ASYNCIO BRIDGE — drains mp.Queue without blocking the event loop
# ══════════════════════════════════════════════════════════════════════════════

def _drain_queue(q: mp.Queue) -> list:
    """Non-blocking drain of all available items. Runs in thread executor."""
    items = []
    try:
        while True:
            items.append(q.get_nowait())
    except Exception:
        pass
    return items


# ══════════════════════════════════════════════════════════════════════════════
#  ASYNCIO POLLING TASK
# ══════════════════════════════════════════════════════════════════════════════

async def _poll(
    chat_id: int,
    status_msg,
    pattern: str,
    mode: str,
    result_queue: mp.Queue,
    start_time: float,
) -> None:
    last_edit = time.monotonic()
    display   = f"0x{pattern}..." if mode == "prefix" else f"0x...{pattern}"
    total     = 0
    loop      = asyncio.get_event_loop()

    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)

            # Drain queue in thread — never blocks the event loop
            items   = await loop.run_in_executor(_bridge_executor, _drain_queue, result_queue)
            elapsed = time.monotonic() - start_time
            rate    = int(total / elapsed) if elapsed > 0 else 0

            for item in items:
                msg_type = item[0]

                if msg_type == "progress":
                    # Each worker reports its own count; sum across all workers
                    total += item[1]

                elif msg_type == "found":
                    _, addr, key, worker_attempts = item
                    total += worker_attempts
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

                elif msg_type == "error":
                    logger.error("Worker error: %s", item[1])

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
        f"⚙️ *Workers:* {NUM_WORKERS} CPU cores\n\n"
        "_Spinning up workers\\.\\.\\._"
    )

    result_queue = mp.Queue()
    stop_event   = mp.Event()
    start_time   = time.monotonic()

    # Spawn one independent process per CPU core
    processes = []
    for _ in range(NUM_WORKERS):
        proc = mp.Process(
            target=_worker_process,
            args=(pattern, mode, result_queue, stop_event),
            daemon=True,   # auto-killed if the bot process exits
        )
        proc.start()
        processes.append(proc)

    logger.info(
        "Chat %s | search started | %s %s | %d processes",
        chat_id, mode, pattern, NUM_WORKERS,
    )

    task = asyncio.create_task(
        _poll(
            chat_id=chat_id,
            status_msg=status_msg,
            pattern=pattern,
            mode=mode,
            result_queue=result_queue,
            start_time=start_time,
        )
    )

    active_jobs[chat_id] = {
        "stop_event":   stop_event,
        "processes":    processes,
        "task":         task,
        "result_queue": result_queue,
        "start_time":   start_time,
        "pattern":      pattern,
        "mode":         mode,
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

    elapsed = time.monotonic() - job["start_time"]
    pattern = job["pattern"]
    mode    = job["mode"]
    display = f"0x{pattern}..." if mode == "prefix" else f"0x...{pattern}"

    loop  = asyncio.get_event_loop()
    items = await loop.run_in_executor(_bridge_executor, _drain_queue, job["result_queue"])
    total = sum(item[1] for item in items if item[0] == "progress")
    rate  = int(total / elapsed) if elapsed > 0 and total > 0 else 0

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
    # Required on Windows and macOS with Python 3.8+
    mp.set_start_method("spawn", force=True)

    logger.info("Starting Vanity ETH Bot | %d worker processes", NUM_WORKERS)

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
