# Recorded agent trajectories

README section 5 makes claims about how coding agents fail on this task. This directory holds the evidence: one set of files per attempt, produced by `run_gemini_attempt.sh` and never edited by hand.

## Method

1. `run_gemini_attempt.sh <n>` copies `workspace/` fresh and commits that baseline in a throwaway git repo.
2. The agent runs in that directory with `prompt.md` passed verbatim as its only prompt, every tool call auto-approved, and a Python 3.11 + pytest environment first on `PATH` (the same Python the grader container uses). The run is bounded at 20 minutes of wall clock.
3. The agent's working tree is diffed against the baseline (`gemini_attempt_NN.diff`), then mounted unchanged into the evaluation container at `/eval/workspace` and graded (`gemini_attempt_NN_score.json`). `gemini_attempt_NN_meta.json` records agent, model, Python, start time, duration, and exit code.

## Rubric versions

Each score file names the rubric it was produced under (`rubric_version` in the JSON). Attempt 01 ran against the v1.0 prompt and grader and saturated them; rubric v1.1 (same day) added the claim-lease and bounded-state requirements to `prompt.md`, gated non-functional credit on correctness, and re-balanced the weights to sum to 1.00. Attempts from 02 onward ran against the v1.1 prompt. Where a workspace was re-graded under a later rubric, the file carries the rubric suffix (`_score_v1.1.json`).

## Attempt 01: Gemini 3.8 Flash (high) via Antigravity CLI 1.3.6, 2026-10-15

| | |
|---|---|
| Score | 1.00 / 1.00, integrity gate clean, all six dimensions at full credit |
| Wall clock | 13 min 47 s of agent time (826.6 s) |
| Tool calls | 27 shell commands, 20 file views, 9 file edits |
| Tokens | 657k input, 88k output, 68k thinking, 4.07M cache reads |
| Files changed | `engine.py`, `db.py`, `outbox.py`, `batch.py`, `tests/test_reproduction.py` |
| Re-graded under v1.1 | 0.85: lease recovery not implemented (`OutboxRelayWorker` rejects `claim_lease_sec`), and 15,051 lock objects stay alive after 5,000 transfers, so bounded state fails |

What the patch does:

- **Idempotency storm:** a per-key `asyncio.Lock` plus an in-flight map keyed by idempotency key, so retries wait for the first request and mutated payloads raise `IdempotencyConflictError` whether the original is in flight or already committed.
- **Balance drift and throughput:** per-account locks acquired in sorted order around the read-modify-write, so same-pair retries serialize while disjoint pairs stay parallel (100 disjoint transfers in 0.051 s under the grader).
- **Outbox:** a `claim_pending_outbox_events` method marks rows `IN_FLIGHT` under a mutex before dispatch, so concurrent workers never pick the same row; `PermanentWebhookError` goes straight to `DEAD_LETTER` and the tick continues.
- **Batches:** all accounts in the batch are locked in sorted order, every leg is evaluated against in-memory running balances first, and writes happen only after the whole batch validates. Batch results are persisted with a payload hash for replay and conflict detection.
- **Reproduction tests:** three tests that fail on the baseline and pass on the patch, verified by the grader's fail-to-pass check.

Residual observations the rubric does not grade: the lock dictionaries grow without bound, an unexpected transport exception is recorded and then re-raised so it still aborts the remainder of that tick, and in-flight conflict detection lives in process-local dictionaries, so it does not extend across processes.

## Attempt 02: Gemini 3.8 Flash (high) via Antigravity CLI 1.3.6, 2026-10-15, rubric v1.1

| | |
|---|---|
| Score | 1.00 / 1.00 on rubric v1.1, integrity gate clean |
| Wall clock | 10 min 22 s of agent time (621.6 s) |
| Tool calls | 31 shell commands, 13 file views, 7 file writes |
| Tokens | 280k input, 81k output, 58k thinking, 3.10M cache reads |
| Files changed | `engine.py`, `db.py`, `outbox.py`, `batch.py`, new `sync.py`, `tests/test_reproduction.py`, `docs/architecture.md` |

What the patch does: a reference-counted `KeyedLockRegistry` in a new `sync.py`, instantiated on the DB object so every engine and processor over the same database shares the same locks; sorted multi-key acquisition through an `AsyncExitStack`; outbox claims via a single `UPDATE ... RETURNING` with a lease expiry and a per-worker id; single transfers and whole batches persisted through one SQLite `BEGIN`/`COMMIT`/`ROLLBACK` transaction; batch legs pre-validated against running balances; and the architecture notes rewritten to describe the new design.

Transcript note: the CLI's final event reports `status: ERROR, "The stream was interrupted"`. The agent had already produced its completion summary (root causes, fixes, 7/7 tests passing) before the stream dropped, the process exited 0, and the workspace was complete, so this is recorded as a transport artifact rather than an agent failure.

Residual observations the rubric does not grade: `claim_outbox_events` relies on `UPDATE ... RETURNING`, which needs SQLite 3.35 or newer; `fetch_pending_outbox_events` was changed to also return lease-expired `CLAIMED` rows, which is sensible but a silent contract change for any caller expecting only `PENDING`.

## Attempt 03: Gemini 3.8 Flash (low) via Antigravity CLI 1.3.6, 2026-10-15, rubric v1.1

| | |
|---|---|
| Score | 1.00 / 1.00 on rubric v1.1, integrity gate clean |
| Wall clock | 4 min 4 s of agent time (244.2 s) |
| Tool calls | 15 shell commands, 36 file views, 5 file edits |
| Tokens | 241k input, 18k output, 0 thinking, 2.16M cache reads |
| Files changed | `engine.py`, `db.py`, `outbox.py`, `batch.py`, `tests/test_reproduction.py` |

What the patch does: a reference-counted `KeyedLockManager` on the engine (so idle keys are freed and the bounded-state check passes with zero live locks), per-key idempotency locks with a double-check after acquiring sorted account locks, a `claimed_at` column with a lease cutoff in the claim query (rows stay `PENDING` while leased, so a hung worker's claims expire and are recovered), batch idempotency persisted in `batch_idempotency`, and batches that validate every leg against working balances before writing directly to the DB.

Residual observations the rubric does not grade: the lock manager is per `LedgerEngine` instance, so two engines over the same database would not serialize with each other; `engine.py` imports `batch.py` at module level, a circular dependency that happens to resolve; and `time` is imported inside the claim method.

## Summary

| Attempt | Model / effort | Prompt + rubric | Score | Agent time |
|---|---|---|---|---|
| 01 | Gemini 3.8 Flash, high | v1.0 | 1.00 | 13 min 47 s |
| 01 re-graded | same patch | v1.1 grader | 0.85 | n/a |
| 02 | Gemini 3.8 Flash, high | v1.1 | 1.00 | 10 min 22 s |
| 03 | Gemini 3.8 Flash, low | v1.1 | 1.00 | 4 min 4 s |

Reading: once a requirement is written into `prompt.md`, this model family implements it at either effort level, and low effort does so faster with no thinking tokens. The v1.1 checks only discriminate against patches written before those requirements existed. At the current frontier the task therefore measures specification-following under concurrency rather than discovery, which is the finding the next rubric revision has to act on.
