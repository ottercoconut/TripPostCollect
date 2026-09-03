#!/usr/bin/env python3
"""Manage non-secret Xiaohongshu runtime slots and orphan leases."""

from __future__ import annotations

import argparse
import json
import sqlite3

from trippostcollect.core.paths import DEFAULT_DB
from trippostcollect.xhs.accounts import (
    bootstrap_xhs_control_database,
    ensure_xhs_schema,
    get_account,
    list_accounts,
    record_event,
    register_account_slot,
    set_account_status,
    validate_account_id,
)
from trippostcollect.xhs.leases import (
    XhsOrphanLeaseRecoveryRefused,
    public_lease,
    recover_orphaned_account_lease,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage Xiaohongshu crawl coordination slots.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    subparsers = parser.add_subparsers(dest="command", required=True)

    ensure_slot = subparsers.add_parser(
        "ensure-slot",
        help="Create a non-secret checkpoint and lease namespace.",
    )
    ensure_slot.add_argument("--account-id", required=True)

    subparsers.add_parser("list", help="List coordination slots and active leases.")

    retire = subparsers.add_parser("retire", help="Retire a coordination slot.")
    retire.add_argument("--account-id", required=True)

    quarantine = subparsers.add_parser("quarantine", help="Manually quarantine a coordination slot.")
    quarantine.add_argument("--account-id", required=True)
    quarantine.add_argument("--reason", required=True)

    activate = subparsers.add_parser("activate", help="Make a previously blocked slot selectable.")
    activate.add_argument("--account-id", required=True)
    activate.add_argument("--reason", required=True)

    recover = subparsers.add_parser(
        "recover-orphan-lease",
        help="Release one exact lease after proving its failed runtime has no live process.",
    )
    recover.add_argument("--account-id", required=True)
    recover.add_argument("--run-id", required=True)
    recover.add_argument("--lease-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = bootstrap_xhs_control_database(args.db)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_xhs_schema(conn)
        if args.command == "ensure-slot":
            result = register_account_slot(conn, args.account_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "list":
            leases = [
                public_lease(dict(row))
                for row in conn.execute(
                    "SELECT * FROM xhs_account_leases ORDER BY acquired_at",
                )
            ]
            print(
                json.dumps(
                    {
                        "leases": leases,
                        "slots": list_accounts(conn),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        account_id = validate_account_id(args.account_id)
        account = get_account(conn, account_id)
        if not account:
            raise SystemExit(f"XHS coordination slot does not exist: {account_id}")
        if args.command == "recover-orphan-lease":
            try:
                result = recover_orphaned_account_lease(
                    conn,
                    account_id=account_id,
                    run_id=args.run_id,
                    lease_id=args.lease_id,
                )
            except XhsOrphanLeaseRecoveryRefused as exc:
                raise SystemExit(f"XHS orphan lease reconciliation refused: {exc}") from exc
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        lease = conn.execute(
            "SELECT lease_id, run_id, expires_at FROM xhs_account_leases WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if lease:
            raise SystemExit(
                "XHS coordination slot has a lease: "
                f"lease_id={lease['lease_id']} run_id={lease['run_id']} "
                f"expires_at={lease['expires_at']}"
            )
        if args.command == "quarantine":
            set_account_status(conn, account_id, "quarantined", reason=args.reason)
            print(json.dumps({"account_id": account_id, "status": "quarantined"}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "activate":
            set_account_status(conn, account_id, "active", reason=args.reason)
            print(json.dumps({"account_id": account_id, "status": "active"}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "retire":
            set_account_status(conn, account_id, "retired", reason="manual_retirement")
            record_event(conn, account_id=account_id, event_type="account_slot_retired")
            conn.commit()
            print(json.dumps({"account_id": account_id, "status": "retired"}, ensure_ascii=False, indent=2))
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
