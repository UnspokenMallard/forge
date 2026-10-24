"""Brokkr's own checks, against a scratch Mimir store and a fake host. Run: ~/.forge/.venv/bin/python tests/test_brokkr.py

The cutover bar is "no change in behavior", and Jarvis's selftest proves that with the real host.
This file proves the part Brokkr owns: its records in Mimir, its refusal to guess, and its pure rules.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="brokkr-test-"))
os.environ["BROKKR_HOME"] = str(TMP / "brokkr")
os.environ["JARVIS_HOME"] = str(TMP / "jarvis")
os.environ["MEMORY_DB"] = str(TMP / "memory.db")  # Mimir's store, a scratch one
os.environ["MIMIR_OUTBOX"] = str(TMP / "outbox")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path.home() / ".mind" / "mimir"))

import mem  # noqa: E402
import mimirrecords as mr  # noqa: E402

mr.use_local(mem.connect(shared=True))  # calls go to the scratch store in-process, no HTTP

from brokkr import cli, earned, g15, policy, proposer, queue, replay, review, store  # noqa: E402
from brokkr.host import HostError, host  # noqa: E402

FAILED = []


def check(name: str, ok: bool, detail=None) -> None:
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        FAILED.append(name)
        print("     ", detail)


# -- paths follow the environment, so no test touches the real store ---------------------
check("intake and checkouts follow JARVIS_HOME", store.QUEUE_PATH == TMP / "jarvis" / "queue"
      and store.WORKTREES == TMP / "jarvis" / "worktrees", (store.QUEUE_PATH, store.WORKTREES))

# -- the host: nothing unbound answers ---------------------------------------------------
host.unbind()
try:
    host.notify("k", "m")
    check("an unbound host name raises", False)
except HostError:
    check("an unbound host name raises", True)
check("Brokkr has no database of its own", not hasattr(store, "DB_PATH"))
check("config has a read-only fallback", host.load_config() == {})
host.bind(notify=lambda *a, **k: None)
check("a bound name answers", host.notify("k", "m") is None)

# -- review: lifecycle in Mimir, and the file is a rendering -----------------------------
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
rows = mr.query(kind="brokkr.review_item")
check("review items live in Mimir as brokkr.review_item records",
      len(rows) == 1 and rows[0]["data"]["outcome"] == review.ACCEPTED and rows[0]["data"].get("open") is None, rows)

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

# Jarvis's events are in its queue in Mimir and Brokkr's classifications in a stream: the replay
# anti-joins the two. Events are written as Jarvis would write them.
for kind in ("verify-delegated-work", "verify-delegated-work", "noop"):
    mr.enqueue("jarvis", "jarvis.events", kind, {})
first_id, second_id, noop_id = sorted(e["id"] for e in mr.claim("jarvis", "jarvis.events", limit=3))
for eid, result in ((first_id, "NEEDS REVIEW"), (second_id, "NEEDS REVIEW"), (noop_id, "")):
    mr.settle("jarvis", "jarvis.events", eid, "done", result)
policy.record({"id": first_id, "kind": "verify-delegated-work"}, policy.LOCAL, "t")   # classified: excluded
classified = mr.query(stream=policy.STREAM)
check("a classification is a telemetry row with the event id",
      len(classified) == 1 and classified[0]["data"]["event_id"] == first_id and classified[0]["data"]["eligible"] == 1,
      classified)
sample = policy.replay_sample()
check("the replay excludes events already classified (anti-join across two stores)",
      sample["events"] == 2 and sample["eligible"] == 1 and sample["escalated"] == 1, sample)

blocked, detail = policy.supply()
passed_gate, lines = policy.gate()
check("supply and the gate read Mimir and answer", isinstance(blocked, bool) and detail and lines and not passed_gate,
      (blocked, detail, lines))

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

# -- Mimir down: a refusal, never a guess ------------------------------------------------
os.environ["MIMIR_URL"] = "none"
check("the CLI exits 3 when Mimir does not answer", cli.main(["review"]) == 3)
try:
    review.waiting()
    check("waiting() raises when Mimir does not answer", False)
except mr.MimirDown:
    check("waiting() raises when Mimir does not answer", True)
check("a corpus capture reports what it could not read",
      all(line.endswith("COULD NOT CAPTURE") for line in replay.capture_standard()))
del os.environ["MIMIR_URL"]

# -- Brokkr opened no SQLite file of its own ---------------------------------------------
check("no forge.db is created", not (TMP / "brokkr" / "state" / "forge.db").exists())

# -- queue: pure parsing -----------------------------------------------------------------
entries = queue._parse_entries("### q-1 — status: pending\nRepo: /x\nTask: t\n```\n### q-2 — status: pending\n```\n")
check("the queue ignores its own fenced example", [e["id"] for e in entries] == ["q-1"], entries)
check("an entry without Repo is blocked, not guessed", "no Repo" in (queue.entry_blocker("Task: t\n") or ""))

# -- proposer: the prompt is frozen by hash ----------------------------------------------
check("the prompt hash is stable text", len(proposer.PROMPT_SHA) == 12)

print(f"\n{'FAILED: ' + ', '.join(FAILED) if FAILED else 'ok'}")
sys.exit(1 if FAILED else 0)
