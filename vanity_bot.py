"""
bot.py — Vanity Ethereum Address Generator Telegram Bot
========================================================
Commands:
  /start                — welcome + usage guide
  /vanity prefix <pat>  — find address starting with <pat>
  /vanity suffix <pat>  — find address ending with <pat>
  /cancel               — cancel an in-progress search

Architecture:
  • The Telegram bot runs on Python's asyncio event loop.
  • When /vanity is called, worker processes are spawned via multiprocessing.
  • An asyncio background task polls the shared Queue for progress/results
    and edits the Telegram message live — keeping the bot responsive.
  • A per-chat lock prevents one user from running two searches at once.
"""

import asyncio
import logging
import multiprocessing
import os
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
# Number of CPU workers per search job.
# Defaults to (cpu_count - 1) so the bot process itself stays responsive.
NUM_WORKERS    = max(1, (os.cpu_count() or 2) - 1)

# How often (seconds) the background task checks the queue for progress
POLL_INTERVAL  = 1.5

# How often (seconds) the Telegram message is edited with new stats
# (Telegram rate-limits edits to ~1 per 2s per message)
EDIT_INTERVAL  = 3.0

# Maximum pattern length — longer patterns can take hours / days
MAX_PATTERN_LEN = 6

# Valid hex characters
VALID_HEX = set("0123456789abcdef")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Per-chat job tracking ─────────────────────────────────────────────────────
# Maps chat_id → {"processes": [...], "stop_flag": Value, "task": asyncio.Task}
active_jobs: dict[int, dict] = {}


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def esc(text: str) -> str:
    """Escape a string for Telegram MarkdownV2."""
    specials = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in specials else c for c in text)


def normalise_pattern(raw: str) -> tuple[str, str]:
    """
    Validate and normalise a vanity pattern.
    Returns (pattern, error_message). error_message is "" on success.
    """
    pattern = raw.lower().lstrip()
    if pattern.startswith("0x"):
        pattern = pattern[2:]

    if not pattern:
        return "", "Pattern cannot be empty\\."

    if len(pattern) > MAX_PATTERN_LEN:
        return "", (
            f"Pattern too long \\(max {MAX_PATTERN_LEN} characters\\)\\.\n"
            f"Longer patterns can take _days_ to find\\."
        )

    invalid = set(pattern) - VALID_HEX
    if invalid:
        chars = ", ".join(f"`{c}`" for c in sorted(invalid))
        return "", f"Pattern contains non\\-hex characters: {chars}"

    return pattern, ""


def estimate_attempts(n: int) -> str:
    """Human-readable expected attempt count for a pattern of length n."""
    val = 16 ** n
    if val >= 1_000_000_000:
        return f"{val / 1_000_000_000:.1f}B"
    if val >= 1_000_000:
        return f"{val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"{val / 1_000:.1f}K"
    return str(val)


def difficulty_warning(n: int) -> str:
    """Return a MarkdownV2-safe warning for patterns that may take a long time."""
    if n <= 4:
        return ""
    if n == 5:
        return "\n⚠️ _5\\-char pattern: may take \\~1\\-2 minutes\\._"
    return "\n⚠️ _6\\-char pattern: may take \\~30\\-60 minutes\\. Be patient\\!_"


def kill_job(chat_id: int) -> None:
    """Terminate all worker processes and cancel the background task for a chat."""
    job = active_jobs.pop(chat_id, None)
    if not job:
        return

    # Signal workers to stop
    try:
        job["stop_flag"].value = 1
    except Exception:
        pass

    # Terminate processes
    for p in job.get("processes", []):
        try:
            p.terminate()
            p.join(timeout=1)
        except Exception:
            pass

    # Cancel the asyncio polling task
    task = job.get("task")
    if task and not task.done():
        task.cancel()


# ══════════════════════════════════════════════════════════════════════════════
#  BACKGROUND POLLING TASK
# ══════════════════════════════════════════════════════════════════════════════

async def poll_results(
    chat_id: int,
    status_message,          # telegram.Message object to edit
    pattern: str,
    mode: str,
    result_queue: Queue,
    worker_attempts: dict,
    start_time: float,
    num_workers: int,
) -> None:
    """
    Asyncio task that runs while workers are searching.
    Drains the result queue, updates the Telegram status message periodically,
    and sends the final result when a match is found.
    """
    last_edit = time.monotonic()
    found     = None

    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)

            # ── Drain everything currently in the queue ───────────────────────
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

            # ── Match found → send result and clean up ────────────────────────
            if found:
                address, private_key = found
                kill_job(chat_id)

                display = f"0x{pattern}…" if mode == "prefix" else f"0x…{pattern}"
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "📋 Copy Address",
                        callback_data=f"copy:{address}"
                    )],
                    [InlineKeyboardButton(
                        "🔗 View on Etherscan",
                        url=f"https://etherscan.io/address/{address}"
                    )],
                ])

                result_text = (
                    "✅ *Vanity Address Found\\!*\n\n"
                    f"🎯 *Pattern:* `{esc(display)}`\n\n"
                    f"📬 *Address:*\n`{address}`\n\n"
                    f"🔑 *Private Key:*\n`{private_key}`\n\n"
                    "─────────────────────────\n"
                    f"🔁 *Attempts:* {esc(f'{total:,}')}\n"
                    f"⏱ *Time:* {esc(f'{elapsed:.1f}s')}\n"
                    f"⚡ *Speed:* {esc(f'{rate:,}')} addr/s\n\n"
                    "⚠️ _Keep your private key secret\\. Never share it\\._"
                )

                await status_message.edit_text(
                    result_text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=keyboard,
                )
                return

            # ── Periodic progress edit ────────────────────────────────────────
            now = time.monotonic()
            if now - last_edit >= EDIT_INTERVAL:
                last_edit = now
                progress_text = (
                    f"🔄 *Searching for vanity address…*\n\n"
                    f"🎯 *Pattern:* `{esc(f'0x{pattern}…' if mode == 'prefix' else f'0x…{pattern}')}`\n\n"
                    f"🔁 Attempts: *{esc(f'{total:,}')}*\n"
                    f"⚡ Speed: *{esc(f'{rate:,}')}* addr/s\n"
                    f"⏱ Elapsed: *{esc(f'{elapsed:.0f}s')}*\n\n"
                    f"_Use /cancel to stop the search\\._"
                )
                try:
                    await status_message.edit_text(
                        progress_text,
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
                except Exception:
                    pass   # ignore edit conflicts

    except asyncio.CancelledError:
        # /cancel was called
        logger.info("Poll task cancelled for chat %s", chat_id)
        raise


# ══════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start."""
    text = (
        "🔐 *Vanity Ethereum Address Generator*\n\n"
        "Generate an Ethereum address that starts or ends with your chosen pattern\\.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 *Commands*\n\n"
        "`/vanity prefix <pattern>`\n"
        "_Find an address starting with your pattern_\n\n"
        "`/vanity suffix <pattern>`\n"
        "_Find an address ending with your pattern_\n\n"
        "`/cancel`\n"
        "_Stop an in\\-progress search_\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 *Examples*\n\n"
        "`/vanity prefix abc`\n"
        "`/vanity suffix dead`\n"
        "`/vanity prefix 0xdead`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *Difficulty Guide*\n\n"
        "3 chars → \\~4K attempts \\(instant\\)\n"
        "4 chars → \\~65K attempts \\(seconds\\)\n"
        "5 chars → \\~1M attempts \\(1\\-2 min\\)\n"
        "6 chars → \\~16M attempts \\(30\\-60 min\\)\n\n"
        "_Each extra character is 16× harder\\._"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_vanity(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /vanity prefix <pattern> and /vanity suffix <pattern>.

    Flow:
    1. Parse and validate arguments.
    2. Kill any existing job for this chat.
    3. Send a status message.
    4. Spawn worker processes.
    5. Launch background polling task.
    """
    chat_id = update.effective_chat.id
    args    = ctx.args  # words after /vanity

    # ── Argument validation ───────────────────────────────────────────────────
    if not args or len(args) < 2:
        await update.message.reply_text(
            "⚠️ *Usage:*\n\n"
            "`/vanity prefix <pattern>`\n"
            "`/vanity suffix <pattern>`\n\n"
            "*Examples:*\n"
            "`/vanity prefix abc`\n"
            "`/vanity suffix dead`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    mode_input = args[0].lower()
    if mode_input not in ("prefix", "suffix"):
        await update.message.reply_text(
            "❌ Mode must be `prefix` or `suffix`\\.\n\n"
            "Example: `/vanity prefix abc`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    pattern, error = normalise_pattern(args[1])
    if error:
        await update.message.reply_text(
            f"❌ *Invalid pattern:* {error}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    mode = mode_input

    # ── Cancel any running job for this chat ──────────────────────────────────
    if chat_id in active_jobs:
        kill_job(chat_id)
        await asyncio.sleep(0.3)

    # ── Send initial status message ───────────────────────────────────────────
    display   = f"0x{pattern}…" if mode == "prefix" else f"0x…{pattern}"
    expected  = estimate_attempts(len(pattern))
    warning   = difficulty_warning(len(pattern))

    status_msg = await update.message.reply_text(
        f"🔄 *Starting vanity search…*\n\n"
        f"🎯 Pattern: `{esc(display)}`\n"
        f"📊 Expected: \\~{esc(expected)} attempts\n"
        f"⚙️ Workers: {esc(str(NUM_WORKERS))}"
        f"{warning}\n\n"
        f"_Spinning up workers\\.\\.\\._",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # ── Spawn worker processes ────────────────────────────────────────────────
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

    logger.info(
        "Started %d workers for chat %s | pattern=%s mode=%s",
        NUM_WORKERS, chat_id, pattern, mode,
    )

    # ── Launch background polling task ────────────────────────────────────────
    task = asyncio.create_task(
        poll_results(
            chat_id=chat_id,
            status_message=status_msg,
            pattern=pattern,
            mode=mode,
            result_queue=result_queue,
            worker_attempts=worker_attempts,
            start_time=start_time,
            num_workers=NUM_WORKERS,
        )
    )

    # Store job state so /cancel can reach it
    active_jobs[chat_id] = {
        "processes":  processes,
        "stop_flag":  stop_flag,
        "task":       task,
    }


async def cmd_cancel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel — stops an active vanity search for this chat."""
    chat_id = update.effective_chat.id

    if chat_id not in active_jobs:
        await update.message.reply_text(
            "ℹ️ No active vanity search to cancel\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    kill_job(chat_id)
    await update.message.reply_text(
        "🛑 *Search cancelled\\.*\n\n"
        "Start a new one with `/vanity prefix <pattern>` or `/vanity suffix <pattern>`\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def handle_copy_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 📋 Copy Address inline button."""
    query = update.callback_query
    await query.answer()

    if query.data and query.data.startswith("copy:"):
        address = query.data.split("copy:", 1)[1]
        await query.message.reply_text(
            f"📋 *Tap and hold to copy:*\n\n`{address}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    logger.info("Starting Vanity ETH Address Bot with %d workers…", NUM_WORKERS)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("vanity", cmd_vanity))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_copy_callback, pattern=r"^copy:"))

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    multiprocessing.freeze_support()   # required on Windows
    main()
