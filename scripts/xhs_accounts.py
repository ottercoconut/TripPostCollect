#!/usr/bin/env python3
"""Manage the independent Xiaohongshu account pool."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path

from trippostcollect.core.paths import DEFAULT_DB
from trippostcollect.db.bootstrap import bootstrap_database
from trippostcollect.xhs.accounts import (
    account_paths,
    enroll_account,
    ensure_xhs_schema,
    get_account,
    list_accounts,
    record_event,
    set_account_status,
    validate_account_id,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage isolated Xiaohongshu crawl accounts.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    subparsers = parser.add_subparsers(dest="command", required=True)

    enroll = subparsers.add_parser("enroll", help="Create an isolated account slot before login.")
    enroll.add_argument("--account-id", required=True)

    subparsers.add_parser("list", help="List account health without secrets.")

    retire = subparsers.add_parser("retire", help="Retire an account and optionally purge its local secrets.")
    retire.add_argument("--account-id", required=True)
    retire.add_argument("--purge-profile", action="store_true")
    retire.add_argument("--yes", action="store_true", help="Required with --purge-profile.")

    quarantine = subparsers.add_parser("quarantine", help="Manually quarantine an account.")
    quarantine.add_argument("--account-id", required=True)
    quarantine.add_argument("--reason", required=True)

    activate = subparsers.add_parser("activate", help="Manually make a previously blocked account selectable.")
    activate.add_argument("--account-id", required=True)
    activate.add_argument("--reason", required=True)
    return parser.parse_args()


def public_account(record: dict) -> dict:
    return {
        key: value
        for key, value in record.items()
        if key not in {"identity_hash"}
    }


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser()
    bootstrap_database(db_path, sync_jobs=False)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        if args.command == "enroll":
            result = enroll_account(conn, args.account_id)
            print(json.dumps(public_account(result), ensure_ascii=False, indent=2))
            return 0
        if args.command == "list":
            leases = [dict(row) for row in conn.execute("SELECT * FROM xhs_account_leases ORDER BY acquired_at")]
            print(
                json.dumps(
                    {
                        "active_leases": leases,
                        "accounts": [public_account(item) for item in list_accounts(conn)],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        account_id = validate_account_id(args.account_id)
        account = get_account(conn, account_id)
        if not account:
            raise SystemExit(f"XHS account is not enrolled: {account_id}")
        lease = conn.execute(
            "SELECT run_id, expires_at FROM xhs_account_leases WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if lease:
            raise SystemExit(
                f"XHS account has an active lease: run_id={lease['run_id']} expires_at={lease['expires_at']}"
            )
        if args.command == "quarantine":
            set_account_status(conn, account_id, "quarantined", reason=args.reason)
            print(json.dumps({"account_id": account_id, "status": "quarantined"}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "activate":
            if not account.get("identity_hash"):
                raise SystemExit(f"XHS account has no verified identity: {account_id}; run xhs_login.py first")
            set_account_status(conn, account_id, "active", reason=args.reason)
            print(json.dumps({"account_id": account_id, "status": "active"}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "retire":
            if args.purge_profile and not args.yes:
                raise SystemExit("--purge-profile requires --yes")
            set_account_status(conn, account_id, "retired", reason="manual_retirement")
            purged = False
            if args.purge_profile:
                root = account_paths(account_id)["root"]
                if root.is_dir():
                    shutil.rmtree(root)
                    purged = True
                record_event(conn, account_id=account_id, event_type="account_secrets_purged")
                conn.commit()
            print(json.dumps({"account_id": account_id, "status": "retired", "purged": purged}, ensure_ascii=False, indent=2))
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
