import asyncio
import hashlib
import json
import uuid
from typing import Dict, Optional
from .models import (
    AccountNotFoundError,
    IdempotencyConflictError,
    InsufficientFundsError,
    InvalidAmountError,
    TransferReceipt,
    TransferRequest,
)


class LedgerEngine:
    """
    In-memory + SQLite WAL backed double-entry ledger engine.
    WARNING: Contains subtle concurrency defects for Polaris evaluation.
    """

    def __init__(self, db_pool, simulated_io_delay_sec: float = 0.015):
        self.db = db_pool
        self.io_delay = simulated_io_delay_sec

    async def execute_transfer(self, req: TransferRequest) -> TransferReceipt:
        if req.amount_cents <= 0:
            raise InvalidAmountError("Transfer amount must be positive integer cents.")
        if req.source_account == req.target_account:
            raise InvalidAmountError("Source and target accounts must differ.")

        payload_hash = self._hash_request(req)

        existing = await self.db.fetch_idempotency_record(req.idempotency_key)
        if existing is not None:
            return TransferReceipt(
                transfer_id=existing["transfer_id"],
                idempotency_key=req.idempotency_key,
                source_account=req.source_account,
                target_account=req.target_account,
                amount_cents=existing["amount_cents"],
                status="COMMITTED",
            )

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
            payload=json.dumps({
                "transfer_id": transfer_id,
                "source": req.source_account,
                "target": req.target_account,
                "amount_cents": req.amount_cents,
            }),
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
    def _hash_request(req: TransferRequest) -> str:
        canonical = f"{req.source_account}:{req.target_account}:{req.amount_cents}"
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
