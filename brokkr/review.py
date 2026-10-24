"""What is finished and waiting for a human, with a lifecycle instead of a log.

`REVIEW-QUEUE.md` was designed in ORIGINATION-DESIGN.md section 3b and shipped
2026-09-03 as an append-only file: `queue.py`'s `append_review_note` opens it
with `"a"` and writes four lines per finished branch. That was the right first
cut and it has one structural problem -- **nothing ever takes a line out**.

Under Option A the human's whole role is this gate: *"I want to be a gatekeeper
for already done work, not to approve work on certain aspects but to review
work."* A gate whose queue only ever grows stops being read, which is the same
failure `alerts.log` had before dedup and `alerts` had before `incidents`. This
module is the third application of the same lesson: **a channel a human is
expected to act on needs a way for items to leave it.**

### Items leave by observation, never by being asked

The obvious design is a checkbox -- the user ticks a line, the daemon drops it.
Two reasons not to:

  * `AUTONOMY-LADDER-DESIGN.md` section 5 rules out files the daemon reads back
    as decisions, and while a tick here would authorize nothing, the *fate of
    the branch is already recorded somewhere more trustworthy*: git.
  * Any state a human has to maintain by hand drifts from reality, and a review
    queue that disagrees with the repo is worse than no review queue.

So the branch itself is the evidence:

| Observation | Outcome |
|---|---|
| the branch's tip is an ancestor of the repo's base branch | `accepted` |
| the branch is gone and its recorded tip is not an ancestor | `discarded` |
| the branch exists and is not merged | still waiting |
| the repo or git cannot be read | **cannot tell -- stays open** |

The recorded tip SHA is what makes `discarded` decidable after the branch is
deleted, which is why it is captured when the item is created rather than read
at reconcile time.

The fourth row is the same three-valued rule as `incidents` and `actions.py`'s
exit 2: not being able to check is its own answer, and it is never the good one.

### It never deletes a worktree

ORIGINATION-DESIGN.md section 3a: *"cleanup is **never automatic**. A worktree
holds unreviewed work; deleting one destroys it."* A cleared item reports that
its worktree is now removable and leaves it exactly where it is. The nearest
this module comes to acting is printing a path.

### The file is a rendering, not the record

`REVIEW-QUEUE.md` is regenerated from the items in Mimir, the way `ALERTS-NOW.md` is
rendered from `alerts`. Line shapes are preserved exactly, because
`run-morning-digest.ps1` greps `^\\s*-` out of it under powershell.exe 5.1 --
and for the same reason every byte written here stays ASCII.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from . import store
from ._mimir import records as mr
from .host import host
from .store import log

REVIEW_QUEUE = store.STATE / "REVIEW-QUEUE.md"
WORKTREES = store.WORKTREES

ACCEPTED = "accepted"
DISCARDED = "discarded"
UNKNOWN = "unknown"

# Items older than this are called out as aging in the rendered file. Not a
# deadline and nothing acts on it -- the point is that "landed an hour ago" and
# "has been sitting for three weeks" must not look identical, which in an
# append-only file they did.
AGING_S = 7 * 86400

# One Mimir record per item (kind `brokkr.review_item`, key = its id). The data holds the columns
# id, item_key, source, task_id, repo, branch, worktree, tip_sha, summary, waiting_since,
# last_seen_at, cleared_at, outcome, evidence, review_min and brain_verdict. `review_min` is
# optional, and the only field a human can supply: everything else is observed. While an item
# is open it also carries `open: true` and `open_key: <item_key>`, which `clear()` removes.
# So an open key is found by `where=open_key:<key>`, and a cleared item keeps its history: a
# branch reviewed, deleted and re-created has two records.
KIND = "brokkr.review_item"
_OPEN_COLS = ("id", "item_key", "source", "task_id", "repo", "branch", "worktree",
              "tip_sha", "summary", "waiting_since", "last_seen_at", "brain_verdict")


def _items(**query) -> list[dict]:
    """The data of the matching records, ids as ints. Raises MimirDown when Mimir does not answer."""
    out = []
    query.setdefault("limit", 5000)
    for row in mr.query(kind=KIND, source=store.SOURCE, **query):
        item = row["data"]
        item["id"] = int(item.get("id") or row["key"])
        out.append(item)
    return out


def cleared_since(cutoff: float) -> list[dict]:
    """Items whose fate was decided at or after `cutoff` (epoch seconds)."""
    return [i for i in _items(since=cutoff) if (i.get("cleared_at") or 0) >= cutoff]


def _cfg(cfg: Mapping[str, Any] | None = None) -> dict:
    cfg = cfg if cfg is not None else host.load_config()
    section = dict(cfg.get("review") or {})
    section.setdefault("enabled", True)
    return section


def add(
    source: str,
    summary: str,
    task_id: str = "",
    repo: str = "",
    branch: str = "",
    worktree: str = "",
    tip_sha: str = "",
) -> int | None:
    """Record something finished and waiting. Idempotent per (source, key).

    Re-adding an open item refreshes its summary and `last_seen_at` but keeps
    `waiting_since`, so the age shown is the age of the *wait*, not of the last
    time something touched the row. That distinction is the entire value of
    having a table here.
    """
    try:
        now = time.time()
        key = f"{source}:{repo}:{branch}" if branch else f"{source}:{task_id or summary[:60]}"
        found = _items(where="open_key:" + json.dumps(key), limit=1)
        if found:
            data = {"summary": summary[:500], "last_seen_at": now}
            if tip_sha:
                data["tip_sha"] = tip_sha
            mr.patch(store.SOURCE, KIND, str(found[0]["id"]), data, at=now)
            return found[0]["id"]
        # shortcut: a new id is max+1, claimed with put-if-absent. Two writers racing for one id retry
        # (5 tries). Two racing for one key could each open it; Brokkr runs one reconcile at a time.
        for _ in range(5):
            item_id = int(mr.agg("max", kind=KIND, field="id", source=store.SOURCE) or 0) + 1
            data = {"id": item_id, "item_key": key, "source": source, "task_id": task_id, "repo": repo,
                    "branch": branch, "worktree": worktree, "tip_sha": tip_sha, "summary": summary[:500],
                    "waiting_since": now, "last_seen_at": now, "open": True, "open_key": key}
            if mr.put(store.SOURCE, KIND, str(item_id), data, at=now, if_absent=True, wait=True):
                return item_id
        return None
    except Exception as exc:  # noqa: BLE001 - the work is already done; never fail on bookkeeping
        # Mimir down: the item is not recorded, and its worktree shows as an orphan until it is re-added.
        log.warning("review: could not record %s/%s: %s", source, task_id or branch, exc)
        return None


def waiting() -> list[dict]:
    """The open items, oldest wait first. Raises MimirDown when Mimir does not answer."""
    items = _items(where="open:true")
    items.sort(key=lambda i: i["waiting_since"])
    return [{c: i.get(c) for c in _OPEN_COLS} for i in items]


def verdict_line(text: str) -> str:
    """The `VERDICT: ...` line of a brain answer, else its first non-blank line.

    The prompt asks for the verdict first, and the first real answer
    (2026-09-13) led with a one-line summary instead -- so a first-line rule
    showed everything except the verdict."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return next((ln for ln in lines if ln.upper().startswith("VERDICT:")), lines[0] if lines else "")


def attach_verdict(task_id: str, verdict: str) -> bool:
    """Record the brain's verdict on the open item for `task_id`, then re-render.

    Returns whether an open item took it. No item is normal: a NEEDS REVIEW
    task that produced no commits has no branch to review, and the verdict
    still reaches status.py and the digest from the escalation row.
    """
    took = False
    for item in _items(where="task_id:" + json.dumps(task_id)):
        if item.get("open"):
            took |= bool(mr.patch(store.SOURCE, KIND, str(item["id"]), {"brain_verdict": verdict[:1000]},
                                  if_={"open": True}, wait=True))
    if took:
        render()
    return took


def clear(item_id: int, outcome: str, evidence: str) -> bool:
    now = time.time()
    return bool(mr.patch(store.SOURCE, KIND, str(item_id),
                         {"cleared_at": now, "outcome": outcome, "evidence": evidence[:300],
                          "open": None, "open_key": None},
                         at=now, if_={"open": True}, wait=True))


# --------------------------------------------------------------------------
# Observing the fate of a branch
# --------------------------------------------------------------------------


async def _git(repo: str, *args: str) -> tuple[int, str]:
    return await host.run_process(["git", "-C", repo, *args], timeout_s=60)


async def _base_branch(repo: str) -> str:
    """The branch merged work lands on. Read, never guessed.

    Same rule and same fallback as `queue.py`'s copy: `general\\caddy` has no
    remote at all, and defaulting to "main" would fail there while appearing to
    work everywhere else.
    """
    code, out = await _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if code == 0 and out.strip():
        return out.strip().rsplit("/", 1)[-1]
    code, out = await _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if code == 0 and out.strip() else ""


async def fate(item: dict) -> tuple[str, str] | None:
    """`(outcome, evidence)` once the branch's fate is decidable, else None.

    None means **cannot tell**, and the item stays in the queue. A repo that has
    moved, a git that is not on PATH, or a base branch that cannot be read all
    land here rather than being rounded to a verdict -- an item silently cleared
    is unreviewed work with nothing left pointing at it, which under "no push"
    is the only copy.
    """
    repo, branch = item.get("repo") or "", item.get("branch") or ""
    if not repo or not branch or not Path(repo).exists():
        return None

    base = await _base_branch(repo)
    if not base:
        return None

    code, _ = await _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    branch_exists = code == 0

    if branch_exists:
        merged, _ = await _git(repo, "merge-base", "--is-ancestor", branch, base)
        if merged == 0:
            return ACCEPTED, f"{branch} is an ancestor of {base}; worktree can be removed"
        # Alive and unmerged: still the human's to look at.
        return None

    # The branch is gone. Without a recorded tip there is nothing left to test
    # it against, and guessing "accepted" because a branch vanished would clear
    # exactly the work someone deleted unreviewed.
    tip = item.get("tip_sha") or ""
    if not tip:
        return UNKNOWN, f"{branch} no longer exists and no tip was recorded"

    code, _ = await _git(repo, "cat-file", "-e", f"{tip}^{{commit}}")
    if code != 0:
        return UNKNOWN, f"{branch} is gone and its tip {tip[:8]} is unreachable"

    merged, _ = await _git(repo, "merge-base", "--is-ancestor", tip, base)
    if merged == 0:
        return ACCEPTED, f"{branch} was merged into {base} and deleted"
    return DISCARDED, f"{branch} was deleted without reaching {base} (tip {tip[:8]})"


async def reconcile(cfg: Mapping[str, Any] | None = None) -> list[str]:
    """Ask git what happened to every waiting item. Returns what changed."""
    if not _cfg(cfg)["enabled"]:
        return []
    changed: list[str] = []
    for item in waiting():
        try:
            verdict = await fate(item)
        except Exception as exc:  # noqa: BLE001 - one bad repo must not stall the rest
            log.debug("review: could not judge #%s: %s", item["id"], exc)
            continue
        if verdict is None:
            continue
        outcome, evidence = verdict
        if outcome == UNKNOWN:
            # Decidably undecidable: keep it in the queue, but say so, because
            # "we cannot tell what happened to this branch" is itself something
            # the human should see rather than a silent hold.
            continue
        if clear(item["id"], outcome, evidence):
            changed.append(f"cleared #{item['id']} {item['branch'] or item['task_id']} - {outcome}: {evidence}")
    if changed:
        render()
    return changed


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _age(seconds: float) -> str:
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def render() -> None:
    """Rewrite `REVIEW-QUEUE.md` from the open items.

    ASCII only, and every line of an item starts with a dash, the shape the
    original writer used: `run-morning-digest.ps1` pulls `^\\s*-` lines straight
    into the digest under powershell.exe 5.1, which reads anything non-ASCII as
    mojibake.
    """
    items = waiting()
    now = time.time()
    lines = [
        "# Review queue",
        "",
        "Finished autonomous work waiting for review. Nothing here has been",
        "pushed. Rendered from the `brokkr.review_item` records in Mimir by `brokkr/review.py`;",
        "surfaced by `morning-digest` and `status.py`.",
        "",
        "Items leave this file when git says what happened to the branch -- merged",
        "into the base branch is `accepted`, deleted unmerged is `discarded`. There",
        "is nothing to tick, and worktrees are never removed automatically.",
        "",
    ]
    if not items:
        lines.append("Nothing waiting.")
    for item in items:
        age = _age(now - item["waiting_since"])
        aging = "  **(aging)**" if now - item["waiting_since"] > AGING_S else ""
        lines.append(
            f"- **{item['task_id'] or item['item_key']}** | `{Path(item['repo']).name if item['repo'] else '?'}` "
            f"| branch `{item['branch'] or '?'}` | waiting {age}{aging}"
        )
        lines.append(f"  - {item['summary']}")
        if item.get("brain_verdict"):
            lines.append(f"  - brain (read-only second look): {verdict_line(item['brain_verdict'])[:200]}")
        if item["repo"] and item["branch"]:
            lines.append(f"  - review: `git -C \"{item['repo']}\" diff {item['branch']}`")
        if item["worktree"]:
            lines.append(f"  - worktree: `{item['worktree']}`")
    body = "\n".join(lines) + "\n"
    try:
        tmp = REVIEW_QUEUE.with_suffix(".tmp")
        tmp.write_text(body, encoding="ascii", errors="replace")
        tmp.replace(REVIEW_QUEUE)
    except OSError as exc:
        log.warning("review: could not write %s: %s", REVIEW_QUEUE.name, exc)


def orphan_worktrees() -> list[str]:
    """Worktree directories with no open review item pointing at them.

    Reported, never removed. This is the debt pile ORIGINATION-DESIGN.md 3a
    creates on purpose by refusing automatic cleanup -- naming it is the most
    this module is allowed to do about it.
    """
    if not WORKTREES.exists():
        return []
    claimed = {Path(i["worktree"]).name for i in waiting() if i["worktree"]}
    try:
        return sorted(p.name for p in WORKTREES.iterdir() if p.is_dir() and p.name not in claimed)
    except OSError:
        return []


def _outcomes(cleared: list[dict]) -> dict:
    """{outcome: (count, mean seconds waited)} over cleared items."""
    out: dict = {}
    for item in cleared:
        out.setdefault(item.get("outcome"), []).append(item["cleared_at"] - item["waiting_since"])
    return {o: (len(w), sum(w) / len(w)) for o, w in out.items()}


def report(days: int = 30) -> str:
    now = time.time()
    outcomes = _outcomes(cleared_since(now - days * 86400))

    items = waiting()
    lines = [f"review queue - last {days}d", "", f"  waiting : {len(items)}"]
    if items:
        oldest = min(i["waiting_since"] for i in items)
        lines.append(f"  oldest  : {_age(now - oldest)}")
    for outcome, (count, avg_wait) in outcomes.items():
        lines.append(f"  {outcome or '?':10}: {count}  (median-ish wait {_age(avg_wait or 0)})")
    if items:
        lines += ["", "  WAITING:"]
        for item in items:
            lines.append(
                f"    #{item['id']:<4} {_age(now - item['waiting_since']):>5}  "
                f"{(item['task_id'] or '?'):18} {(item['branch'] or '?'):26} {item['summary'][:60]}"
            )
    orphans = orphan_worktrees()
    if orphans:
        lines += ["", f"  worktrees with no open item ({len(orphans)}) - removal is yours:"]
        lines += [f"    {WORKTREES / name}" for name in orphans]
    return "\n".join(lines)


def set_minutes(item_id: int, minutes: float) -> bool:
    """The one number a human can add. Everything else here is observed.

    Optional by design. `ORIGINATION-DESIGN.md`'s whole premise is that the
    user is a gatekeeper rather than an operator, so a ledger that only works
    when they fill in a form is a ledger that will be empty. What it unlocks is
    named explicitly in `ledger()` and withheld rather than estimated when it
    is missing.
    """
    return bool(mr.patch(store.SOURCE, KIND, str(item_id), {"review_min": float(minutes)}, wait=True))


def ledger(days: int = 30) -> str:
    """Is the autonomy worth what it costs, in numbers nobody had to type.

    `ORIGINATION-DESIGN.md` already names the real cost of Option A -- *"what is
    not recoverable is the time spent reviewing bad commits -- which is what G13
    measures and why a failing G13 means deleting the proposer, not tuning it"*
    -- and schedules G13 as a one-off over the first 8 originated tasks. This is
    the same question asked continuously, from data that accrues on its own.

    **What is measured, and how honestly:**

      * `accepted` / `discarded` come from git, and are as good as the repo.
      * `time to review` is wall-clock from the branch landing to its fate being
        decided. It is *latency*, not effort, and is labelled as such.
      * spend is everything autonomous work cost over the window, divided by
        accepted changes: the implementer's runs (`job_calls`), the brain's
        origination and escalation calls (`brain_calls`) and criteria scoring
        (`provider_calls`), all read from Mimir. Until 2026-09-13 it read `provider_calls` alone,
        which had one row, ever -- so the figure left out the implementer, the
        one component that costs real money per task. It is **amortised, not
        attributed**: none of those tables carries a task id, so a per-task
        figure would be a guess dressed as a measurement. Named here so nobody
        reads it as one.
      * cost per accepted change in *human minutes* is the number that actually
        decides whether this pipeline should exist, and it is the one number
        that cannot be observed. It is printed only when review minutes have
        been supplied, and its absence is stated rather than filled in.

    That last rule is the same one the criteria scorer follows: a check that did
    not run must never render as a check that passed.
    """
    now = time.time()
    cutoff = now - days * 86400
    # outcome -> [outcome, count, mean seconds waited, review minutes, items with minutes]
    by: dict = {}
    for item in cleared_since(cutoff):
        r = by.setdefault(item.get("outcome"), [item.get("outcome"), 0, 0.0, 0.0, 0])
        r[1] += 1
        r[2] += item["cleared_at"] - item["waiting_since"]
        if item.get("review_min") is not None:
            r[3] += item["review_min"]
            r[4] += 1
    for r in by.values():
        r[2] /= r[1]
    rows = list(by.values())
    # (label, Mimir stream, extra filter). Each source is read on its own so a stream that has
    # nothing yet costs its line, not the whole ledger. The implementer's runs and the scoring calls
    # are Fafnir's records since 2026-10-17; the brain's calls are Jarvis's. Scoring is the
    # criteria-score surface only: until 2026-10-17 it read all of provider_calls, which also
    # holds every brain call, so brain spend was counted twice.
    sources = (
        ("implementer", "fafnir.job_call", "job:implementer"),
        ("brain", "jarvis.brain_calls", None),
        ("scoring", "fafnir.provider_call", "surface:criteria-score"),
    )
    spent_by: list[tuple[str, float, int, int]] = []
    for label, stream, where in sources:
        try:
            q = {"stream": stream, "since": cutoff, "where": where}
            total = mr.agg("sum", field="cost_usd", **q) or 0
            priced_n = mr.agg("count", field="cost_usd", **q)
            calls_n = mr.agg("count", **q)
        except mr.MimirError:
            continue
        spent_by.append((label, float(total), int(priced_n), int(calls_n)))
    spend = sum(s[1] for s in spent_by)
    calls = sum(s[3] for s in spent_by)
    priced = sum(s[2] for s in spent_by)

    accepted = by.get(ACCEPTED, (None, 0, 0, 0, 0))[1]
    discarded = by.get(DISCARDED, (None, 0, 0, 0, 0))[1]
    decided = accepted + discarded
    open_items = waiting()

    lines = [f"review ledger - last {days}d", ""]
    lines.append(f"  decided            : {decided}  (accepted {accepted}, discarded {discarded})")
    lines.append(f"  still waiting      : {len(open_items)}")
    if decided:
        lines.append(f"  accept rate        : {100.0 * accepted / decided:.0f}%")
    else:
        # An empty ledger is a fact about the ledger, not a verdict about the
        # pipeline, and it must not read like one.
        lines.append("  accept rate        : no work has been reviewed yet - "
                     "no verdict available")

    for outcome, (_o, count, avg_wait, _sum_min, _n_min) in by.items():
        if avg_wait:
            lines.append(f"  {str(outcome or '?'):18} : {count}, decided after {_age(avg_wait)} (latency, not effort)")

    lines.append("")
    lines.append(f"  autonomy spend     : ${spend:.2f} over {calls} call(s) "
                 f"({priced} priced, {calls - priced} unpriced)")
    if spent_by:
        lines.append("                       " + ", ".join(
            f"{label} ${total:.2f} ({n} call(s))" for label, total, _p, n in spent_by))
    if accepted and spend:
        lines.append(f"  $ per accepted     : ${spend / accepted:.2f}  "
                     "(AMORTISED across all autonomy spend, not attributed per task)")

    minutes_rows = sum(r[4] for r in rows)
    total_min = sum(r[3] for r in rows)
    lines.append("")
    if minutes_rows and accepted:
        lines.append(f"  review minutes     : {total_min:.0f} over {minutes_rows} item(s)")
        lines.append(f"  minutes per accept : {total_min / accepted:.1f}")
        lines.append("  -> compare against how long the change would have taken by hand. "
                     "If this is larger, the pipeline is costing more than it saves.")
    else:
        lines.append("  minutes per accept : NOT MEASURED - no review minutes recorded.")
        lines.append("     This is the number that decides whether the pipeline is worth")
        lines.append("     keeping, and it cannot be observed from git. Record one with")
        lines.append("     `forge review minutes <id> <minutes>`; it is optional,")
        lines.append("     and this line stays honest about being absent rather than")
        lines.append("     estimating it.")
    return "\n".join(lines)


def _cli(argv: list[str]) -> int:
    import asyncio

    if argv[:1] == ["reconcile"]:
        changed = asyncio.run(reconcile())
        for line in changed:
            print(line)
        print(f"{len(changed)} change(s)")
        return 0
    if argv[:1] == ["render"]:
        render()
        print(f"wrote {REVIEW_QUEUE}")
        return 0
    if argv[:1] == ["ledger"]:
        print(ledger(int(argv[1]) if len(argv) > 1 else 30))
        return 0
    if argv[:1] == ["minutes"] and len(argv) >= 3:
        try:
            ok = set_minutes(int(argv[1]), float(argv[2]))
        except ValueError as exc:
            print(f"review: bad minutes call: {exc}")
            return 2
        print(f"recorded {argv[2]} minutes on #{argv[1]}" if ok else f"no item #{argv[1]}")
        return 0 if ok else 1
    print(report())
    # Exit 1 when something is waiting, so a script can branch without parsing.
    return 1 if waiting() else 0

