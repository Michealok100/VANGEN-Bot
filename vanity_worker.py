"""
vanity_worker.py — Vanity address worker using threads (no multiprocessing).

coincurve and pysha3 are C extensions that release Python's GIL,
so threads genuinely run in parallel for this workload.
This works in all environments including Railway containers.

Queue messages:
  ("progress", worker_id, attempt_count)
  ("found",    worker_id, attempt_count, address, private_key_hex)
"""

import os
import threading
from queue import Queue

REPORT_EVERY = 100_000


# ── Pick fastest available library ───────────────────────────────────────────

def _build_generator():
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

        print("[vanity_worker] Generator: coincurve + pycryptodome ✓ (fast)", flush=True)
        return _gen

    except ImportError:
        pass

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

        print("[vanity_worker] ⚠ Generator: eth_account (SLOW). Add coincurve + pycryptodome to requirements.txt", flush=True)
        return _gen

    except ImportError:
        pass

    raise RuntimeError("No crypto library found. Add coincurve and pycryptodome to requirements.txt")


_generate = _build_generator()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _matches(address: str, prefix: str, suffix: str) -> bool:
    body = address[2:].lower()
    return body.startswith(prefix) and body.endswith(suffix)


# ── Worker function (runs in a thread) ───────────────────────────────────────

def worker(
    worker_id: int,
    prefix: str,
    suffix: str,
    result_queue: Queue,
    stop_event: threading.Event,
) -> None:
    attempts  = 0
    prefix_len = len(prefix)
    suffix_len = len(suffix)

    while not stop_event.is_set():
        # Process in batches to reduce stop_event check overhead
        for _ in range(REPORT_EVERY):
            address, private_key_hex = _generate()
            body = address[2:].lower()
            if body[:prefix_len] == prefix and body[-suffix_len:] == suffix:
                result_queue.put(("found", worker_id, attempts, address, private_key_hex))
                stop_event.set()
                return
        attempts += REPORT_EVERY
        result_queue.put(("progress", worker_id, REPORT_EVERY))

    result_queue.put(("progress", worker_id, attempts % REPORT_EVERY))
