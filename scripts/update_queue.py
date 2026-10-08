#!/usr/bin/env python3
"""Append the clips of a batch manifest to manifests/all-clips.json.

all-clips.json is the single reading-order list of every clip that is ready
to post. The scheduler (Claude, once a week) reads it, finds the last clip
already scheduled in Metricool, and schedules the following ones, one a day.
Usage: update_queue.py out/manifest.json
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
QUEUE = ROOT / "manifests" / "all-clips.json"


def main(path):
    batch = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    queue = json.loads(QUEUE.read_text(encoding="utf-8")) if QUEUE.exists() else {"clips": []}
    seen = {c["url"] for c in queue["clips"]}
    n = len(queue["clips"])
    for p in batch["posts"]:
        if p["url"] in seen:
            continue
        queue["clips"].append({
            "seq": len(queue["clips"]) + 1,
            "batch": batch["tag"],
            "surah_number": p["surah_number"],
            "surah_name": p["surah_name"],
            "from_ayah": p["from_ayah"],
            "to_ayah": p["to_ayah"],
            "style": p.get("style"),
            "seconds": p["seconds"],
            "url": p["url"],
            "caption": p["caption"],
        })
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"queue: {n} -> {len(queue['clips'])} clips")


if __name__ == "__main__":
    main(sys.argv[1])
