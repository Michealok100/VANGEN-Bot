"""
generator.py — Vanity address worker (runs in a separate process).

HOW ETHEREUM ADDRESSES ARE DERIVED FROM PRIVATE KEYS:
======================================================
1. Generate a cryptographically secure random 32-byte private key.
2. Multiply the secp256k1 generator point by the private key integer
   to get a 64-byte uncompressed public key.
3. Keccak-256 hash the public key.
4. Take the last 20 bytes → prefix "0x" → Ethereum address.

eth-account handles steps 2-4 internally.
"""

import secrets
from multiprocessing import Queue, Value

from eth_account import Account

# How often each worker reports progress back to the bot
REPORT_EVERY = 5_000


def _matches(address: str, pattern: str, mode: str) -> bool:
    """Return True if address satisfies the vanity pattern."""
    body = address[2:].lower()   # strip "0x", lowercase
    return body.startswith(pattern) if mode == "prefix" else body.endswith(pattern)


def worker(
    worker_id: int,
    pattern: str,
    mode: str,
    result_queue: Queue,
    stop_flag: Value,
) -> None:
    """
    Runs in its own process. Generates random private keys and checks
    each derived Ethereum address against the pattern.

    Puts one of these onto result_queue:
      ("progress", worker_id, attempt_count)   — every REPORT_EVERY attempts
      ("found",    worker_id, attempt_count, address, private_key_hex)
    """
    attempts = 0

    while not stop_flag.value:
        # ── Generate secure random 32-byte private key ────────────────────────
        private_key_bytes = secrets.token_bytes(32)
        private_key_hex   = "0x" + private_key_bytes.hex()

        # ── Derive Ethereum address (secp256k1 → Keccak-256 → last 20 bytes) ─
        account = Account.from_key(private_key_hex)
        address = account.address   # EIP-55 checksummed

        attempts += 1

        # ── Check match ───────────────────────────────────────────────────────
        if _matches(address, pattern, mode):
            stop_flag.value = 1
            result_queue.put(("found", worker_id, attempts, address, private_key_hex))
            return

        # ── Periodic progress ping ────────────────────────────────────────────
        if attempts % REPORT_EVERY == 0:
            result_queue.put(("progress", worker_id, attempts))

    # Stopped by another worker finding the answer first
    result_queue.put(("progress", worker_id, attempts))
