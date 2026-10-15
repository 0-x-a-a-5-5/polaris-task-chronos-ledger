# Engineering Task: Chronos Ledger — High-Concurrency Integrity & Atomic Multi-Hop Batches

## Context
`chronos-ledger` is our core asynchronous double-entry ledger service. It processes balance transfers between accounts, enforces idempotency via client-supplied `idempotency_key` headers, and writes domain events to an transactional outbox table (`outbox_events`) which `OutboxRelayWorker` asynchronously dispatches to downstream webhook consumers.

All existing unit tests in `tests/test_public_ledger.py` currently pass. However, following Tuesday's traffic spike, production incident response identified two critical reliability failures under concurrent load, alongside a blocking requirement for our Q4 settlement pipeline.

---

## Part 1: Production Incident Remediation

### Incident A — Duplicate Debits Under Client Retry Storms
When upstream API gateways time out and issue parallel retries for the same `TransferRequest` (identical `idempotency_key`, `source_account`, `target_account`, and `amount_cents`) within a 5–20ms window:
1. Multiple concurrent coroutines occasionally bypass the idempotency check in `LedgerEngine.execute_transfer()`, resulting in double or triple debits from the source account.
2. Under high concurrency on the same account pair, `source_account` balances occasionally drift from the sum of committed `ledger_entries`, violating double-entry conservation of funds (`SUM(credits) - SUM(debits) == 0`).
3. If a second request arrives with an **existing** `idempotency_key` but a **mutated payload** (e.g., different `amount_cents` or `target_account`), the engine must deterministically raise `IdempotencyConflictError` rather than returning the cached receipt or executing the new transfer.

### Incident B — Outbox Relay Poison-Pill Stalls & Duplicate Dispatch
`OutboxRelayWorker.run_tick()` polls pending outbox events and dispatches them via an injected async `WebhookTransport`.
1. **Head-of-Line Poison Pill:** When `WebhookTransport.send()` raises `PermanentWebhookError` (e.g., HTTP 422 schema rejection), the worker fails the entire batch tick, leaving healthy events behind it stuck in `PENDING` status indefinitely. Events raising `PermanentWebhookError` must be immediately transitioned to `DEAD_LETTER` with their error recorded, allowing remaining events in the batch to proceed.
2. **Transient Backoff vs. Duplicate Send:** When `WebhookTransport.send()` raises `TransientWebhookError` (e.g., HTTP 503), the event's `attempts` counter must increment and transition back to `PENDING` (or `DEAD_LETTER` once `attempts >= max_attempts`). Currently, concurrent worker instances polling `run_tick()` at the same time can pick up and dispatch the same event twice.
3. **Stuck Claims After Worker Death:** A worker that claims events and then hangs or dies (network black hole, OOM-kill mid-dispatch) must not strand them. Claims carry a lease: `OutboxRelayWorker(db, transport, max_attempts=3, batch_size=20, claim_lease_sec=DEFAULT_OUTBOX_CLAIM_LEASE_SEC)`. An event whose claim has expired is eligible for any worker again; an event whose claim has not expired must never be re-dispatched.

---

## Part 2: Feature Specification — Atomic Multi-Hop Batch Transfers

Implement `BatchTransferProcessor.execute_batch(batch_id: str, transfers: list[TransferSpec]) -> BatchResult` in `src/chronos/batch.py` and wire it into `LedgerEngine`:
1. **All-or-Nothing Atomicity:** A batch contains 1 to 25 ordered `TransferSpec` legs. Because an earlier leg in the batch may fund an account needed by a subsequent leg in the same batch, legs must be evaluated sequentially within the batch's transactional scope. If *any* leg fails (e.g., `InsufficientFundsError`, `AccountNotFoundError`, or `InvalidAmountError`), the **entire batch** must roll back cleanly—zero balance mutations, zero ledger entries, and zero outbox events persisted.
2. **Deadlock Freedom Under Cross-Batch Contention:** In production, Worker 1 may execute a batch transferring `Account_A -> Account_B` at the exact moment Worker 2 executes a batch transferring `Account_B -> Account_A`. Your implementation must never deadlock or raise unhandled lock-contention failures under concurrent cross-account batches.
3. **Batch-Level Idempotency:** Calling `execute_batch` multiple times (sequentially or concurrently) with the same `batch_id` and identical `transfers` list must execute the batch at most once and return the identical `BatchResult`. Reusing a `batch_id` with a mutated transfers list must raise `IdempotencyConflictError`.

---

## Non-Functional & Verification Requirements
* **Throughput Invariant:** Do **not** serialize the entire engine behind a single coarse global lock. Unrelated transfers (touching disjoint account pairs) must execute concurrently. Our benchmark requires 100 concurrent transfers across disjoint account pairs (with a simulated 15ms I/O delay per transaction) to complete in under **0.35 seconds** wall-clock time for full credit.
* **Bounded Synchronization State:** Per-key and per-account synchronization objects must be released once no operation is using them. A fixed-size striped pool or a reference-counted registry both qualify; a dictionary that keeps one `asyncio.Lock` per key forever does not. The benchmark runs 5,000 transfers across 10,000 distinct accounts and then requires fewer than 512 live asyncio synchronization primitives and fewer than 5,000 retained Python objects.
* **Diagnostic Reproduction Suite:** In `tests/test_reproduction.py`, write at least three deterministic async tests that fail against the initial codebase and pass after your fix:
  1. `test_concurrent_idempotency_retry_storm`
  2. `test_outbox_poison_pill_and_concurrent_workers`
  3. `test_concurrent_opposing_batches_atomic_rollback`

  You may add further tests; the whole file must fail on the initial codebase and pass after your fix.
* **Constraint:** Do not modify existing test assertions in `tests/test_public_ledger.py` or change public class/method signatures in `models.py`. The grading harness reads `event_id`, `status`, `attempts`, and `last_error` from `outbox_events` through `InMemoryAsyncLedgerDB._conn`; keep that attribute and those columns (adding columns is fine).
