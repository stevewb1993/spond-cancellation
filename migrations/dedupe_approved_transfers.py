"""One-off migration: collapse duplicate approved transfer rows.

A concurrent double-submit used to insert two `approved` rows in
`transfer_requests` for the same (member_email, cancelled_event_id). New writes
are now blocked by the `idx_one_approved_transfer` unique index, but a database
with historical data may still hold duplicates that predate it — and the index
can't be created while they exist.

Run this once against each environment that has historical data, BEFORE the code
that creates the index is deployed there:

    # production (Postgres)
    DATABASE_URL=postgresql://... python -m migrations.dedupe_approved_transfers

    # local SQLite (uses ./transfers.db, same as the app)
    python -m migrations.dedupe_approved_transfers

It keeps the earliest approved row of each duplicate group and drops the rest,
then creates the index. Safe to re-run (the dedupe becomes a no-op and the index
uses IF NOT EXISTS). Once every environment has been migrated, this file can be
deleted.
"""

import os

from dotenv import load_dotenv

# Drop every approved row except the earliest (lowest id) of each
# (member_email, cancelled_event_id) group. Other statuses are left untouched.
DEDUPE_SQL = """
    DELETE FROM transfer_requests
    WHERE status = 'approved' AND id NOT IN (
        SELECT MIN(id) FROM transfer_requests
        WHERE status = 'approved'
        GROUP BY member_email, cancelled_event_id
    )
"""

CREATE_INDEX_SQL = """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_one_approved_transfer
    ON transfer_requests (member_email, cancelled_event_id)
    WHERE status = 'approved'
"""


def run(db):
    """Dedupe approved rows then ensure the unique index, and commit.

    Takes an open DB-API connection (psycopg or sqlite3); both expose the
    ``.execute``/``.commit`` slice this needs. Kept connection-agnostic so it's
    trivially testable against an in-memory SQLite database.
    """
    db.execute(DEDUPE_SQL)
    db.execute(CREATE_INDEX_SQL)
    db.commit()


def _connect():
    """Open a connection to the same backend the app uses (Postgres if
    DATABASE_URL is set, otherwise the local SQLite file)."""
    database_url = os.environ.get("DATABASE_URL", "")
    if database_url:
        import psycopg

        return psycopg.connect(database_url)
    import sqlite3

    db_path = os.path.join(os.path.dirname(__file__), "..", "transfers.db")
    return sqlite3.connect(db_path)


def main():
    load_dotenv()
    db = _connect()
    try:
        run(db)
    finally:
        db.close()
    print("Deduped approved transfers and ensured idx_one_approved_transfer.")


if __name__ == "__main__":
    main()
