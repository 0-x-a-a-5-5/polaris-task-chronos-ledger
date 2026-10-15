import asyncio
import contextvars
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from typing import Dict, FrozenSet, Iterable
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


class LedgerEngine:
    """
    Production-grade asynchronous double-entry ledger engine with:
    - Per-idempotency-key serialization and payload hash conflict detection
    - Fine-grained canonical per-account lock ordering (AB-BA deadlock free)
    - Coroutine-local lock reentrancy for multi-hop atomic batches
    """

    def __init__(self, db_pool, simulated_io_delay_sec: float = 0.015) -> None:
        self.db = db_pool
        self.io_delay = simulated_io_delay_sec

        if not hasattr(self.db, "_chronos_acct_locks"):
            setattr(self.db, "_chronos_acct_locks", {})
        if not hasattr(self.db, "_chronos_idemp_locks"):
            setattr(self.db, "_chronos_idemp_locks", {})

    def _get_account_lock(self, account_id: str) -> asyncio.Lock:
        locks: Dict[str, asyncio.Lock] = getattr(self.db, "_chronos_acct_locks")
        if account_id not in locks:
            locks[account_id] = asyncio.Lock()
        return locks[account_id]

    def _get_idempotency_lock(self, idempotency_key: str) -> asyncio.Lock:
        locks: Dict[str, asyncio.Lock] = getattr(self.db, "_chronos_idemp_locks")
        if idempotency_key not in locks:
            locks[idempotency_key] = asyncio.Lock()
        return locks[idempotency_key]

    @asynccontextmanager
    async def acquire_canonical_account_locks(self, account_ids: Iterable[str]):
        currently_held = _HELD_ACCOUNT_LOCKS.get()
        needed = sorted(set(account_ids) - currently_held)
        acquired = []
        try:
            for acct_id in needed:
                lock = self._get_account_lock(acct_id)
                await lock.acquire()
                acquired.append(lock)
            token = _HELD_ACCOUNT_LOCKS.set(currently_held | set(needed))
            try:
                yield
            finally:
                _HELD_ACCOUNT_LOCKS.reset(token)
        finally:
            for lock in reversed(acquired):
                lock.release()

    async def execute_transfer(self, req: TransferRequest) -> TransferReceipt:
        self.validate_transfer_params(
            req.source_account, req.target_account, req.amount_cents
        )
        payload_hash = self._hash_request(req)

        idemp_lock = self._get_idempotency_lock(req.idempotency_key)
        async with idemp_lock:
            existing = await self.db.fetch_idempotency_record(req.idempotency_key)
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise IdempotencyConflictError(
                        f"Idempotency key '{req.idempotency_key}' reused with mutated payload."
                    )
                return TransferReceipt(
                    transfer_id=existing["transfer_id"],
                    idempotency_key=req.idempotency_key,
                    source_account=req.source_account,
                    target_account=req.target_account,
                    amount_cents=existing["amount_cents"],
                    status="COMMITTED",
                )

            async with self.acquire_canonical_account_locks(
                (req.source_account, req.target_account)
            ):
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

                await self.db.update_balance(
                    req.source_account, source_bal - req.amount_cents
                )
                await self.db.update_balance(
                    req.target_account, target_bal + req.amount_cents
                )

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
    def validate_transfer_params(
        source_account: str, target_account: str, amount_cents: int
    ) -> None:
        if amount_cents <= 0:
            raise InvalidAmountError("Transfer amount must be positive integer cents.")
        if source_account == target_account:
            raise InvalidAmountError("Source and target accounts must differ.")

    @staticmethod
    def _hash_request(req: TransferRequest) -> str:
        canonical = f"{req.source_account}:{req.target_account}:{req.amount_cents}"
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
