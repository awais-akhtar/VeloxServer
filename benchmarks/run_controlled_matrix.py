from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run repeated controlled benchmark rounds.")
    parser.add_argument("--target", action="append", required=True, help="name=http://host:port/path")
    parser.add_argument("--requests", type=int, default=5000)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--cooldown", type=float, default=2.0)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results/latest.json"))
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    runs = []
    for round_index in range(args.rounds):
        command = [
            sys.executable,
            "benchmarks/static_benchmark.py",
            "--requests",
            str(args.requests),
            "--concurrency",
            str(args.concurrency),
        ]
        for target in args.target:
            command.extend(["--target", target])
        started = time.time()
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        runs.append(
            {
                "round": round_index + 1,
                "started_at": started,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )
        if completed.returncode != 0:
            break
        time.sleep(args.cooldown)
    args.output.write_text(json.dumps({"runs": runs}, indent=2), encoding="utf-8")
    return 0 if all(item["returncode"] == 0 for item in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
