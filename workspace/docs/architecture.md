# Chronos Ledger Internal Architecture Notes

## Storage Model
`InMemoryAsyncLedgerDB` wraps an in-memory SQLite instance (`:memory:`) and yields to the asyncio event loop on every read/write operation.

## Known Operational Assumptions (Legacy v0.1)
* Originally designed for a single-coroutine event consumer.
* `OutboxRelayWorker` assumes only one worker process polls `outbox_events` at a time.
* `BatchTransferProcessor` currently iterates over `TransferSpec` legs and calls `LedgerEngine.execute_transfer` directly without pre-validating downstream legs.
* Outbox claims carry no lease: a worker that hangs or dies after claiming events strands them.
