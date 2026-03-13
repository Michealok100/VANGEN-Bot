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

REPORT_EVERY = 1_000


# ── Pick fastest available library ───────────────────────────────────────────

def _build_generator():
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

        print("[vanity_worker] Generator: coincurve + pysha3 ✓ (fast)", flush=True)
        return _gen

    except ImportError:
        pass

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

        print("[vanity_worker] Generator: coincurve + pycryptodome (medium)", flush=True)
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

        print("[vanity_worker] ⚠ Generator: eth_account (SLOW). Add pysha3 to requirements.txt", flush=True)
        return _gen

    except ImportError:
        pass

    raise RuntimeError("No crypto library found. Add coincurve and pysha3 to requirements.txt")


_generate = _build_generator()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _matches(address: str, pattern: str, mode: str) -> bool:
    body = address[2:].lower()
    return body.startswith(pattern) if mode == "prefix" else body.endswith(pattern)


# ── Worker function (runs in a thread) ───────────────────────────────────────

def worker(
    worker_id: int,
    pattern: str,
    mode: str,
    result_queue: Queue,
    stop_event: threading.Event,
) -> None:
    attempts = 0

    while not stop_event.is_set():
        address, private_key_hex = _generate()
        attempts += 1

        if _matches(address, pattern, mode):
            result_queue.put(("found", worker_id, attempts, address, private_key_hex))
            stop_event.set()
            return

        if attempts % REPORT_EVERY == 0:
            result_queue.put(("progress", worker_id, attempts))

    result_queue.put(("progress", worker_id, attempts))
