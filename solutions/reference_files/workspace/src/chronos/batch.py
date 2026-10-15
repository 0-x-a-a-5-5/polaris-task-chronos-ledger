import asyncio
import hashlib
import json
from typing import Dict, List, Optional
from .models import (
    AccountNotFoundError,
    BatchResult,
    IdempotencyConflictError,
    InsufficientFundsError,
    TransferReceipt,
    TransferRequest,
    TransferSpec,
)


class BatchTransferProcessor:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.db = engine.db

        if not hasattr(self.db, "_chronos_batch_locks"):
            setattr(self.db, "_chronos_batch_locks", {})
        if not hasattr(self.db, "_chronos_batch_records"):
            setattr(self.db, "_chronos_batch_records", {})

    def _get_batch_lock(self, batch_id: str) -> asyncio.Lock:
        locks: Dict[str, asyncio.Lock] = getattr(self.db, "_chronos_batch_locks")
        if batch_id not in locks:
            locks[batch_id] = asyncio.Lock()
        return locks[batch_id]

    async def _fetch_batch_record(self, batch_id: str) -> Optional[Dict[str, str]]:
        await asyncio.sleep(0)
        records: Dict[str, Dict[str, str]] = getattr(self.db, "_chronos_batch_records")
        return records.get(batch_id)

    async def _save_batch_record(
        self, batch_id: str, payload_hash: str, result: BatchResult
    ) -> None:
        await asyncio.sleep(0)
        records: Dict[str, Dict[str, str]] = getattr(self.db, "_chronos_batch_records")
        serialized = json.dumps(
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
        records[batch_id] = {
            "payload_hash": payload_hash,
            "result_json": serialized,
        }

    @staticmethod
    def _deserialize_batch_result(result_json: str) -> BatchResult:
        data = json.loads(result_json)
        receipts = [TransferReceipt(**r) for r in data["receipts"]]
        return BatchResult(
            batch_id=data["batch_id"],
            status=data["status"],
            receipts=receipts,
        )

    @staticmethod
    def _hash_batch(transfers: List[TransferSpec]) -> str:
        canonical = "|".join(
            f"{t.source_account}:{t.target_account}:{t.amount_cents}"
            for t in transfers
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def execute_batch(
        self, batch_id: str, transfers: List[TransferSpec]
    ) -> BatchResult:
        for spec in transfers:
            self.engine.validate_transfer_params(
                spec.source_account, spec.target_account, spec.amount_cents
            )

        payload_hash = self._hash_batch(transfers)
        batch_lock = self._get_batch_lock(batch_id)

        async with batch_lock:
            existing_batch = await self._fetch_batch_record(batch_id)
            if existing_batch is not None:
                if existing_batch["payload_hash"] != payload_hash:
                    raise IdempotencyConflictError(
                        f"Batch ID '{batch_id}' reused with different transfer legs."
                    )
                return self._deserialize_batch_result(existing_batch["result_json"])

            if not transfers:
                empty_res = BatchResult(
                    batch_id=batch_id, status="COMMITTED", receipts=[]
                )
                await self._save_batch_record(batch_id, payload_hash, empty_res)
                return empty_res

            all_accounts = {
                acct
                for spec in transfers
                for acct in (spec.source_account, spec.target_account)
            }

            async with self.engine.acquire_canonical_account_locks(all_accounts):
                simulated_balances: Dict[str, int] = {}
                for acct_id in sorted(all_accounts):
                    bal = await self.db.get_balance(acct_id)
                    if bal is None:
                        raise AccountNotFoundError(
                            f"Account '{acct_id}' does not exist."
                        )
                    simulated_balances[acct_id] = bal

                for idx, spec in enumerate(transfers):
                    leg_key = f"{batch_id}:leg:{idx}"
                    leg_req = TransferRequest(
                        idempotency_key=leg_key,
                        source_account=spec.source_account,
                        target_account=spec.target_account,
                        amount_cents=spec.amount_cents,
                    )
                    existing_leg = await self.db.fetch_idempotency_record(leg_key)
                    if existing_leg is not None:
                        expected_hash = self.engine._hash_request(leg_req)
                        if existing_leg["payload_hash"] != expected_hash:
                            raise IdempotencyConflictError(
                                f"Batch leg key '{leg_key}' conflicts with existing record."
                            )

                    if simulated_balances[spec.source_account] < spec.amount_cents:
                        raise InsufficientFundsError(
                            f"Batch '{batch_id}' leg {idx} failed: account "
                            f"'{spec.source_account}' has {simulated_balances[spec.source_account]}, "
                            f"needs {spec.amount_cents}."
                        )
                    simulated_balances[spec.source_account] -= spec.amount_cents
                    simulated_balances[spec.target_account] += spec.amount_cents

                receipts: List[TransferReceipt] = []
                for idx, spec in enumerate(transfers):
                    leg_req = TransferRequest(
                        idempotency_key=f"{batch_id}:leg:{idx}",
                        source_account=spec.source_account,
                        target_account=spec.target_account,
                        amount_cents=spec.amount_cents,
                    )
                    receipt = await self.engine.execute_transfer(leg_req)
                    receipts.append(receipt)

                result = BatchResult(
                    batch_id=batch_id, status="COMMITTED", receipts=receipts
                )
                await self._save_batch_record(batch_id, payload_hash, result)
                return result
