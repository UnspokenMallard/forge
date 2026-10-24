"""Ladder step 5: one portfolio-advisor finding becomes one queue entry, unattended.

ORIGINATION-DESIGN.md §5 -- the two boxes origination adds to the pipeline:

    portfolio-report.md -> [G15 passing] -> proposer -> permit("originate-task", repo)
      -> queue entry, status: pending -> queue-dispatch (unchanged from here down)

Everything that makes this safe lives somewhere else on purpose, so nothing in
this file can bypass it:

  * `brain.allowed(purpose=ORIGINATION)`: `[brain] origination_enabled`, a G15
    pass recomputed from the replay and the labels, and the brain's daily caps,
    breaker, usage limit and burn brake;
  * `host.permit("originate-task", repo)`: one named repo, once a day,
    enforced;
  * `queue-dispatch`: one in flight, a worktree, never pushed, the verifier,
    and a review note for the user.

What this file adds is the shape of the entry. It writes `status: pending`
(Option A, decided 2026-09-03), names the proposer in `Source:` so G14 can hold
it to having asked the ladder, writes `Base:` -- the branch the finding
describes -- so the worktree starts there, and records the finding's corpus key
so a later night does not propose the same work again while this one waits for
review.
The key alone is not enough -- the advisor rephrases nightly and the key
drifts with it -- so recent proposer tasks also go to the model as "already
originated".
"""

from __future__ import annotations

import time
from pathlib import Path

import bifrost

from . import findings, proposer, store
from .host import host
from .queue import QUEUE_PATH, _field, _parse_entries
from .store import log

REPORT = store.REPORT
PROJECTS = Path.home() / "code"  # macOS port: ~/code/eventregistry + ~/code/personal
SOURCE = "proposer"
# portfolio-advisor writes at 04:15. A report older than this is last night's
# failure, not tonight's news, and proposing from it would repeat old work.
MAX_REPORT_AGE_S = 20 * 3600
# Proposer entries this recent are shown to the model as already originated.
RECENT_DAYS = 30


def _targets(cfg: dict) -> dict[str, str]:
    """Report section -> repo path, for each `originate-task` declaration.

    Taken from the declarations themselves, so the proposer can only ever be
    shown findings for a repo the ladder would permit. A glob target is skipped
    outright -- ORIGINATION-DESIGN.md §7 rules one out, now or later.
    """
    out: dict[str, str] = {}
    for decl in cfg.get("actions") or []:
        if decl.get("name") != "originate-task":
            continue
        target = str(decl.get("target") or "")
        if not target or any(ch in target for ch in "*?["):
            continue
        try:
            section = str(Path(target).relative_to(PROJECTS))
        except ValueError:
            continue
        out[section.lower()] = target
    return out


def _proposer_entries(text: str) -> list[dict]:
    return [
        e for e in _parse_entries(text)
        if (_field(e["body"], "Source") or "").lower().startswith(SOURCE)
    ]


def _recent(entry: dict, now: float) -> bool:
    added = _field(entry["body"], "Added") or ""
    try:
        return now - time.mktime(time.strptime(added[:16], "%Y-%m-%dT%H:%M")) <= RECENT_DAYS * 86400
    except ValueError:
        return True  # unreadable date: keep it in view rather than forget it


def _new_id(text: str) -> str:
    taken = {e["id"] for e in _parse_entries(text)}
    base = time.strftime("q-%Y%m%d-%H%M")
    task_id, n = base, 2
    while task_id in taken:
        task_id, n = f"{base}-{n}", n + 1
    return task_id


async def _checked_out_branch(repo: str) -> str | None:
    """The branch the advisor's finding describes, for the entry's `Base:` line.

    None for a detached HEAD or any failure, and queue-dispatch then falls back
    to its old rule. See `_prepare_worktree` for why the line exists.
    """
    code, out = await host.run_process(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"], timeout_s=30)
    name = out.strip()
    return name if code == 0 and name and name != "HEAD" and "\n" not in name else None


def _entry_lines(task_id: str, repo: str, cand: dict, parsed: dict, base: str | None) -> list[str]:
    return [
        f"### {task_id} — status: pending",
        f"Added: {time.strftime('%Y-%m-%dT%H:%M')}",
        f"Source: {SOURCE} (originate-task, prompt {proposer.PROMPT_SHA})",
        f"Repo: {repo}",
        *([f"Base: {base}"] if base else []),
        f"Risk: {parsed['risk']}",
        f"Finding: {cand['text']}",
        f"Key: {cand['key']}",
        f"Task: {parsed['task']}",
        "Acceptance:",
        *[f"- {c}" for c in parsed["acceptance_criteria"]],
        "Constraints:",
        "- Change only what the task needs: no unrelated edits, reformatting or new notes files.",
        "- Do not push, create PRs, switch branches, merge, rebase, or tag.",
    ]



def _scope_report(report: str, sections: dict[str, str]) -> str:
    """The report cut down to the `## <repo>` sections in `sections`, so a call
    under one Claude config never sees the other scope's repos."""
    keep = {s.lower() for s in sections}
    out: list[str] = []
    include = True
    for line in report.splitlines():
        m = findings.SECTION_RE.match(line)
        if m:
            include = m.group(1).strip().lower() in keep
        if include:
            out.append(line)
    return "\n".join(out)


async def _originate_scope(cfg: dict, report: str, targets: dict[str, str], scope: str) -> str:
    """One proposer call over the findings of one scope, under that scope's Claude config."""
    try:
        queue_text = QUEUE_PATH.read_text(encoding="utf-8") if QUEUE_PATH.exists() else ""
    except OSError as exc:
        raise RuntimeError(f"unreadable queue: {exc}")
    earlier = _proposer_entries(queue_text)
    seen_keys = {(_field(e["body"], "Key") or "") for e in earlier}
    now = time.time()
    already = [
        f"{Path(_field(e['body'], 'Repo') or '?').name}: {_field(e['body'], 'Task') or '?'} ({e['id']}, {e['status']})"
        for e in earlier
        if _recent(e, now) and host.scope_of(_field(e['body'], 'Repo')) == scope
    ]

    scoped = {sec: path for sec, path in targets.items() if host.scope_of(path) == scope}
    cands = [c for c in proposer.candidates(report, repos=scoped) if c["key"] not in seen_keys]
    for i, cand in enumerate(cands, 1):
        cand["id"] = f"F{i}"
    if not cands:
        return f"[{scope}] nothing to judge - no new findings for {', '.join(sorted(scoped)) or 'any repo'}"

    # G15 measured the proposer on proposer.MODEL; Bifrost's surface must still route it there.
    routed = bifrost.model_for("originate")
    if routed != proposer.MODEL:
        return f"[{scope}] proposer not called - Bifrost routes 'originate' to {routed}, G15 measured {proposer.MODEL}"
    proposer.WORKDIR.mkdir(parents=True, exist_ok=True)
    result = await host.brain_work(
        proposer.build_prompt(_scope_report(report, scoped), cands, already),
        cwd=str(proposer.WORKDIR),
        surface="originate",
        cfg=cfg,
        purpose=host.ORIGINATION,
        isolated=True,
        scope=scope,
    )
    if not result["ok"]:
        return f"[{scope}] proposer call did not complete - {result['reason'][:200]}"

    try:
        parsed = proposer.parse(result["text"], cands)
    except proposer.OutputError as exc:
        host.notify(
            "originate-broken-answer",
            f"the proposer's answer broke its contract and was discarded: {exc}",
            severity="warn",
            cooldown_s=86400,
        )
        return f"proposer answer discarded - {exc}"

    proposed = sum(1 for verdict, _ in parsed["verdicts"].values() if verdict == "propose")
    if not parsed["pick"]:
        return f"proposer picked nothing ({len(cands)} judged, {proposed} marked propose, ${result['cost_usd']:.2f})"

    cand = next(c for c in cands if c["id"] == parsed["pick"])
    repo = targets[cand["repo"].lower()]
    task_id = _new_id(queue_text)

    verdict = host.permit("originate-task", repo, cfg=cfg, evidence=f"{task_id}: {parsed['task'][:120]}")
    if not verdict.allowed:
        return f"{task_id} not queued - {verdict.reason}"

    block = "\n".join(_entry_lines(task_id, repo, cand, parsed, await _checked_out_branch(repo))) + "\n"
    try:
        # Re-read immediately before appending: the file is shared with humans
        # and other agents, and it is append-only by contract.
        current = QUEUE_PATH.read_text(encoding="utf-8") if QUEUE_PATH.exists() else ""
        with QUEUE_PATH.open("a", encoding="utf-8") as fh:
            fh.write(("" if current.endswith("\n\n") or not current else "\n" if current.endswith("\n") else "\n\n") + block)
    except OSError as exc:
        host.record_outcome(verdict.action_id, f"not queued: {exc}")
        raise RuntimeError(f"could not append {task_id} to the queue: {exc}")

    host.record_outcome(verdict.action_id, f"queued {task_id} (pending)")
    host.notify(
        f"originated:{task_id}",
        f"Jarvis originated {task_id} in {Path(repo).name}: {parsed['task'][:160]}",
        severity="info",
        cooldown_s=0,
    )
    log.info("originate-task: queued %s -> %s: %s", task_id, repo, parsed["task"])
    return f"originated {task_id} -> {repo} ({proposed} of {len(cands)} marked propose, ${result['cost_usd']:.2f})"


async def originate(payload: dict) -> str:
    """The `originate-task` job: at most one finding becomes one pending queue entry. Returns the job result."""
    cfg = host.load_config()

    # First, and before anything is read: a refusal here costs nothing. Each
    # account is gated on its own limit, caps and breaker.
    gates = {s: host.brain_allowed(cfg, host.ORIGINATION, s) for s in host.SCOPES}
    if not any(ok for ok, _ in gates.values()):
        return "not originating - " + "; ".join(f"{s}: {why}" for s, (_, why) in gates.items())

    try:
        report = REPORT.read_text(encoding="utf-8")
        age = time.time() - REPORT.stat().st_mtime
    except OSError as exc:
        return f"not originating - no report to read ({exc})"
    if age > MAX_REPORT_AGE_S:
        return f"not originating - portfolio-report.md is {age / 3600:.0f}h old"

    # A report the parser cannot read looks exactly like a quiet night unless
    # it is said out loud. 14 of the first 21 revisions were like that.
    if not findings.extract(report):
        host.notify(
            "originate-unreadable-report",
            "portfolio-report.md has no findings the parser can read - the advisor's "
            "layout drifted from the one run-portfolio-advisor.ps1 pins",
            severity="warn",
            cooldown_s=86400,
        )
        return "not originating - tonight's report has no readable findings"

    targets = _targets(cfg)
    if not targets:
        return "not originating - no originate-task declaration names a repo"


    results = []
    for scope in ("work", "personal"):
        ok, why = gates[scope]
        results.append(
            await _originate_scope(cfg, report, targets, scope) if ok else f"[{scope}] not originating - {why}"
        )
    return "; ".join(results)


