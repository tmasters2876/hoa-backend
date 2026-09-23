"""
seed_from_prod.py — clone production data into the local DEV Supabase stack.

Reads every public table from PRODUCTION (credentials from ../.env, the same
file the console uses) through the Supabase REST API — read-only — and
inserts the rows into the LOCAL stack (credentials from .env.dev).

Safety:
  * Production is only ever READ. The only client that writes is the local
    one, and it is refused unless its URL points at localhost/127.0.0.1.
  * Production password hashes never leave production: every copied
    admin_users row gets the same DEV password (below), and three extra
    dev accounts are added, one per role.

Usage (from hoa-backend/dev):
    ./dev.sh seed              # or:
    ../venv/bin/python3 seed_from_prod.py [--wipe]

--wipe deletes local rows first (default: refuse to seed into non-empty
tables, so an accidental double-run cannot duplicate data).
"""
import argparse
import os
import sys
from pathlib import Path

import bcrypt
from dotenv import dotenv_values
from supabase import create_client

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

DEV_PASSWORD = "devpass123"
DEV_ACCOUNTS = [  # username, role
    ("dev-superuser", "superuser"),
    ("dev-board", "board"),
    ("dev-member", "member"),
]

# Insert order respects foreign keys (clause_flags -> clauses,
# clause_flag_comments -> clause_flags).
TABLES = [
    "clauses",
    "admin_users",
    "clause_audit_log",
    "pending_changes",
    "user_activity_log",
    "clause_flags",
    "clause_flag_comments",
    "resident_questions",
]
PAGE = 1000
BATCH = 100


def _is_local(url: str) -> bool:
    return any(h in url for h in ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"))


def load_clients():
    prod = dotenv_values(REPO / ".env")
    dev = dotenv_values(HERE / ".env.dev")
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        if not prod.get(k):
            sys.exit(f"../.env is missing {k} (production credentials)")
        if not dev.get(k):
            sys.exit(f".env.dev is missing {k} — run ./dev.sh up first")
    if _is_local(prod["SUPABASE_URL"]):
        sys.exit("../.env SUPABASE_URL looks local — expected the production project")
    if not _is_local(dev["SUPABASE_URL"]):
        sys.exit(f"REFUSING: .env.dev SUPABASE_URL is not local ({dev['SUPABASE_URL']})")
    return (
        create_client(prod["SUPABASE_URL"], prod["SUPABASE_SERVICE_ROLE_KEY"]),
        create_client(dev["SUPABASE_URL"], dev["SUPABASE_SERVICE_ROLE_KEY"]),
    )


def fetch_all(client, table: str) -> list[dict]:
    rows, offset = [], 0
    while True:
        batch = (
            client.from_(table).select("*")
            .order("id")  # any stable order; every table has a uuid id
            .range(offset, offset + PAGE - 1).execute()
        ).data or []
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        offset += PAGE


def count(client, table: str) -> int:
    return (client.from_(table).select("id", count="exact").limit(1).execute()).count or 0


def insert_batches(client, table: str, rows: list[dict]):
    for i in range(0, len(rows), BATCH):
        client.from_(table).insert(rows[i:i + BATCH]).execute()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wipe", action="store_true", help="delete local rows before seeding")
    args = ap.parse_args()

    prod, dev = load_clients()

    # Guard: never seed on top of existing local data unless asked.
    existing = {t: count(dev, t) for t in TABLES}
    if any(existing.values()):
        if not args.wipe:
            sys.exit("Local tables already contain data: "
                     + ", ".join(f"{t}={n}" for t, n in existing.items() if n)
                     + "\nRe-run with --wipe (or ./dev.sh reset) to replace it.")
        for t in reversed(TABLES):  # children first
            dev.from_(t).delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
        print("[seed] local tables wiped")

    dev_hash = bcrypt.hashpw(DEV_PASSWORD.encode(), bcrypt.gensalt(rounds=12)).decode()

    for table in TABLES:
        rows = fetch_all(prod, table)
        extra = []
        if table == "admin_users":
            for r in rows:
                r["password_hash"] = dev_hash          # prod hashes never leave prod
                r["must_change_password"] = False
            present = {r["username"] for r in rows}
            # Dev accounts have no id (the DB assigns one). They must go in a
            # separate insert: PostgREST pads a mixed-key batch with nulls, and
            # a null id violates the primary key.
            extra = [{
                "username": username, "password_hash": dev_hash,
                "is_active": True, "must_change_password": False,
                "role": role, "is_approver": role == "board",
            } for username, role in DEV_ACCOUNTS if username not in present]
        insert_batches(dev, table, rows)
        if extra:
            insert_batches(dev, table, extra)
        print(f"[seed] {table:<22} {len(rows) + len(extra):>5} rows")

    print("\nDone. Every account (copied or dev-*) signs in with password:", DEV_PASSWORD)
    print("Dev accounts:", ", ".join(f"{u} ({r})" for u, r in DEV_ACCOUNTS))


if __name__ == "__main__":
    main()
