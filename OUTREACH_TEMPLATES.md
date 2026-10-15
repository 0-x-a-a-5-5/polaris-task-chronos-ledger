# Polaris Outreach & Application Protocol

Reference Job ID: **114077679900074694** ("Research Engineer, Polaris, DeepMind")

---

## 1. Direct Email to Polaris Research Leads & Engineers
**Target Audience:** Polaris / SWE Evaluation leads and researchers at Google DeepMind (e.g., Tamay Besiroglu and SWE evaluation engineers).

**Subject:** `[Polaris RE #114077679900074694] Built a Polaris-spec async SWE eval task + AST anti-hack grader (POLARIS-SWE-042)`

**Body:**
> Hi [Name],
> 
> I recently applied for the **Research Engineer, Polaris** role (Job ID: 114077679900074694). Since the team focuses on hand-crafting realistic, long-horizon software engineering tasks rather than synthetic pipelines, I built a complete, Dockerized sample task and continuous grader to demonstrate how I approach eval design:
> 
> **Repository:** [GitHub Link to polaris-task-chronos-ledger]
> 
> **What POLARIS-SWE-042 (chronos-ledger) tests:** The task drops an agent into a zero-dependency Python 3.11 async double-entry ledger and transactional outbox codebase where all public sequential tests pass (0.15/1.00 baseline), and asks it to remediate two production concurrency incidents and implement an atomic multi-hop batch processor.
> 
> **How it exposes frontier coding agent blind spots:**
> * **The Global Lock Trap:** Agents that fix the 50-way idempotency race by wrapping `execute_transfer()` in a coarse `asyncio.Lock()` pass correctness checks but fail Sub-Rubric 6 (100 concurrent transfers across disjoint account pairs with a 15ms I/O wait must finish in $\le$ 0.35s; a global lock forces $\ge$ 1.50s sequential execution).
> * **AB-BA & Reentrant Self-Deadlocks:** Solving multi-hop batch isolation while delegating leg execution to `LedgerEngine` causes standard agents to either deadlock on opposing batches (`X->Y` vs. `Y->X`) or self-deadlock on non-reentrant `asyncio.Lock` acquisition unless they combine canonical lock ordering with `contextvars.ContextVar` task reentrancy.
> * **4-Layer Anti-Reward-Hacking Grader (`eval_harness/grader.py`):** Enforces SHA-256 file integrity, AST static inspection (`AntiHackASTVisitor` blocks deleting `asyncio.sleep`, monkey-patching timers, or `except Exception: pass`), and an automated **Fail-to-Pass protocol** that verifies the agent’s reproduction tests fail on the unpatched baseline before passing on the patch.
> 
> The repo includes the unpatched workspace, the [0.00 - 1.00] deterministic grader, a 1.00/1.00 reference patch (`solutions/reference_patch.diff`), and a diagnostic post-mortem on model failure trajectories.
> 
> I’d love to bring this level of evaluation rigor to Polaris. Happy to walk through the task design or build another benchmark on a stack of your choice.
> 
> Best,  
> [Your Name]  
> [GitHub Link] | [LinkedIn Link] | [Phone / Email]

---

## 2. Direct Email / LinkedIn InMail to DeepMind Technical Recruiters
**Target Audience:** Google DeepMind Technical Sourcers and Staffing Partners supporting Research Engineering.

**Subject:** `Application & Work Sample: Research Engineer, Polaris, DeepMind (Job ID: 114077679900074694)`

**Body:**
> Hi [Recruiter Name],
> 
> I recently submitted my application for **Research Engineer, Polaris, DeepMind** (**Job ID: 114077679900074694**).
> 
> Because the job description emphasizes that Polaris engineers spend ~1 week hand-crafting realistic software engineering evaluation tasks (prompt + numerical grader) to measure Gemini's long-horizon coding capabilities, I built a complete, production-ready Polaris evaluation package to submit alongside my application:
> 
> * **GitHub Repository (POLARIS-SWE-042):** [GitHub Link to polaris-task-chronos-ledger]
> * **What’s Included:** A multi-file async Python systems benchmark (`prompt.md`), an isolated deterministic numerical grader (`grader.py`) with AST anti-reward-hacking enforcement and fail-to-pass test verification, a 1.00/1.00 reference patch, and a technical post-mortem analyzing where frontier models fail on the task.
> * **Run Time:** Runs completely offline in Docker (`docker build -t polaris-chronos-eval . && docker run --rm polaris-chronos-eval`) in under 5 seconds.
> 
> Could you attach this repository link to my candidate packet for **Job ID 114077679900074694** for the Polaris hiring team’s review? I would welcome the chance to discuss my background and this evaluation task with you.
> 
> Best regards,  
> [Your Name]  
> [Email Address Used on Google Careers Application]  
> [LinkedIn Profile] | [GitHub Link]

---

## 3. LinkedIn Connection Request Notes (Under 300 Characters)

### Option A — For a Polaris Engineer / Researcher (284 chars)
> Hi [Name] — I applied for the Polaris Research Engineer role (#114077679900074694) and built a complete Polaris-spec async SWE eval task + AST anti-reward-hacking grader that catches frontier agents in a global-lock vs. deadlock trap: [Short GitHub URL]. Would love to connect!

### Option B — For a DeepMind Technical Recruiter (279 chars)
> Hi [Name] — I just applied for Research Engineer, Polaris (#114077679900074694). To demonstrate direct fit for the role, I hand-built a complete, Dockerized SWE benchmark task + deterministic Python grader (POLARIS-SWE-042): [Short GitHub URL]. Would love to share it with the team.

---

## 4. X (Twitter) Technical Thread & DM

**Tweet 1:**
> Why do frontier coding agents score high on unit-test benchmarks yet break production async systems?
> Because binary test suites reward **Symptom Patching** (e.g., slapping a global asyncio.Lock() on a service).
> I built POLARIS-SWE-042 (chronos-ledger), an open-source, Dockerized long-horizon SWE eval task + anti-reward-hacking grader: [GitHub Link] 🧵👇

**Tweet 2:**
> The setup: A zero-dependency Python 3.11 double-entry ledger & outbox where all sequential public tests pass (0.15/1.00 baseline), hiding a 50-way TOCTOU idempotency race, poison-pill head-of-line blocking, and non-atomic multi-hop batches.
> If an agent uses a global lock to stop the race, it fails Sub-Rubric 6: 100 disjoint transfers with a 15ms I/O wait must finish in $\le$ 0.35s (global lock takes $\ge$ 1.50s).

**Tweet 3:**
> To score 1.00/1.00 on grader.py, the agent has to:
> 1. Acquire per-account locks in canonical order (`sorted(set(accts))`) to survive opposing batch deadlocks (`X->Y` vs `Y->X`).
> 2. Track coroutine-held locks via `ContextVar` so multi-hop batches don't self-deadlock.
> 3. Pass AST static checks & Fail-to-Pass test verification.
> Full repo, rubric, and reference patch: [GitHub Link]

---

## 5. Resume Top-of-Page Callout (Page 1 Header)

```text
POLARIS EVALUATION PORTFOLIO (BUILT FOR JOB ID: 114077679900074694)
POLARIS-SWE-042: Chronos High-Concurrency Ledger & Transactional Outbox Benchmark | [Clickable GitHub Link]
• Designed an end-to-end, air-gapped Docker evaluation task (prompt.md + multi-file Python 3.11 async codebase + 1.00/1.00 reference patch) testing long-horizon concurrency debugging, canonical lock ordering, and transactional outbox reliability.
• Engineered a continuous [0.00–1.00] deterministic grader (grader.py) featuring SHA-256 integrity checks, AST anti-reward-hacking static analysis, automated Fail-to-Pass reproduction test verification, and a 100-way disjoint throughput SLA that penalizes coarse global-lock patches.
```
