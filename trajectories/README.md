# Recorded agent trajectories

README section 5 makes claims about how coding agents fail on this task. This directory holds the evidence: one set of files per attempt, produced by `run_gemini_attempt.sh` and never edited by hand.

## Method

1. `run_gemini_attempt.sh <n>` copies `workspace/` fresh and commits that baseline in a throwaway git repo.
2. The agent runs in that directory with `prompt.md` passed verbatim as its only prompt, every tool call auto-approved, and a Python 3.11 + pytest environment first on `PATH` (the same Python the grader container uses). The run is bounded at 20 minutes of wall clock.
3. The agent's working tree is diffed against the baseline (`gemini_attempt_NN.diff`), then mounted unchanged into the evaluation container at `/eval/workspace` and graded (`gemini_attempt_NN_score.json`). `gemini_attempt_NN_meta.json` records agent, model, Python, start time, duration, and exit code.

## Attempt 01: Gemini 3.8 Flash (high) via Antigravity CLI 1.3.6, 2026-10-15

| | |
|---|---|
| Score | 1.00 / 1.00, integrity gate clean, all six dimensions at full credit |
| Wall clock | 13 min 47 s of agent time (826.6 s) |
| Tool calls | 54 shell commands, 40 file views, 18 file edits |
| Tokens | 657k input, 88k output, 68k thinking, 4.07M cache reads |
| Files changed | `engine.py`, `db.py`, `outbox.py`, `batch.py`, `tests/test_reproduction.py` |

What the patch does:

- **Idempotency storm:** a per-key `asyncio.Lock` plus an in-flight map keyed by idempotency key, so retries wait for the first request and mutated payloads raise `IdempotencyConflictError` whether the original is in flight or already committed.
- **Balance drift and throughput:** per-account locks acquired in sorted order around the read-modify-write, so same-pair retries serialize while disjoint pairs stay parallel (100 disjoint transfers in 0.051 s under the grader).
- **Outbox:** a `claim_pending_outbox_events` method marks rows `IN_FLIGHT` under a mutex before dispatch, so concurrent workers never pick the same row; `PermanentWebhookError` goes straight to `DEAD_LETTER` and the tick continues.
- **Batches:** all accounts in the batch are locked in sorted order, every leg is evaluated against in-memory running balances first, and writes happen only after the whole batch validates. Batch results are persisted with a payload hash for replay and conflict detection.
- **Reproduction tests:** three tests that fail on the baseline and pass on the patch, verified by the grader's fail-to-pass check.

Residual observations the rubric does not grade: the lock dictionaries grow without bound, an unexpected transport exception is recorded and then re-raised so it still aborts the remainder of that tick, and in-flight conflict detection lives in process-local dictionaries, so it does not extend across processes.
