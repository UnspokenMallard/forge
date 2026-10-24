"""Verify work the Claude implementer reported as done.

The trap this exists for: a task landing in `~/.jarvis/delegation/done` means
the agent exited 0, **not** that the acceptance criteria were met. (Written for
Cursor Agent; since Cursor delegation was removed on 2026-10-01 the records all
come from `implementer.py`.) That
distinction has already cost time here, and an exit code has never been
evidence in this environment.

What this can check today is mechanical, and it says so rather than implying
more:

  * did the repo actually gain a commit after the task was dispatched?
  * is the working tree dirty, i.e. changes made but never committed?
  * did the task record acceptance criteria at all?
  * does the repo's own declared gate still pass? (2026-08-22)

The gate is the one signal here that is evidence rather than a proxy, and it
only exists where a human declared one -- see `_resolve_gate`. Where none is
declared the verdict says the change is *unverified*, which is not the same
claim as unsafe, and is meant to read as a gap to close.

Judging whether a diff *satisfies* the wording of its acceptance criteria still
needs the brain and remains out of scope. This handler produces a verdict a
human can act on in seconds, and never claims a change is correct on the
strength of an exit code alone.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import bifrost

from .host import host
from .store import log


async def _git(repo: str, *args: str) -> tuple[int, str]:
    return await host.run_process(["git", "-C", repo, *args], timeout_s=60)


# Third gate source (2026-09-11): the agent-loop's per-repo contract, written by the owner,
# kept outside the repositories (owner decision 2026-09-10: nothing agent-related goes into
# a repo, so `.jarvis-gate.json` can no longer be the standing case). This reads a config
# file the human wrote; it does not call agent-loop's scripts (charter D1: separate
# executors). Overridable for the selftest.
AGENT_LOOP_REPOS = Path(os.environ.get("JARVIS_AGENT_LOOP_REPOS") or (Path.home() / ".claude-work" / "agent-loop" / "repos"))


def _agent_loop_gate(repo: str) -> dict | None:
    """The hard gate stages of the agent-loop repo.json whose `path` is `repo`, as one
    `cmd /c a && b && c` command, or None. Soft (report-only) stages are skipped: the
    verifier asks "did the change break the gate", and a soft stage is red by definition."""
    want = os.path.normcase(os.path.normpath(os.path.abspath(repo)))
    try:
        candidates = sorted(AGENT_LOOP_REPOS.glob("*/repo.json"))
    except OSError:
        return None
    for cfg_path in candidates:
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("verify-delegated-work: unreadable %s: %s", cfg_path, exc)
            continue
        path = cfg.get("path")
        if not path or os.path.normcase(os.path.normpath(os.path.abspath(path))) != want:
            continue
        stages = cfg.get("gate") or {}
        cmds = [s.get("cmd") for s in stages.values() if isinstance(s, dict) and s.get("cmd") and not s.get("soft")]
        if not cmds:
            return None
        return {"command": ["cmd", "/c", " && ".join(cmds)], "timeout_s": int(cfg.get("gate_timeout_s", 1800)),
                "source": f"agent-loop {cfg_path.parent.name}/repo.json"}
    return None


def _resolve_gate(repo: str, task: dict) -> tuple[list[str], int] | None:
    """Find the repo's real build/test gate, or None if none is declared.

    Deliberately never *infers* a command. `npm test` on a repo this was only
    ever pointed at by a path string would run whatever lifecycle scripts that
    repo happens to define, from a background daemon, unattended. A gate has to
    be written down by a human -- in the task JSON for a one-off, in a
    repo-local `.jarvis-gate.json`, or (since 2026-09-11) in the agent-loop's
    `repos/<name>/repo.json` `gate` block for the standing case:

        {"command": ["npm", "run", "check"], "timeout_s": 600}
    """
    declared = task.get("gate")
    source = "task JSON"
    if not declared:
        gate_file = Path(repo) / ".jarvis-gate.json"
        if gate_file.is_file():
            try:
                declared = json.loads(gate_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("verify-delegated-work: unreadable .jarvis-gate.json in %s: %s", repo, exc)
                return None
            source = ".jarvis-gate.json"
        else:
            declared = _agent_loop_gate(repo)
            if not declared:
                return None
            source = declared["source"]

    command = declared.get("command") if isinstance(declared, dict) else declared
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command) or not command:
        log.warning("verify-delegated-work: gate in %s (%s) is not a list of strings", repo, source)
        return None

    timeout_s = declared.get("timeout_s", 600) if isinstance(declared, dict) else 600
    return command, int(timeout_s)


async def _run_gate(repo: str, task: dict) -> str:
    """Run the declared gate and report pass/fail. Never raises on gate failure.

    A failing gate is a finding about the delegated work, not a failure of this
    handler -- raising here would mark the *verification* failed and bury the
    thing it just caught (same shape as the docker-runaway-check job reporting
    a real detection as a handler error).
    """
    resolved = _resolve_gate(repo, task)
    if resolved is None:
        return "NO GATE declared - change is unverified, not verified"

    command, timeout_s = resolved
    code, out = await host.run_process(command, timeout_s=timeout_s, cwd=repo)

    if code == 0:
        return f"gate passed ({' '.join(command)})"
    if code == 124:
        return f"GATE TIMED OUT after {timeout_s}s ({' '.join(command)})"

    tail = " | ".join(line.strip() for line in out.splitlines() if line.strip())[-300:]
    return f"GATE FAILED exit={code} ({' '.join(command)}): {tail}"


# Windows caps a whole command line at ~32k characters, and `claude -p` takes
# the prompt as argv. The diff therefore has to be capped rather than streamed;
# 8k is well clear of the limit once criteria and instructions are added, and a
# criteria check that needs more diff than this is one a human should be
# reading anyway.
_DIFF_CHAR_CAP = 8000

# Git's canonical empty-tree object. Diffing against it yields the entire
# history, which is what a root commit's "parent" should mean here.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_SCORE_INSTRUCTIONS = """\
You are checking whether a code diff satisfies each acceptance criterion.

Rules:
- Judge ONLY from the diff shown. Do not assume unshown code does the right thing.
- If the diff is truncated or a criterion concerns code not visible, answer "unclear".
- "met" requires positive evidence in the diff, not the mere absence of contradiction.

Reply with ONLY a JSON array, no prose and no code fence, one object per
criterion in the order given:
[{"criterion": "<verbatim>", "verdict": "met"|"not-met"|"unclear", "why": "<one short sentence>"}]
"""


async def _score_criteria(repo: str, task: dict, base: str | None) -> str | None:
    """Ask a cheap model whether the diff actually satisfies each criterion.

    Runs headless `claude -p --model haiku`, which uses the logged-in
    subscription -- no API key, no separate billing surface, and it lands on
    the same Haiku tier the routing ladder already prefers for mechanical work.

    Fails OPEN, always. Every failure path returns a "scoring unavailable" note
    rather than raising: a verifier that goes red because a model call timed
    out trains you to ignore it, and the git/gate findings above are still
    valid without it. This never decides anything on its own -- a "not-met"
    only adds NEEDS REVIEW, and a clean score never clears a failing gate.
    """
    criteria = task.get("acceptance_criteria") or []
    if not criteria or not base:
        return None

    code, diff = await _git(repo, "diff", f"{base}..HEAD")
    if code != 0 or not diff.strip():
        return None

    truncated = len(diff) > _DIFF_CHAR_CAP
    if truncated:
        diff = diff[:_DIFF_CHAR_CAP] + "\n...[diff truncated]"

    numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(criteria, 1))
    prompt = (
        f"{_SCORE_INSTRUCTIONS}\nTASK: {task.get('task', '(none)')}\n\n"
        f"ACCEPTANCE CRITERIA:\n{numbered}\n\nDIFF:\n{diff}\n"
    )

    # Through Bifrost (surface `criteria-score`, which picks the model and writes the provider-ledger
    # row under that name). Since 2026-10-17 the call is isolated with JSON output, so its cost is
    # known; it reads the diff it was handed and nothing else.
    res = await bifrost.aask("criteria-score", prompt, scope=host.scope_of(repo), timeout_s=180)
    if res is None or not res["ok"]:
        why = "surface is off" if res is None else str(res["reason"])[:80]
        return f"criteria scoring unavailable ({why})"
    out = res["text"]

    body = out.strip()
    start, end = body.find("["), body.rfind("]")
    if start == -1 or end <= start:
        return "criteria scoring unavailable (no JSON in model reply)"
    try:
        scored = json.loads(body[start : end + 1])
    except ValueError:
        return "criteria scoring unavailable (unparseable model reply)"
    if not isinstance(scored, list) or not scored:
        return "criteria scoring unavailable (empty result)"

    unmet, unclear = [], 0
    for item in scored:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict", "")).lower()
        if verdict == "not-met":
            unmet.append(f"{str(item.get('criterion', '?'))[:60]} ({str(item.get('why', ''))[:80]})")
        elif verdict != "met":
            unclear += 1

    note = "; ".join(
        part
        for part in (
            f"CRITERIA NOT MET: {' | '.join(unmet)}" if unmet else "",
            f"{unclear} criterion(s) unclear" if unclear else "",
            "criteria met" if not unmet and not unclear else "",
            "judged on a truncated diff" if truncated else "",
        )
        if part
    )
    return note


async def verify(payload: dict) -> str:
    """The `verify-delegated-work` job: judge one finished task file. Returns the job result."""
    path = Path(payload["path"])
    if not path.exists():
        return f"task file {path.name} no longer exists (moved or cleaned up)"

    try:
        task = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"unreadable task file {path.name}: {exc}")

    repo = task.get("repo")
    summary = task.get("task", "(no summary)")
    criteria = task.get("acceptance_criteria") or []
    landed_failed = "failed" in payload.get("source", "")

    findings: list[str] = []

    # Usage limits are not scanned for here: implementer.run() records its own
    # Claude limit hits before it files the record.

    if landed_failed:
        findings.append("task landed in failed/")

    if not criteria:
        findings.append("no acceptance_criteria recorded")

    # Initialised before the branch below so the review-note check at the end of
    # this function is safe when `repo` was falsy and the else never ran.
    commits: list[str] = []

    if not repo:
        findings.append("no repo field")
    # `.exists()`, NOT `.is_dir()`: in a git worktree `.git` is a FILE holding a
    # `gitdir:` pointer, so an is_dir() test reports every autonomous worktree
    # dispatch as "not a git repo" and fails it before a single check runs
    # (ORIGINATION-DESIGN.md §3a).
    elif not (Path(repo) / ".git").exists():
        findings.append(f"repo {repo} is not a git repo")
    else:
        # Commits since the task file landed. mtime is the best dispatch
        # timestamp available without the watcher having recorded one.
        since = path.stat().st_mtime
        code, out = await _git(
            repo, "log", "--since", str(int(since) - 3600), "--pretty=format:%h %s"
        )
        commits = [line for line in out.splitlines() if line.strip()] if code == 0 else []

        code_status, status_out = await _git(repo, "status", "--porcelain")
        dirty = [line for line in status_out.splitlines() if line.strip()] if code_status == 0 else []

        if not commits and not dirty:
            findings.append("repo has NO new commits and a clean tree - nothing was done")
        elif not commits and dirty:
            findings.append(f"{len(dirty)} uncommitted change(s), no commit - work left unstaged")
        else:
            findings.append(f"{len(commits)} commit(s)")
            if dirty:
                findings.append(f"plus {len(dirty)} uncommitted file(s)")

            findings.append(await _run_gate(repo, task))

            # Oldest commit in the dispatch window; its parent is the diff
            # base. When that commit is the repo's root it has no parent, so
            # the base becomes git's empty-tree object and the diff is the
            # whole history -- correct for a repo the task created from
            # scratch. Skipping instead would silently drop scoring on exactly
            # those tasks, and a skipped score reads as a pass.
            oldest = commits[-1].split()[0]
            code_base, _ = await _git(repo, "rev-parse", "--verify", f"{oldest}^")
            base = f"{oldest}^" if code_base == 0 else EMPTY_TREE
            score_note = await _score_criteria(repo, task, base)
            if criteria:
                findings.append(score_note or "criteria scoring did not run")

    verdict = "NEEDS REVIEW" if any(
        marker in f
        for f in findings
        for marker in (
            "NO new commits",
            "unstaged",
            "failed/",
            "not a git repo",
            "no repo",
            "GATE FAILED",
            "GATE TIMED OUT",
            "CRITERIA NOT MET",
        )
    ) else "looks plausible"

    # A passing gate is the only thing here that is evidence rather than a
    # proxy, so it is the only case allowed to sound more confident.
    # "gate green" is the only reassuring verdict this emits, so anything the
    # verifier could not actually resolve has to block it. A skipped check must
    # never read the same as a passed one.
    unresolved = any(
        (
            "unclear" in f
            or "unavailable" in f
            or "did not run" in f
            or "truncated" in f
            # A task with no criteria recorded was never checked against intent,
            # only against its gate. That is not the same as verified, and read
            # as "gate green" once already for real delegated work.
            or "no acceptance_criteria" in f
        )
        for f in findings
    )
    if verdict == "looks plausible" and not unresolved and any(
        f.startswith("gate passed") for f in findings
    ):
        verdict = "gate green"

    result = f"{verdict}: {path.name} ({summary[:60]}) - {'; '.join(findings)}"

    # Only shout when something looks wrong. A verifier that alerts on success
    # is a verifier that gets ignored.
    if verdict == "NEEDS REVIEW":
        host.notify(
            f"delegated-work:{path.name}",
            result,
            severity="warn",
            cooldown_s=3600,
        )

    # Close the loop for queue-dispatched work. NEEDS REVIEW becomes `blocked`
    # rather than `done` -- it releases the one-in-flight gate so the queue
    # keeps moving, while still reading as something a human must look at.
    # Marking it done would bury exactly the tasks worth reading.
    task_id = str(task.get("task_id") or "")
    if task_id.startswith("q-"):
        from .queue import update_queue_entry

        update_queue_entry(
            task_id,
            "blocked" if verdict == "NEEDS REVIEW" else "done",
            f"{verdict} - {'; '.join(findings)}"[:300],
        )

    # ORIGINATION-DESIGN.md §3b. Autonomous work runs in a worktree and is never
    # pushed, so the review channel is a note rather than a pull request. Every
    # branch that produced commits gets a line carrying its verdict -- NEEDS
    # REVIEW included, because a branch left unlisted is a branch silently
    # orphaned on disk with nothing pointing at it.
    if repo and commits:
        from .queue import WORKTREES, append_review_note

        repo_path = Path(repo)
        try:
            in_worktree = repo_path.is_relative_to(WORKTREES)
        except ValueError:  # different drive on Windows
            in_worktree = False
        if in_worktree:
            code_branch, branch_out = await _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
            code_origin, origin_out = await _git(
                repo_path, "rev-parse", "--path-format=absolute", "--git-common-dir"
            )
            # The branch tip, captured now because it is the only thing that can
            # answer "was this merged or thrown away" after the branch itself is
            # deleted -- see review.fate().
            code_tip, tip_out = await _git(repo_path, "rev-parse", "HEAD")
            # The main repo is the parent of the shared .git dir the worktree
            # points at; without it the review command would name the worktree,
            # which is not where the user reviews from.
            source = (
                Path(origin_out.strip()).parent if code_origin == 0 and origin_out.strip() else repo_path
            )
            append_review_note(
                task_id=task_id or path.name,
                repo=source,
                branch=branch_out.strip() if code_branch == 0 else "?",
                worktree=repo_path,
                summary=f"{verdict} - {len(commits)} commit(s) - {summary[:80]}",
                tip_sha=tip_out.strip() if code_tip == 0 else "",
            )

    log.info("verify-delegated-work: %s", result)
    return result
