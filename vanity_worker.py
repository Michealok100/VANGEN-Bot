"""
vanity_worker.py — fast address worker using coincurve + pysha3.

Speed:
  coincurve + pysha3       ~18,000 addr/s per core  ← target
  coincurve + pycryptodome  ~8,000 addr/s per core
  eth_account (fallback)      ~500 addr/s per core  ← what you had before

Queue messages (unchanged interface):
  ("progress", worker_id, attempt_count)
  ("found",    worker_id, attempt_count, address, private_key_hex)
"""

import os
from multiprocessing import Queue, Value

# How often each worker reports progress back to the bot
REPORT_EVERY = 1_000


# ── Pick fastest available library ───────────────────────────────────────────

def _build_generator():
    # Option 1: coincurve + pysha3 (fastest — both are C extensions that release the GIL)
    try:
        from coincurve import PublicKey
        from sha3 import keccak_256

        def _gen():
            while True:
                priv = os.urandom(32)
                try:
                    pub  = PublicKey.from_valid_secret(priv).format(compressed=False)[1:]
                    addr = "0x" + keccak_256(pub).digest()[-20:].hex()
                    return addr, "0x" + priv.hex()
                except Exception:
                    continue

        print("[vanity_worker] Generator: coincurve + pysha3 ✓ (fast)")
        return _gen

    except ImportError:
        pass

    # Option 2: coincurve + pycryptodome
    try:
        from coincurve import PublicKey
        from Crypto.Hash import keccak as _kmod

        def _gen():
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

        print("[vanity_worker] Generator: coincurve + pycryptodome (medium)")
        return _gen

    except ImportError:
        pass

    # Option 3: eth_account fallback (slow — warns loudly)
    try:
        import secrets
        from eth_account import Account

        def _gen():
            while True:
                priv = secrets.token_bytes(32)
                try:
                    key  = "0x" + priv.hex()
                    addr = Account.from_key(key).address
                    return addr, key
                except Exception:
                    continue

        print("[vanity_worker] ⚠ Generator: eth_account (SLOW). Add pysha3 to requirements.txt")
        return _gen

    except ImportError:
        pass

    raise RuntimeError(
        "No crypto library found! "
        "Add to requirements.txt: coincurve, pysha3"
    )


_generate = _build_generator()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _matches(address: str, pattern: str, mode: str) -> bool:
    body = address[2:].lower()
    return body.startswith(pattern) if mode == "prefix" else body.endswith(pattern)


# ── Worker (drop-in replacement — same signature as before) ──────────────────

def worker(
    worker_id: int,
    pattern: str,
    mode: str,
    result_queue: Queue,
    stop_flag: Value,
) -> None:
    attempts = 0

    while not stop_flag.value:
        address, private_key_hex = _generate()
        attempts += 1

        if _matches(address, pattern, mode):
            stop_flag.value = 1
            result_queue.put(("found", worker_id, attempts, address, private_key_hex))
            return

        if attempts % REPORT_EVERY == 0:
            result_queue.put(("progress", worker_id, attempts))

    result_queue.put(("progress", worker_id, attempts))
