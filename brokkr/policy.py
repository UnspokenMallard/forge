"""Phase 4 step 1: decide what deserves a model, using plain `if` statements.

Drafted 2026-08-28. **Not wired into anything yet** -- `main.py` does not import
this module, and neither does `handlers.load_all()`, which only imports
`handlers/*.py`. It is safe on disk and does nothing until something calls it.

The phase's whole risk is spending tokens (and the only two cores on this box)
on events that a comparison already answers. So classification is deliberately
mechanical, environment-specific, and readable -- no model participates in
deciding whether to call a model.

Measured against the live database 2026-08-28, which is what shapes every rule
below:

    queue-dispatch         231.6/day  |  ps-script   196.6/day
    verify-delegated-work    2.1/day  |  check-lesson-duplicate  0.3/day
    python-script            0.6/day  |  token-report            0.1/day

428 events/day, **99.4% of them the clock**. The judgment-shaped surface is
~2.4 events/day, of which ~35% of `verify-delegated-work` runs come back
`NEEDS REVIEW` (12 of 34 historically) -- so ~0.7 escalations/day at the
proposed rules. See PHASE4-DESIGN.md for the full reasoning.

Three decisions:

    ignore    -- carried no information; do not count it in any rate
    local     -- the existing mechanical handler's verdict stands
    escalate  -- a model is worth paying for

`local` is the default, and `escalate` is opt-in per kind, per PLAN.md.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Mapping

from . import store
from ._mimir import records as mr
from .host import host
from .store import log

Decision = str  # "ignore" | "local" | "escalate"

IGNORE = "ignore"
LOCAL = "local"
ESCALATE = "escalate"

# Kinds that are the clock. A scheduled job either produced its asserted
# artifact or it did not, and that comparison is the verdict -- there is no
# judgment left for a model to add. These are 99.4% of all traffic, so the
# single most important property of this module is that they can never
# escalate; PHASE4-DESIGN.md §2 makes that a gate rather than an intention.
SCHEDULED_KINDS = frozenset(
    {"noop", "queue-dispatch", "ps-script", "python-script", "token-report"}
)

# Kinds where a model could add something the handler cannot. Everything else
# is `local` by default, including kinds added later -- a new handler does not
# become eligible to spend money by existing.
ESCALATION_ELIGIBLE_KINDS = frozenset({"verify-delegated-work"})

# Task files created to fail. Replaying all 6,127 historical events through
# `classify()` on 2026-08-28 showed **7 of 12** would-be escalations were these
# -- `smoke-test-*`, `ZZ-SELFTEST-*`, `smoke-2026*` -- fixtures written by the
# selftest and by cutover smoke tests, several of which assert the NEEDS REVIEW
# path itself. Paying a model to review a file whose purpose is to be reviewed
# badly is the same waste as escalating docker-runaway-check's deliberate
# exit 1.
#
# The tradeoff, stated plainly: a genuine task named `smoke-something` would be
# skipped. These prefixes are this environment's fixture convention and the
# handler's own results show them used that way, so the trade is worth making
# -- but it is a naming convention, not a guarantee.
_FIXTURE = re.compile(r"^(zz-selftest|smoke)", re.IGNORECASE)

def _task_name(event: Mapping[str, Any]) -> str:
    """Basename of the task file an event refers to, '' when there is none."""
    payload = event.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return ""
    if not isinstance(payload, Mapping):
        return ""
    path = str(payload.get("path") or "")
    return path.replace("\\", "/").rsplit("/", 1)[-1]

# The dry-run log: one Mimir telemetry row per classified event, in the stream below. A row holds
# event_id, kind, decision, reason, eligible (0/1), ts and wanted -- what the policy WANTED, so the gate
# need not re-derive it from `reason`. Mimir keeps telemetry 90 days.
STREAM = "brokkr.classification"
EVENTS_QUEUE = "jarvis.events"


def classify(event: Mapping[str, Any]) -> tuple[Decision, str]:
    """Return (decision, reason) for one event. Never raises, never calls out.

    Expects the columns `events` already has: `kind`, `state`, `attempts`,
    `result`. `result` matters because the escalation-worthy cases are only
    recognisable *after* the mechanical handler has run -- the question this
    phase asks is not "should a model do this work" but "should a model review
    the verdict the handler just produced".

    The reason string is returned rather than logged here so the dry-run log
    can record exactly why, which is what makes a bad rule visible in review
    instead of only in a bill.
    """
    kind = str(event.get("kind") or "")
    state = str(event.get("state") or "")
    result = str(event.get("result") or "")

    # --- the clock ---------------------------------------------------------
    if kind in SCHEDULED_KINDS:
        if state == "failed":
            # Deliberately NOT escalated in the first cut. Several jobs here
            # exit 1 to mean "I acted, look at me" rather than "I broke":
            # docker-runaway-check exits 1 after successfully restarting
            # Docker, cursor-quota-reminder failed daily by design until it
            # was acknowledged 2026-08-28, and probe-liar exists to fail. A
            # model asked to interpret those would be paid to re-derive
            # something already understood, so this stays local until the
            # exit-1 idiom is encoded as data rather than as prose.
            return LOCAL, f"{kind} failed; scheduled-job failures are local in the first cut"
        if not result or "no pending entries" in result:
            return IGNORE, f"{kind} did no work"
        return LOCAL, f"{kind} is mechanical and artifact-asserted"

    # --- delegated work ----------------------------------------------------
    if kind == "verify-delegated-work":
        if state == "failed":
            return LOCAL, "handler itself failed; that is an operational fault, not a judgment call"
        if _FIXTURE.match(_task_name(event)):
            return LOCAL, "test fixture task file; its verdict is the point of the fixture"
        if "NEEDS REVIEW" in result:
            # The one case this phase exists for. The handler can see that a
            # diff exists, that the tree is dirty, that no criteria were
            # recorded -- it cannot see whether the diff *satisfies* the
            # criteria, which is the half PLAN.md's Phase 3 has been waiting
            # on this phase to finish.
            return ESCALATE, "mechanical verdict is NEEDS REVIEW; whether the diff meets the criteria needs judgment"
        return LOCAL, "gate green; nothing left to judge"

    # --- anything else -----------------------------------------------------
    # Default local, per PLAN.md. A kind added later must be opted in
    # explicitly above; it does not become eligible to spend by existing.
    return LOCAL, f"kind '{kind}' is not opted in to escalation"


def escalation_enabled(cfg: Mapping[str, Any] | None = None) -> bool:
    """Whether escalation may actually fire. Defaults to False.

    Step 1 of the phase runs with this off: classify, log, change nothing. The
    default is False in code as well as in config, so a missing config key
    cannot switch spending on.
    """
    cfg = cfg if cfg is not None else host.load_config()
    brain = cfg.get("brain") or {}
    return bool(brain.get("escalation_enabled", False))


# The reason prefix a downgraded escalate is recorded with. The dry-run log's
# `decision` field holds what the daemon DID; this prefix is how the policy's
# wish is read back from it (`_wanted()`, stored as `wanted`).
WOULD_HAVE = "escalation disabled; would have escalated: "


def decide(event: Mapping[str, Any], cfg: Mapping[str, Any] | None = None) -> tuple[Decision, str]:
    """`classify()`, then downgrade `escalate` to `local` while escalation is off.

    Separate from `classify()` so the dry-run log records what the policy
    *wanted* (the number PHASE4-DESIGN.md §2's gate is measured on) while the
    daemon does what is currently allowed. Collapsing the two would make the
    dry run measure the switch instead of the policy.
    """
    decision, reason = classify(event)
    if decision == ESCALATE and not escalation_enabled(cfg):
        return LOCAL, f"{WOULD_HAVE}{reason}"
    return decision, reason


def _wanted(decision: str, reason: str | None) -> str:
    """What the policy wanted for one dry-run log row, rather than what ran."""
    if decision == LOCAL and str(reason or "").startswith(WOULD_HAVE):
        return ESCALATE
    return decision


def record(event: Mapping[str, Any], decision: Decision, reason: str) -> None:
    """Append one row to the dry-run log (Mimir telemetry). Best effort -- never fails an event.

    A classification that raises would turn a diagnostic into an outage, and
    this module's entire purpose during step 1 is to be observable without
    being load-bearing.
    """
    try:
        kind = str(event.get("kind") or "")
        now = time.time()
        mr.append(store.SOURCE, STREAM, {
            "event_id": event.get("id"),
            "kind": kind,
            "decision": decision,
            "reason": reason,
            "eligible": 1 if kind in ESCALATION_ELIGIBLE_KINDS else 0,
            "ts": now,
            "wanted": _wanted(decision, reason),
        }, at=now)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not break the queue
        log.warning("policy: failed to record classification: %s", exc)


# --------------------------------------------------------------------------
# The dry-run gate (PHASE4-DESIGN.md §2)
# --------------------------------------------------------------------------
#
# PLAN.md's original gate -- ">= 200 events classified, >90% resolve locally"
# -- cannot fail here. 428 events/day means it is satisfied in half a day by
# cron ticks alone, and a policy that escalated *every* judgment-shaped event
# would still score 99.4% local. What follows replaces it with thresholds that
# a bad policy actually trips.

MIN_DAYS = 10
MIN_ELIGIBLE = 25
MAX_ESCALATION_RATE = 0.50

# Phase 6 §1a. The replacement gate above fixed a gate that could not FAIL and
# produced one that, on this workload, cannot PASS: measured 2026-08-30, the
# dry run had been recording for 2.0 days with **0** eligible events, because
# `verify-delegated-work` last fired 2026-08-25 and `check-lesson-duplicate`
# 2026-08-28 10:10 -- both before the dry run started at 2026-08-28 23:05. The
# ~2.4 eligible events/day the threshold was sized against came from a
# mid-August burst of Cursor delegation that has since stopped; it was never a
# steady-state rate.
#
# A bare FAIL reads as "wait longer", which is wrong and expensive: it dates a
# gate that will not open until the user hand-generates the sample. So the gate
# now reports its own SUPPLY and says so. This changes no threshold -- lowering
# one to fit the answer is the failure PHASE4-DESIGN.md §2 exists to name.
#
# Horizon for calling it blocked rather than slow: reaching the floor should be
# plausible inside one window, since the sample was kept 30 days (`[retention]
# classification_days`) until 2026-10-24, when it moved to Mimir (telemetry, 90
# days). The horizon stays 30: a projection past it is still a sign of a gate that
# will not open in any useful time.
SUPPLY_HORIZON_DAYS = 30


def supply(days: int = MIN_DAYS) -> tuple[bool, str]:
    """Is the gate reachable at the eligible-event rate actually observed?

    Returns (blocked, detail). Separate from `gate()` because it answers a
    different question: `gate()` says whether the policy has been shown to be
    safe, this says whether it can ever be shown. A gate whose verdict depends
    on a workload nobody controls needs to report that out loud -- see the
    SUPPLY_HORIZON_DAYS comment for what went wrong without it.
    """
    cutoff = time.time() - days * 86400
    n_rows = mr.agg("count", stream=STREAM, since=cutoff)

    if not n_rows:
        return False, "no classifications yet -- too early to judge supply"

    span_days = max((time.time() - mr.agg("min", stream=STREAM, field="ts", since=cutoff)) / 86400, 1e-9)
    n_eligible = mr.agg("count", stream=STREAM, since=cutoff, where="eligible:1")
    if n_eligible >= MIN_ELIGIBLE:
        return False, f"floor already met ({n_eligible} eligible)"

    # The replay counts toward the floor (§5a-2), so it also decides whether
    # supply is blocked -- otherwise the CLI would print SUPPLY-BLOCKED under a
    # gate whose sample row reads PASS, and the two would have to be reconciled
    # by a human every time.
    replayed = replay_sample()["eligible"]
    if n_eligible + replayed >= MIN_ELIGIBLE:
        return False, (
            f"floor met with the replay ({n_eligible} live + {replayed} replay); "
            f"live supply is still {n_eligible / span_days:.2f}/day, so the sample stays replay-carried"
        )

    per_day = n_eligible / span_days
    missing = MIN_ELIGIBLE - n_eligible
    if per_day <= 0:
        return True, (
            f"0 eligible events in {span_days:.1f}d -- at this rate the gate never opens. "
            "Eligible kind is verify-delegated-work; "
            "it has not fired since the dry run began."
        )

    eta_days = missing / per_day
    if eta_days > SUPPLY_HORIZON_DAYS:
        return True, (
            f"{n_eligible} eligible in {span_days:.1f}d = {per_day:.2f}/day; "
            f"{missing} more needed = ~{eta_days:.0f}d, past the {SUPPLY_HORIZON_DAYS}d "
            "window the sample is judged over"
        )
    return False, f"{per_day:.2f} eligible/day, ~{eta_days:.0f}d to the floor"


def replay_sample() -> dict[str, Any]:
    """Replay `classify()` over retained events the live dry run never saw.

    PHASE6-DESIGN.md §5a-2, chosen 2026-09-03 over lowering the floor (§5a-3,
    rejected there) and over hand-generating 25 delegations. The argument: the
    sample floor exists to prove the policy has been exercised on enough real
    judgment-shaped events to trust it, and the events sitting in this database
    from before 2026-08-28 23:05 are exactly that -- real handler results,
    classified by the same pure function, differing from live rows only in when
    they were scored. Counting them is not lowering a threshold; refusing to
    count them measures the calendar rather than the policy.

    Two properties keep it honest:

    - **Labelled, never merged.** The counts stay separate from live ones all
      the way to the printed verdict, so nobody reads a replayed sample as
      evidence the deployed classifier ran in production for that long. The
      duration floor is live-only for the same reason -- replay proves the rule
      is sound, only live time proves it is *wired*.
    - **Anti-joined on `event_id`, not on a timestamp.** Any event already
      carrying a classification row is excluded outright, so an event can never
      be counted twice however the two windows overlap.

    Recomputed on every call rather than stored, so it can never assert a
    measurement this code could not reproduce right now. The cost is that
    `core.prune_events` is deleting the corpus underneath it at 14 days: this
    sample shrinks over time and will eventually stop clearing the floor on its
    own. That is a real limit, reported rather than hidden -- see `gate()`.
    """
    # The events are in Jarvis's queue and the classifications in Brokkr's stream, so the
    # anti-join is done here. Only the kinds that can matter are read: the eligible kind, and
    # the scheduled kinds that must never escalate. Events are kept 14 days.
    rows = []
    for kind in sorted(ESCALATION_ELIGIBLE_KINDS | SCHEDULED_KINDS):
        classified = mr.agg("count", stream=STREAM, by="event_id", where="kind:" + json.dumps(kind))
        for e in mr.queue_all(store.SOURCE, EVENTS_QUEUE, kind=kind):
            if str(e["id"]) not in classified:
                rows.append((e["id"], kind, e.get("state"), e.get("result"), e.get("payload"), e["created_at"]))

    eligible = escalated = scheduled_escalations = 0
    first_ts = last_ts = None
    for _id, kind, state, result, payload, ts in rows:
        event = {"kind": kind, "state": state, "result": result, "payload": payload}
        decision, _ = classify(event)
        if kind in ESCALATION_ELIGIBLE_KINDS:
            eligible += 1
            if decision == ESCALATE:
                escalated += 1
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        if kind in SCHEDULED_KINDS and decision == ESCALATE:
            scheduled_escalations += 1

    return {
        "events": len(rows),
        "eligible": eligible,
        "escalated": escalated,
        "scheduled_escalations": scheduled_escalations,
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


def gate(days: int = MIN_DAYS) -> tuple[bool, list[str]]:
    """Evaluate the dry-run gate. Returns (passed, lines) -- each line states
    the number required and the number observed, per PLAN.md's rule.

    The sample floor, the escalation rate and the scheduled-escalation check
    read live rows *plus* the labelled replay (see `replay_sample()`); the
    duration floor reads live rows only.
    """
    cutoff = time.time() - days * 86400
    if not mr.agg("count", stream=STREAM, since=cutoff):
        return False, ["no classifications recorded yet"]
    # The duration floor asks how long the classifier has run live, so it is
    # measured from the first row on file. Until 2026-09-13 it was measured
    # inside the window, whose length equals the floor -- a span that can
    # never reach it, so the check printed "10.0d of 10d" and FAILed forever.
    first_ts = mr.agg("min", stream=STREAM, field="ts")

    replay = replay_sample()

    span_days = (time.time() - first_ts) / 86400
    eligible = [r["data"] for r in mr.query(stream=STREAM, since=cutoff, where="eligible:1", limit=5000)]
    # What the policy WANTED, not what ran. While escalation is off, decide()
    # stores an escalate as `local` with a WOULD_HAVE reason, and until
    # 2026-09-13 this counted the stored decision -- so every live escalation
    # read as local and the rate check measured the switch, the exact thing
    # decide() exists to prevent. The three NEEDS REVIEW rows of 2026-08-31
    # were counted that way.
    escalated = [r for r in eligible if (r.get("wanted") or _wanted(r["decision"], r["reason"])) == ESCALATE]
    scheduled_escalations = [
        r["data"] for r in mr.query(stream=STREAM, since=cutoff, where="wanted:" + ESCALATE, limit=5000)
        if r["data"]["kind"] in SCHEDULED_KINDS
    ]

    n_eligible = len(eligible) + replay["eligible"]
    n_escalated = len(escalated) + replay["escalated"]
    n_scheduled = len(scheduled_escalations) + replay["scheduled_escalations"]
    rate = (n_escalated / n_eligible) if n_eligible else 0.0

    checks = [
        ("dry-run duration", span_days >= MIN_DAYS, f"{span_days:.1f}d of {MIN_DAYS}d required (live only)"),
        (
            "eligible sample",
            n_eligible >= MIN_ELIGIBLE,
            f"{n_eligible} of {MIN_ELIGIBLE} required "
            f"({len(eligible)} live + {replay['eligible']} replay)",
        ),
        (
            "escalation rate",
            rate <= MAX_ESCALATION_RATE,
            f"{rate:.0%} of eligible (limit {MAX_ESCALATION_RATE:.0%}) "
            f"({len(escalated)} live + {replay['escalated']} replay)",
        ),
        # The row that actually protects the budget: scheduled jobs are 99.4%
        # of traffic, so this is the only way this phase produces a runaway
        # bill. Binary, and observable on day one.
        ("no scheduled-job escalations", not n_scheduled, f"{n_scheduled} found (limit 0)"),
    ]
    lines = [f"  {'PASS' if ok else 'FAIL'}  {name:32} {detail}" for name, ok, detail in checks]

    # Said out loud whenever the replay is what clears the floor: the events
    # table is pruned at 14 days, so this support is wasting, and a gate that
    # passes today on a shrinking corpus should say so rather than let a later
    # reader assume the sample is durable.
    if len(eligible) < MIN_ELIGIBLE <= n_eligible:
        lines.append(
            f"        note: the floor is carried by {replay['eligible']} replayed events, "
            "which core.prune_events deletes at 14 days"
        )
    return all(ok for _, ok, _ in checks), lines
