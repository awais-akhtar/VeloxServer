from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


TARGETS = [
    ("http_parser", ["fuzz/fuzz_http_parser.py", "50000"]),
    ("native_abi", ["fuzz/fuzz_native_abi.py", "5000"]),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the VeloxServer fuzz campaign.")
    parser.add_argument("--output", type=Path, default=Path("fuzz/results/latest.json"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results = []
    for name, command in TARGETS:
        started = time.time()
        completed = subprocess.run([sys.executable, *command], text=True, capture_output=True, check=False)
        results.append(
            {
                "target": name,
                "started_at": started,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )
    args.output.write_text(json.dumps({"results": results}, indent=2), encoding="utf-8")
    return 0 if all(item["returncode"] == 0 for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
