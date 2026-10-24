"""Brokkr's store: its state files and the paths of the work it is handed. It keeps no database:
what it remembers (review items, policy classifications) lives in Mimir under the `brokkr.` prefix.

Paths are module attributes read at call time, so a test points them at a scratch directory
(`store.ROOT`, `store.STATE`) the way Jarvis's selftest does with `core.ROOT`;
`BROKKR_HOME` moves Brokkr's own data and `JARVIS_HOME` the intake and checkouts it works on.
Stdlib only: imported by Jarvis's venv and by Brokkr's own.
"""
from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

ROOT = Path(os.environ.get("BROKKR_HOME", Path.home() / ".forge"))
STATE = ROOT / "state"
SOURCE = "brokkr"  # the prefix of every Mimir kind, stream and queue Brokkr writes
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


def load_config() -> dict:
    """~/.forge/config.toml; missing means every module's own defaults."""
    try:
        with open(CONFIG, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
