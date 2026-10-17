"""Forge's own checks, against scratch data and a fake host. Run: ~/.forge/.venv/bin/python tests/test_forge.py

The cutover bar is "no change in behavior", and Jarvis's selftest proves that with the real host.
This file proves the part Forge owns: its store, its refusal to guess, and its pure rules.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="forge-test-"))
os.environ["FORGE_HOME"] = str(TMP / "forge")
os.environ["JARVIS_HOME"] = str(TMP / "jarvis")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge import earned, g15, policy, proposer, queue, replay, review, store  # noqa: E402
from forge.host import HostError, host  # noqa: E402

FAILED = []


def check(name: str, ok: bool, detail=None) -> None:
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        FAILED.append(name)
        print("     ", detail)


# -- paths follow the environment, so no test touches the real store ---------------------
check("data goes to FORGE_HOME", store.DB_PATH == TMP / "forge" / "state" / "forge.db", store.DB_PATH)
check("intake and checkouts follow JARVIS_HOME", store.QUEUE_PATH == TMP / "jarvis" / "queue"
      and store.WORKTREES == TMP / "jarvis" / "worktrees", (store.QUEUE_PATH, store.WORKTREES))

# -- the host: nothing unbound answers ---------------------------------------------------
host.unbind()
try:
    host.notify("k", "m")
    check("an unbound host name raises", False)
except HostError:
    check("an unbound host name raises", True)
check("config and database have a read-only fallback", host.load_config() == {} and callable(host.connect))
host.bind(notify=lambda *a, **k: None)
check("a bound name answers", host.notify("k", "m") is None)

# -- review: lifecycle in forge.db, and the file is a rendering --------------------------
review.REVIEW_QUEUE = TMP / "REVIEW-QUEUE.md"
first = review.add("branch", "one finished branch", task_id="q-1", repo="/r", branch="jarvis/q-1", tip_sha="abc")
again = review.add("branch", "one finished branch, restated", task_id="q-1", repo="/r", branch="jarvis/q-1")
check("re-adding an open item keeps one row", first == again and len(review.waiting()) == 1, (first, again))
waited = review.waiting()[0]["waiting_since"]
review.add("branch", "touched again", task_id="q-1", repo="/r", branch="jarvis/q-1")
check("re-adding keeps the age of the wait", review.waiting()[0]["waiting_since"] == waited)
review.render()
text = review.REVIEW_QUEUE.read_text()
check("the rendering is ASCII with dash-led item lines", text.isascii() and "- **q-1**" in text, text)
check("clear records an outcome once", review.clear(first, review.ACCEPTED, "merged")
      and not review.clear(first, review.ACCEPTED, "merged again"))
check("a cleared item leaves the queue", review.waiting() == [])
with sqlite3.connect(store.DB_PATH) as conn:
    check("review_items lives in forge.db", conn.execute("SELECT COUNT(*) FROM review_items").fetchone()[0] == 1)

# -- fate: git decides, and "cannot tell" is its own answer -------------------------------
check("an item whose repo is gone stays open", asyncio.run(review.fate({"repo": "/nope", "branch": "b"})) is None)

# -- earned: proposals only, from reviewed outcomes ---------------------------------------
now = time.time()
for i in range(3):
    j = review.add("branch", f"w{i}", task_id=f"q-e{i}", repo="/code/x", branch=f"b{i}")
    review.clear(j, review.ACCEPTED, "merged")
lines = earned.proposals({"actions": []}, now=now + 1)
check("three accepts with no discard propose declaring the repo", len(lines) == 1 and "earned:" in lines[0], lines)

# -- policy: the clock never escalates ----------------------------------------------------
check("a scheduled kind never escalates", policy.classify({"kind": "queue-dispatch", "state": "done", "result": "x"})[0]
      == policy.LOCAL)
check("NEEDS REVIEW escalates", policy.classify(
    {"kind": "verify-delegated-work", "state": "done", "result": "NEEDS REVIEW: x"})[0] == policy.ESCALATE)
check("escalation is off without config", not policy.escalation_enabled({}))
policy.record({"id": 1, "kind": "verify-delegated-work"}, policy.LOCAL, "t")


@contextlib.contextmanager
def _events():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE events(id INTEGER, kind TEXT, state TEXT, result TEXT, payload TEXT, created_at REAL)")
    conn.executemany("INSERT INTO events VALUES(?,?,?,?,?,?)", [
        (1, "verify-delegated-work", "done", "NEEDS REVIEW", "{}", now),      # already classified: excluded
        (2, "verify-delegated-work", "done", "NEEDS REVIEW", "{}", now),
        (3, "noop", "done", "", "{}", now),
    ])
    yield conn


host.bind(connect=_events)
sample = policy.replay_sample()
check("the replay excludes events already classified (anti-join across two databases)",
      sample["events"] == 2 and sample["eligible"] == 1 and sample["escalated"] == 1, sample)

# -- replay: a corpus never shrinks -------------------------------------------------------
replay.CORPUS_DIR = TMP / "corpora"
replay.capture("t", [{"key": "a", "v": 1}, {"key": "b", "v": 2}], source="test")
out = replay.capture("t", [{"key": "a", "v": 9}], source="test")
check("a capture that omits a key keeps it", out["total"] == 2 and out["retained"] == 1, out)
try:
    replay.capture("t", [{"v": 1}], source="test")
    check("a record without a key is refused", False)
except ValueError:
    check("a record without a key is refused", True)

# -- g15: no record is a refusal ---------------------------------------------------------
g15.REPLAY, g15.CORPUS = TMP / "none.json", TMP / "none2.json"
passed, why = g15.status()
check("G15 refuses with no replay", not passed and "no replay" in why, why)

# -- queue: pure parsing -----------------------------------------------------------------
entries = queue._parse_entries("### q-1 — status: pending\nRepo: /x\nTask: t\n```\n### q-2 — status: pending\n```\n")
check("the queue ignores its own fenced example", [e["id"] for e in entries] == ["q-1"], entries)
check("an entry without Repo is blocked, not guessed", "no Repo" in (queue.entry_blocker("Task: t\n") or ""))

# -- proposer: the prompt is frozen by hash ----------------------------------------------
check("the prompt hash is stable text", len(proposer.PROMPT_SHA) == 12)

print(f"\n{'FAILED: ' + ', '.join(FAILED) if FAILED else 'ok'}")
sys.exit(1 if FAILED else 0)
