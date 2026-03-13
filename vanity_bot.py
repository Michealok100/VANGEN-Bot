"""
vanity_worker.py — Vanity address worker (runs in a separate process).

HOW ETHEREUM ADDRESSES ARE DERIVED:
1. Generate secure random 32-byte private key
2. secp256k1 elliptic curve multiplication → 64-byte public key
3. Keccak-256 hash the public key
4. Take last 20 bytes → prefix 0x → Ethereum address
"""

import secrets
from multiprocessing import Queue, Value

from eth_account import Account

REPORT_EVERY = 5_000


def _matches(address: str, pattern: str, mode: str) -> bool:
    body = address[2:].lower()
    return body.startswith(pattern) if mode == "prefix" else body.endswith(pattern)


def worker(
    worker_id: int,
    pattern: str,
    mode: str,
    result_queue: Queue,
    stop_flag: Value,
) -> None:
    attempts = 0

    while not stop_flag.value:
        private_key_bytes = secrets.token_bytes(32)
        private_key_hex   = "0x" + private_key_bytes.hex()
        account           = Account.from_key(private_key_hex)
        address           = account.address
        attempts         += 1

        if _matches(address, pattern, mode):
            stop_flag.value = 1
            result_queue.put(("found", worker_id, attempts, address, private_key_hex))
            return

        if attempts % REPORT_EVERY == 0:
            result_queue.put(("progress", worker_id, attempts))

    result_queue.put(("progress", worker_id, attempts))
