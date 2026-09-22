from __future__ import annotations

import argparse
import json
import uuid

from historybot.conflict_review import run_deterministic_review

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate evidence-linked person/place/time conflict candidates")
    parser.add_argument("--db", required=True)
    parser.add_argument("--task-id", default=f"conflict_review_{uuid.uuid4().hex[:12]}")
    args = parser.parse_args()
    print(json.dumps(run_deterministic_review(args.db, args.task_id), ensure_ascii=False, indent=2))
