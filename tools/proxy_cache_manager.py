from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def cache_entries(root: Path) -> list[dict[str, object]]:
    entries = []
    for meta_path in root.rglob("*.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            body_path = meta_path.with_suffix(".body")
            head_path = meta_path.with_suffix(".head")
            entries.append(
                {
                    "meta": str(meta_path),
                    "head": str(head_path),
                    "body": str(body_path),
                    "key": meta.get("key", ""),
                    "status": int(meta.get("status", 0)),
                    "expires_at": float(meta.get("expires_at", 0)),
                    "bytes": sum(path.stat().st_size for path in (meta_path, head_path, body_path) if path.exists()),
                }
            )
        except Exception:
            continue
    return entries


def purge_entry(entry: dict[str, object]) -> None:
    for name in ("meta", "head", "body"):
        path = Path(str(entry[name]))
        if path.exists():
            path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect and manage VeloxServer disk proxy cache directories.")
    parser.add_argument("root", type=Path)
    parser.add_argument("--purge-expired", action="store_true")
    parser.add_argument("--max-bytes", type=int, default=0)
    args = parser.parse_args()
    entries = cache_entries(args.root)
    now = time.time()
    purged = 0
    if args.purge_expired:
        for entry in list(entries):
            if float(entry["expires_at"]) < now:
                purge_entry(entry)
                purged += 1
        entries = cache_entries(args.root)
    if args.max_bytes > 0:
        total = sum(int(entry["bytes"]) for entry in entries)
        for entry in sorted(entries, key=lambda item: float(item["expires_at"])):
            if total <= args.max_bytes:
                break
            purge_entry(entry)
            total -= int(entry["bytes"])
            purged += 1
        entries = cache_entries(args.root)
    print(json.dumps({"entries": len(entries), "bytes": sum(int(entry["bytes"]) for entry in entries), "purged": purged}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
