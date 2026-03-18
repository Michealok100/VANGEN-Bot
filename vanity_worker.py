"""
vanity_worker.py — Vanity address worker using multiprocessing.

Uses multiprocessing instead of threads to bypass Python's GIL entirely,
giving true parallelism on multi-core machines.

Queue messages:
  ("progress", worker_id, attempt_count)
  ("found",    worker_id, attempt_count, address, private_key_hex)
"""

import os
import multiprocessing

REPORT_EVERY = 25_000


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

        print("[vanity_worker] Generator: coincurve + pycryptodome (fast)", flush=True)
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

        print("[vanity_worker] WARNING: eth_account (SLOW). Add coincurve + pycryptodome.", flush=True)
        return _gen

    except ImportError:
        pass

    raise RuntimeError("No crypto library found.")


# ── Worker process entry point ────────────────────────────────────────────────

def _worker_process(worker_id, prefix, suffix, result_queue, stop_event):
    """Runs in its own process — bypasses GIL for true parallelism."""
    generate   = _build_generator()
    attempts   = 0
    prefix_len = len(prefix)
    suffix_len = len(suffix)

    while not stop_event.is_set():
        for i in range(REPORT_EVERY):
            address, private_key_hex = generate()
            body = address[2:].lower()
            if body[:prefix_len] == prefix and body[-suffix_len:] == suffix:
                result_queue.put(("found", worker_id, attempts + i + 1, address, private_key_hex))
                stop_event.set()
                return
        attempts += REPORT_EVERY
        result_queue.put(("progress", worker_id, REPORT_EVERY))

    result_queue.put(("progress", worker_id, attempts % REPORT_EVERY))


# ── Public API ────────────────────────────────────────────────────────────────

def worker(worker_id, prefix, suffix, result_queue, stop_event):
    """Called from vanity_bot.py — spawns a child process."""
    p = multiprocessing.Process(
        target=_worker_process,
        args=(worker_id, prefix, suffix, result_queue, stop_event),
        daemon=True,
    )
    p.start()
    p.join()
