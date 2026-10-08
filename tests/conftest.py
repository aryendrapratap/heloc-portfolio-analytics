"""
Shared test setup.

The tests run against the real MySQL database in .env (or the DB_* environment
variables on GitHub Actions). The database is rebuilt and reloaded once at the
start, so running the tests resets heloc_db to its normal state.
"""

import os
import subprocess
import sys
from pathlib import Path

import pymysql
import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def run_script(path: str) -> None:
    result = subprocess.run([sys.executable, path], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        pytest.fail(f"{path} failed:\n{result.stdout}\n{result.stderr}")


def rebuild_database() -> None:
    run_script("scripts/build_db.py")
    run_script("data_generator/generate_data.py")


def connect():
    return pymysql.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        autocommit=False,
        init_command="SET SESSION lock_wait_timeout = 20",  # fail fast instead of hanging
    )


@pytest.fixture(scope="session", autouse=True)
def fresh_database():
    rebuild_database()


@pytest.fixture
def db():
    """A cursor inside a transaction that is rolled back after the test."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def rebuild_after():
    """For tests that save changes (month-end): reset the database afterwards.
    These tests use their own connection and close it, so the rebuild isn't blocked."""
    yield
    rebuild_database()


def scalar(cur, sql, args=None):
    cur.execute(sql, args)
    return cur.fetchone()[0]
