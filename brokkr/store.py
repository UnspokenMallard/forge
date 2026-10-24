"""Brokkr's store: state/forge.db (WAL), its state files, and the paths of the work it is handed.

Paths are module attributes read at call time, so a test points them at a scratch directory
(`store.ROOT`, `store.STATE`, `store.DB_PATH`) the way Jarvis's selftest does with `core.ROOT`;
`BROKKR_HOME` moves Brokkr's own data and `JARVIS_HOME` the intake and checkouts it works on.
Stdlib only: imported by Jarvis's venv and by Brokkr's own.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import tomllib
from pathlib import Path

ROOT = Path(os.environ.get("BROKKR_HOME", Path.home() / ".forge"))
STATE = ROOT / "state"
DB_PATH = STATE / "forge.db"
CONFIG = ROOT / "config.toml"

# Where the work comes from and lands. These stay in Jarvis (Anze, 2026-10-17): `queue` is his
# intake file and `worktrees` holds unreviewed work that nothing may move or delete.
JARVIS = Path(os.environ.get("JARVIS_HOME", Path.home() / ".jarvis"))
QUEUE_PATH = JARVIS / "queue"
WORKTREES = JARVIS / "worktrees"
DELEGATION = JARVIS / "delegation"
# The portfolio advisor's report, written by Odin since 2026-10-18 (was ~/.jarvis/portfolio-report.md).
REPORT = Path(os.environ.get("ODIN_HOME", Path.home() / ".analyst")) / "state" / "reports" / "portfolio" / "latest.md"

# A child of Jarvis's "jarvis" logger, so Brokkr's lines land in the same log with the same handlers.
log = logging.getLogger("jarvis.forge")


@contextlib.contextmanager
def connect():
    """`with connect() as conn:` commits or rolls back, and always closes (one handle per call)."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def load_config() -> dict:
    """~/.forge/config.toml; missing means every module's own defaults."""
    try:
        with open(CONFIG, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
