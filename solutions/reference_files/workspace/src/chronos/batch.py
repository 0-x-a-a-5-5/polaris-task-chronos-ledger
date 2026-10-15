import hashlib
import json
from typing import Dict, List
from .config import MAX_BATCH_LEGS
from .engine import shared_registry
from .models import (
    AccountNotFoundError,
    BatchResult,
    IdempotencyConflictError,
    InsufficientFundsError,
    InvalidAmountError,
    TransferReceipt,
    TransferRequest,
    TransferSpec,
)


class BatchTransferProcessor:
    """
    All-or-nothing multi-hop batches. Solvency of every leg is proven against
    simulated running balances under canonical account locks before any leg is
    committed, so a failing leg leaves no balance change, ledger entry or outbox
    event behind. Results are persisted per batch_id so replays return the
    identical BatchResult and mutated replays raise IdempotencyConflictError.
    """

    def __init__(self, engine) -> None:
        self.engine = engine
        self.db = engine.db
        self._batch_locks = shared_registry(self.db, "_chronos_batch_locks")

    @staticmethod
    def _hash_batch(transfers: List[TransferSpec]) -> str:
        canonical = "|".join(f"{t.source_account}:{t.target_account}:{t.amount_cents}" for t in transfers)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _serialize(result: BatchResult) -> str:
        return json.dumps(
            {
                "batch_id": result.batch_id,
                "status": result.status,
                "receipts": [
                    {
                        "transfer_id": r.transfer_id,
                        "idempotency_key": r.idempotency_key,
                        "source_account": r.source_account,
                        "target_account": r.target_account,
                        "amount_cents": r.amount_cents,
                        "status": r.status,
                    }
                    for r in result.receipts
                ],
            }
        )

    @staticmethod
    def _deserialize(result_json: str) -> BatchResult:
        data = json.loads(result_json)
        return BatchResult(
            batch_id=data["batch_id"],
            status=data["status"],
            receipts=[TransferReceipt(**r) for r in data["receipts"]],
        )

    async def execute_batch(self, batch_id: str, transfers: List[TransferSpec]) -> BatchResult:
        if len(transfers) > MAX_BATCH_LEGS:
            raise InvalidAmountError(f"Batch exceeds the maximum of {MAX_BATCH_LEGS} legs.")
        for spec in transfers:
            self.engine.validate_transfer_params(spec.source_account, spec.target_account, spec.amount_cents)
        payload_hash = self._hash_batch(transfers)

        async with self._batch_locks.acquire(batch_id):
            existing = await self.db.fetch_batch_record(batch_id)
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise IdempotencyConflictError(f"Batch '{batch_id}' reused with different transfer legs.")
                return self._deserialize(existing["result_json"])

            if not transfers:
                empty = BatchResult(batch_id=batch_id, status="COMMITTED", receipts=[])
                await self.db.save_batch_record(batch_id, payload_hash, self._serialize(empty))
                return empty

            all_accounts = {acct for spec in transfers for acct in (spec.source_account, spec.target_account)}
            async with self.engine.acquire_canonical_account_locks(all_accounts):
                simulated: Dict[str, int] = {}
                for acct_id in sorted(all_accounts):
                    balance = await self.db.get_balance(acct_id)
                    if balance is None:
                        raise AccountNotFoundError(f"Account '{acct_id}' does not exist.")
                    simulated[acct_id] = balance

                for idx, spec in enumerate(transfers):
                    leg_req = TransferRequest(
                        f"{batch_id}:leg:{idx}", spec.source_account, spec.target_account, spec.amount_cents
                    )
                    existing_leg = await self.db.fetch_idempotency_record(leg_req.idempotency_key)
                    if existing_leg is not None and existing_leg["payload_hash"] != self.engine._hash_request(leg_req):
                        raise IdempotencyConflictError(
                            f"Batch leg key '{leg_req.idempotency_key}' conflicts with an existing record."
                        )
                    if simulated[spec.source_account] < spec.amount_cents:
                        raise InsufficientFundsError(
                            f"Batch '{batch_id}' leg {idx}: account '{spec.source_account}' has "
                            f"{simulated[spec.source_account]}, needs {spec.amount_cents}."
                        )
                    simulated[spec.source_account] -= spec.amount_cents
                    simulated[spec.target_account] += spec.amount_cents

                receipts: List[TransferReceipt] = []
                for idx, spec in enumerate(transfers):
                    receipts.append(
                        await self.engine.execute_transfer(
                            TransferRequest(
                                f"{batch_id}:leg:{idx}", spec.source_account, spec.target_account, spec.amount_cents
                            )
                        )
                    )

                result = BatchResult(batch_id=batch_id, status="COMMITTED", receipts=receipts)
                await self.db.save_batch_record(batch_id, payload_hash, self._serialize(result))
                return result
