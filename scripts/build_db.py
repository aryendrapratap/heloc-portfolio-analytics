"""
Rebuilds the HELOC database from scratch.

Runs every .sql file in the sql/ folder in file-name order
(01_..., 02_..., 03_...), then prints how many rows each table has.

It understands DELIMITER lines, so files with triggers and stored
procedures (added in later steps) work too. Rule for all .sql files:
end each statement with its delimiter at the end of a line.

Usage (from the project folder, with .venv active):
    python scripts/build_db.py

WARNING: 01_schema.sql drops and recreates every table, so this
deletes all data in heloc_db.
"""

import os
import sys
from pathlib import Path

import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql"


def split_statements(sql_text: str) -> list[str]:
    """Split a SQL script into single statements, handling DELIMITER changes."""
    statements = []
    delimiter = ";"
    buffer = []

    for line in sql_text.splitlines():
        stripped = line.strip()

        # DELIMITER is an instruction for the mysql command-line tool, not SQL,
        # so we apply it here and don't send it to the server.
        if stripped.upper().startswith("DELIMITER "):
            delimiter = stripped.split(None, 1)[1]
            continue

        # Skip blank lines and comment-only lines between statements.
        if not buffer and (not stripped or stripped.startswith("--")):
            continue

        buffer.append(line)

        if stripped.endswith(delimiter):
            statement = "\n".join(buffer).rstrip()
            statement = statement[: -len(delimiter)].strip()
            if statement:
                statements.append(statement)
            buffer = []

    leftover = "\n".join(buffer).strip()
    if leftover:
        statements.append(leftover)
    return statements


def connect() -> pymysql.connections.Connection:
    load_dotenv(ROOT / ".env")
    return pymysql.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        autocommit=True,
    )


def run_sql_files(cur) -> None:
    sql_files = sorted(SQL_DIR.glob("*.sql"))
    if not sql_files:
        sys.exit("No .sql files found in the sql/ folder.")

    for path in sql_files:
        statements = split_statements(path.read_text(encoding="utf-8"))
        for number, statement in enumerate(statements, start=1):
            try:
                cur.execute(statement)
            except pymysql.MySQLError:
                print(f"\nFAILED in {path.name}, statement {number}:")
                print(statement[:400])
                raise
        print(f"OK   {path.name:<28} {len(statements):>3} statements")


def print_row_counts(cur) -> None:
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' "
        "ORDER BY table_name"
    )
    tables = [row[0] for row in cur.fetchall()]

    print("\nRows per table:")
    for table in tables:
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        print(f"  {table:<24} {cur.fetchone()[0]:>8,}")


def main() -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            run_sql_files(cur)
            print_row_counts(cur)
    finally:
        conn.close()
    print("\nDatabase build complete.")


if __name__ == "__main__":
    main()
