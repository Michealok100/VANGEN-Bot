"""
vanity_worker.py — Vanity address worker (runs in a separate process).

HOW ETHEREUM ADDRESSES ARE DERIVED FROM PRIVATE KEYS:
======================================================
1. Generate a cryptographically secure random 32-byte private key.
2. Multiply the secp256k1 generator point by the private key integer
   to get a 64-byte uncompressed public key (coincurve handles this).
3. Keccak-256 hash the public key (pysha3 handles this).
4. Take the last 20 bytes → prefix "0x" → Ethereum address.

Library priority (fastest → slowest):
  1. coincurve + pysha3       — C extensions, release the GIL, ~18k addr/s/core
  2. coincurve + pycryptodome — C extensions, release the GIL, ~8k addr/s/core
  3. eth_account              — pure Python fallback, ~500 addr/s/core (slow)

Install the fast path:
  pip install coincurve --prefer-binary
  pip install pysha3
"""

import logging
import os
from multiprocessing import Queue, Value

logger = logging.getLogger(__name__)

# How often each worker reports progress back to the bot
REPORT_EVERY = 5_000


# ══════════════════════════════════════════════════════════════════════════════
#  GENERATOR — pick fastest available library at import time
# ══════════════════════════════════════════════════════════════════════════════

def _build_generator():
    # ── Option 1: coincurve + pysha3 (fastest, both release the GIL) ─────────
    try:
        from coincurve import PublicKey
        from sha3 import keccak_256

        def _gen() -> tuple[str, str]:
            while True:
                priv = os.urandom(32)
                try:
                    pub  = PublicKey.from_valid_secret(priv).format(compressed=False)[1:]
                    addr = "0x" + keccak_256(pub).digest()[-20:].hex()
                    return addr, "0x" + priv.hex()
                except Exception:
                    continue

        logger.info("vanity_worker: using coincurve + pysha3")
        return _gen

    except ImportError:
        pass

    # ── Option 2: coincurve + pycryptodome ───────────────────────────────────
    try:
        from coincurve import PublicKey
        from Crypto.Hash import keccak as _kmod

        def _gen() -> tuple[str, str]:
            while True:
                priv = os.urandom(32)
                try:
                    pub = PublicKey.from_valid_secret(priv).format(compressed=False)[1:]
                    k   = _kmod.new(digest_bits=256)
                    k.update(pub)
                    addr = "0x" + k.digest()[-20:].hex()
                    return addr, "0x" + priv.hex()
                except Exception:
                    continue

        logger.info("vanity_worker: using coincurve + pycryptodome")
        return _gen

    except ImportError:
        pass

    # ── Option 3: eth_account fallback ───────────────────────────────────────
    try:
        from eth_account import Account

        def _gen() -> tuple[str, str]:
            import secrets
            while True:
                priv = secrets.token_bytes(32)
                try:
                    key  = "0x" + priv.hex()
                    addr = Account.from_key(key).address
                    return addr, key
                except Exception:
                    continue

        logger.warning(
            "vanity_worker: falling back to eth_account (slow). "
            "Run: pip install coincurve --prefer-binary pysha3"
        )
        return _gen

    except ImportError:
        pass

    raise RuntimeError(
        "No crypto library found. "
        "Run: pip install coincurve --prefer-binary pysha3"
    )


_generate = _build_generator()


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _matches(address: str, pattern: str, mode: str) -> bool:
    """Return True if address satisfies the vanity pattern."""
    body = address[2:].lower()   # strip "0x", lowercase
    return body.startswith(pattern) if mode == "prefix" else body.endswith(pattern)


# ══════════════════════════════════════════════════════════════════════════════
#  WORKER — same interface as before, drop-in replacement
# ══════════════════════════════════════════════════════════════════════════════

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
      ("progress", worker_id, attempt_count)
      ("found",    worker_id, attempt_count, address, private_key_hex)
    """
    attempts = 0

    while not stop_flag.value:
        address, private_key_hex = _generate()
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
