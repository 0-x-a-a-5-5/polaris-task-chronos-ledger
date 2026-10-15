from typing import List
from .models import BatchResult, TransferReceipt, TransferRequest, TransferSpec


class BatchTransferProcessor:
    """
    Processes multi-hop transfer batches.
    WARNING: Starter implementation is non-atomic and lacks deadlock prevention
    and batch-level idempotency.
    """

    def __init__(self, engine) -> None:
        self.engine = engine

    async def execute_batch(
        self, batch_id: str, transfers: List[TransferSpec]
    ) -> BatchResult:
        if not transfers:
            return BatchResult(batch_id=batch_id, status="COMMITTED", receipts=[])

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

        return BatchResult(batch_id=batch_id, status="COMMITTED", receipts=receipts)
