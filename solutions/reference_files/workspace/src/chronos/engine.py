import asyncio
import contextvars
import hashlib
import json
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Dict, FrozenSet, Iterable, Tuple
from .models import (
    AccountNotFoundError,
    IdempotencyConflictError,
    InsufficientFundsError,
    InvalidAmountError,
    TransferReceipt,
    TransferRequest,
)

_HELD_ACCOUNT_LOCKS: contextvars.ContextVar[FrozenSet[str]] = contextvars.ContextVar(
    "_HELD_ACCOUNT_LOCKS", default=frozenset()
)


class KeyedLockRegistry:
    """
    Hands out one asyncio.Lock per key while any coroutine is using that key and
    forgets the key once the last user releases it. Memory therefore tracks
    in-flight work, not the number of distinct keys the process has ever seen.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, Tuple[asyncio.Lock, int]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    @asynccontextmanager
    async def acquire(self, key: str):
        lock, refs = self._entries.get(key, (None, 0))
        if lock is None:
            lock = asyncio.Lock()
        self._entries[key] = (lock, refs + 1)
        try:
            async with lock:
                yield
        finally:
            lock, refs = self._entries[key]
            if refs <= 1:
                del self._entries[key]
            else:
                self._entries[key] = (lock, refs - 1)


def shared_registry(db_pool, attr: str) -> KeyedLockRegistry:
    """One registry per DB instance, so every engine, processor and worker on it agrees."""
    registry = getattr(db_pool, attr, None)
    if registry is None:
        registry = KeyedLockRegistry()
        setattr(db_pool, attr, registry)
    return registry


class LedgerEngine:
    """
    Asynchronous double-entry ledger engine with:
    - per-idempotency-key serialization and payload-hash conflict detection
    - canonical (sorted) per-account lock ordering, so AB-BA contention cannot deadlock
    - coroutine-local lock reentrancy, so atomic batches can call execute_transfer
    - reference-counted lock registries, so synchronization state stays bounded
    """

    def __init__(self, db_pool, simulated_io_delay_sec: float = 0.015) -> None:
        self.db = db_pool
        self.io_delay = simulated_io_delay_sec
        self._account_locks = shared_registry(db_pool, "_chronos_account_locks")
        self._idempotency_locks = shared_registry(db_pool, "_chronos_idempotency_locks")

    @asynccontextmanager
    async def acquire_canonical_account_locks(self, account_ids: Iterable[str]):
        currently_held = _HELD_ACCOUNT_LOCKS.get()
        needed = sorted(set(account_ids) - currently_held)
        async with AsyncExitStack() as stack:
            for acct_id in needed:
                await stack.enter_async_context(self._account_locks.acquire(acct_id))
            token = _HELD_ACCOUNT_LOCKS.set(currently_held | frozenset(needed))
            try:
                yield
            finally:
                _HELD_ACCOUNT_LOCKS.reset(token)

    async def execute_transfer(self, req: TransferRequest) -> TransferReceipt:
        self.validate_transfer_params(req.source_account, req.target_account, req.amount_cents)
        payload_hash = self._hash_request(req)

        async with self._idempotency_locks.acquire(req.idempotency_key):
            existing = await self.db.fetch_idempotency_record(req.idempotency_key)
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise IdempotencyConflictError(
                        f"Idempotency key '{req.idempotency_key}' reused with a different payload."
                    )
                return TransferReceipt(
                    transfer_id=existing["transfer_id"],
                    idempotency_key=req.idempotency_key,
                    source_account=req.source_account,
                    target_account=req.target_account,
                    amount_cents=existing["amount_cents"],
                    status="COMMITTED",
                )

            async with self.acquire_canonical_account_locks([req.source_account, req.target_account]):
                await asyncio.sleep(self.io_delay)

                source_bal = await self.db.get_balance(req.source_account)
                target_bal = await self.db.get_balance(req.target_account)
                if source_bal is None or target_bal is None:
                    raise AccountNotFoundError("One or both accounts do not exist.")
                if source_bal < req.amount_cents:
                    raise InsufficientFundsError(
                        f"Account {req.source_account} has {source_bal}, needs {req.amount_cents}"
                    )

                transfer_id = str(uuid.uuid4())
                await self.db.update_balance(req.source_account, source_bal - req.amount_cents)
                await self.db.update_balance(req.target_account, target_bal + req.amount_cents)
                await self.db.insert_ledger_entries(
                    transfer_id=transfer_id,
                    source=req.source_account,
                    target=req.target_account,
                    amount_cents=req.amount_cents,
                )
                await self.db.insert_outbox_event(
                    event_id=str(uuid.uuid4()),
                    aggregate_id=transfer_id,
                    payload=json.dumps(
                        {
                            "transfer_id": transfer_id,
                            "source": req.source_account,
                            "target": req.target_account,
                            "amount_cents": req.amount_cents,
                        }
                    ),
                )
                await self.db.save_idempotency_record(
                    idempotency_key=req.idempotency_key,
                    payload_hash=payload_hash,
                    transfer_id=transfer_id,
                    amount_cents=req.amount_cents,
                )

                return TransferReceipt(
                    transfer_id=transfer_id,
                    idempotency_key=req.idempotency_key,
                    source_account=req.source_account,
                    target_account=req.target_account,
                    amount_cents=req.amount_cents,
                    status="COMMITTED",
                )

    @staticmethod
    def validate_transfer_params(source_account: str, target_account: str, amount_cents: int) -> None:
        if amount_cents <= 0:
            raise InvalidAmountError("Transfer amount must be positive integer cents.")
        if source_account == target_account:
            raise InvalidAmountError("Source and target accounts must differ.")

    @staticmethod
    def _hash_request(req: TransferRequest) -> str:
        canonical = f"{req.source_account}:{req.target_account}:{req.amount_cents}"
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
