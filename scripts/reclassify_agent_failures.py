"""Reclassify completed Agent failures from their read-only upstream threads.

Dry-run by default. Use --apply only after reviewing the proposed categories.
No task is resubmitted, and stored charges are left unchanged.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings
from app.db import Database
from app.pollo_client import PolloClient
from app.task_errors import failure_diagnostic, public_failure


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    settings = load_settings()
    db = Database(settings.database_path)
    candidates = [
        task for task in db.list_tasks(max(1, min(args.limit, 500)))
        if task["status"] == "failed"
        and task.get("channel") == "pollo_agent"
        and task.get("error_code") in {"GENERATION_FAILED", "AGENT_NO_VIDEO"}
        and task.get("generation_id")
    ]
    changed = 0
    for task in candidates:
        account = db.get_account(task["account_id"], include_secrets=True)
        if not account:
            print(task["id"], "skipped: account missing")
            continue
        client = PolloClient(account, settings)
        try:
            detail = client.agent_detail(task["generation_id"], task["request"])
        except Exception as exc:
            print(task["id"], "skipped:", type(exc).__name__)
            continue
        finally:
            client.close()
        if detail.get("status") != "failed":
            print(task["id"], "skipped: upstream thread is not failed")
            continue
        code = str(detail.get("errorCode") or "")
        if not code:
            print(task["id"], "skipped: no classified reason")
            continue
        if code == task["error_code"]:
            continue
        audit = dict(task.get("upstream_response") or {})
        audit["detail"] = detail
        audit["failure"] = {
            "stage": "generation",
            "message": failure_diagnostic(detail),
        }
        failure = public_failure({**task, "error_code": code, "upstream_response": audit})
        print(task["id"], task["error_code"], "->", code,
              "message:", failure["message"], "refunded:", failure["refunded"])
        if args.apply:
            db.update_task(
                task["id"], error_code=code, error_message=failure["message"],
                upstream_response=audit,
            )
            changed += 1
    print("candidates", len(candidates), "updated", changed,
          "mode", "apply" if args.apply else "dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
