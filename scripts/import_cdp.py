"""Import an authorized existing browser session without launching or navigating Chrome."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def main():
    from app.cdp import read_session
    from app.config import load_settings
    from app.db import Database
    from app.service import PolService

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--name", default="pollo-cdp")
    parser.add_argument("--proxy", default="")
    args = parser.parse_args()
    settings = load_settings()
    db = Database(settings.database_path)
    service = PolService(db, settings)
    try:
        context = read_session(args.port)
        account = service.upsert_account(
            {
                "name": args.name,
                "cdp_port": args.port,
                "proxy_url": args.proxy,
                "auto_login": False,
                "max_concurrency": 1,
                **context,
            }
        )
        checked = service.check_account(account["id"])
        print(
            json.dumps(
                {
                    "account_id": checked["id"],
                    "status": checked["status"],
                    "balance": checked["last_balance"],
                    "has_project": bool(checked.get("team_id")),
                }
            )
        )
    finally:
        service.stop()


if __name__ == "__main__":
    main()
