# /// script
# requires-python = ">=3.12"
# dependencies = ["langfuse"]
# ///
"""Phase 0 check 3: which environment variable does the installed Langfuse SDK read for its host?

The design uses LANGFUSE_HOST (DESIGN §10); the course's Langfuse notebook uses LANGFUSE_BASE_URL.
Pass criterion, written before running: we know, for the installed version, which variable or
variables set the host, from the client itself and not only from its source text.
Nothing is sent: the keys are dummies, tracing is disabled and the host is a closed local port.
Run: uv run 03_langfuse_host_variable.py
"""
import importlib.metadata as md
import os
import pathlib

import langfuse

NAMES = ("LANGFUSE_HOST", "LANGFUSE_BASE_URL")
print(f"langfuse {md.version('langfuse')}")

root = pathlib.Path(langfuse.__file__).parent
for name in NAMES:
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*.py") if name in p.read_text(errors="ignore"))
    print(f"source mentions {name}: {len(files)} file(s) {files[:4]}")


def resolved_host(case: str, env: dict[str, str]) -> str:
    for name in NAMES:
        os.environ.pop(name, None)
    os.environ.update(env)
    client = langfuse.Langfuse(public_key=f"pk-lf-spike-{case}", secret_key="sk-lf-spike", tracing_enabled=False)
    found = {k: v for k, v in vars(client).items() if isinstance(v, str) and v.startswith("http")}
    return str(found)


results = {
    "only LANGFUSE_HOST": resolved_host("a", {"LANGFUSE_HOST": "http://127.0.0.1:9/from-host"}),
    "only LANGFUSE_BASE_URL": resolved_host("b", {"LANGFUSE_BASE_URL": "http://127.0.0.1:9/from-base-url"}),
    "both set": resolved_host("c", {"LANGFUSE_HOST": "http://127.0.0.1:9/from-host", "LANGFUSE_BASE_URL": "http://127.0.0.1:9/from-base-url"}),
}
for case, value in results.items():
    print(f"{case}: {value}")
reads_host = "from-host" in results["only LANGFUSE_HOST"]
reads_base = "from-base-url" in results["only LANGFUSE_BASE_URL"]
print(f"reads LANGFUSE_HOST: {reads_host}; reads LANGFUSE_BASE_URL: {reads_base}")
print("RESULT:", "PASS" if (reads_host or reads_base) else "FAIL")
