FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POLARIS_WORKSPACE=/eval/workspace \
    POLARIS_UNPATCHED=/eval/eval_harness/unpatched_baseline \
    POLARIS_HASHES=/eval/eval_harness/immutable_hashes.json

WORKDIR /eval

# Install pytest only; chronos-ledger itself uses zero third-party dependencies
RUN pip install --no-cache-dir pytest==8.3.3

# Copy workspace and evaluation harness
COPY prompt.md /eval/prompt.md
COPY workspace /eval/workspace
COPY eval_harness/grader.py /eval/eval_harness/grader.py

# Create the unpatched baseline snapshot and SHA-256 integrity manifest at build time
RUN cp -r /eval/workspace /eval/eval_harness/unpatched_baseline && \
    python3 -c 'import hashlib, json, pathlib; \
    ws = pathlib.Path("/eval/workspace"); \
    targets = ["src/chronos/models.py", "tests/test_public_ledger.py"]; \
    hashes = {t: hashlib.sha256((ws / t).read_bytes()).hexdigest() for t in targets}; \
    pathlib.Path("/eval/eval_harness/immutable_hashes.json").write_text(json.dumps(hashes, indent=2))'

WORKDIR /eval/workspace

# Default command runs the Polaris deterministic numerical grader
CMD ["python3", "/eval/eval_harness/grader.py"]
