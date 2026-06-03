"""
Task 0 spike (throwaway): prove the e2b sandbox core before building COMPUTER_SPEC.

Proves the three things the whole platform rests on:
  1. We can run a command in a sandbox.
  2. We can write + read files.
  3. The filesystem PERSISTS across a pause/resume (the "computer that sits for
     them at all times" promise), and measures the wake latency.

Run:
    1. Get a free API key at https://e2b.dev  (Dashboard -> API Keys)
    2. Add it to arkos/.env:   E2B_API_KEY=e2b_...
    3. python scripts/spike_sandbox.py

This script is throwaway -- it is not part of the product. It only tells us
whether e2b's persistence + latency are good enough to commit to.
"""

import os
import time

from dotenv import load_dotenv

# Load arkos/.env so E2B_API_KEY is picked up (the e2b SDK reads it from env).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

if not os.environ.get("E2B_API_KEY"):
    raise SystemExit(
        "E2B_API_KEY not set.\n"
        "  1. Get a free key at https://e2b.dev (Dashboard -> API Keys)\n"
        "  2. Add to arkos/.env:  E2B_API_KEY=e2b_...\n"
    )

from e2b_code_interpreter import Sandbox

WORK = "/home/user/work"
FILE = f"{WORK}/hello.txt"
CONTENT = "persisted across pause/resume\n"


def banner(msg: str) -> None:
    print(f"\n{'=' * 60}\n{msg}\n{'=' * 60}")


def main() -> None:
    banner("1. CREATE sandbox")
    t0 = time.time()
    sbx = Sandbox.create(timeout=300)  # 5 min keep-alive
    create_latency = time.time() - t0
    sandbox_id = sbx.sandbox_id
    print(f"  sandbox_id     = {sandbox_id}")
    print(f"  create latency = {create_latency:.2f}s")

    banner("2. RUN a command")
    sbx.commands.run(f"mkdir -p {WORK}")
    res = sbx.commands.run("python3 -c 'print(6*7)'")
    print(f"  `python3 -c 'print(6*7)'` -> stdout={res.stdout.strip()!r} exit={res.exit_code}")

    banner("3. WRITE + READ a file")
    sbx.files.write(FILE, CONTENT)
    read_back = sbx.files.read(FILE)
    print(f"  wrote {FILE}")
    print(f"  read back -> {read_back!r}")
    assert read_back == CONTENT, "read-back mismatch before pause"

    banner("4. PAUSE (hibernate -- compute stops, state should persist)")
    t0 = time.time()
    sbx.pause()  # returns None in this SDK version; reconnect via sandbox_id from create
    print(f"  paused. (reconnect via sandbox_id = {sandbox_id})")
    print(f"  pause latency = {time.time() - t0:.2f}s")

    banner("5. RESUME via Sandbox.connect(sandbox_id) -- the wake-latency test")
    t0 = time.time()
    sbx2 = Sandbox.connect(sandbox_id)
    resume_latency = time.time() - t0
    print(f"  reconnected. resume latency = {resume_latency:.2f}s")

    banner("6. CONFIRM the file survived the pause/resume")
    survived = sbx2.files.read(FILE)
    print(f"  read after resume -> {survived!r}")
    persisted = survived == CONTENT
    print(f"  PERSISTED? {persisted}")

    banner("7. show the filesystem")
    ls = sbx2.commands.run(f"ls -la {WORK}")
    print(ls.stdout)

    banner("CLEANUP: kill the sandbox")
    sbx2.kill()
    print("  killed.")

    banner("SPIKE RESULT")
    print(f"  command execution : OK")
    print(f"  file write/read   : OK")
    print(f"  persistence       : {'PASS' if persisted else 'FAIL'}")
    print(f"  create latency    : {create_latency:.2f}s")
    print(f"  resume latency    : {resume_latency:.2f}s")
    print()
    print("  Verdict: if persistence is PASS and resume latency is a few seconds,")
    print("  the core holds -- proceed to COMPUTER_SPEC Task 4 (SandboxManager).")


if __name__ == "__main__":
    main()
