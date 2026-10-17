"""Dispatch queued prompts to the Claude implementer, one at a time.

`~/.jarvis/queue` was already the documented intake format, but pickup was
manual -- an agent had to be told to look. This turns it into the actual
entry point: a pending entry is run by the restricted Claude executor
(`implementer.py`) without anyone asking, and the verifier writes the outcome
back to the same entry when the work lands. (Until 2026-10-01 entries went to
Cursor first; Cursor delegation was removed that day.)

Deliberate limits, because this is the piece that acts unattended:

  * **One in flight.** A second entry never dispatches while one is
    in-progress. A queue that fans out in parallel turns a bad instruction
    into several bad commits before anyone reads the first.
  * **No guessed repo.** An entry without an explicit `Repo:` is flipped to
    `blocked`, never dispatched somewhere plausible.
  * **Honours the Claude limit flag.** When Claude has reported a usage
    limit, dispatching just manufactures failures, so entries stay pending.
  * **`Risk: high` needs a human.** Flipped to `blocked` with a Blocker line
    rather than run.
  * **Worktree and budget.** The implementer only ever runs inside a
    worktree, and only within the weekly LLM-job budget; with `[implementer]
    enabled = false` every entry is held.

Queueing an entry IS the authorization to run it -- the user wrote it down on
purpose. What stays with the user is which entries exist and what happens to
the results, not clicking go on each one.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from budget import brake, ledger, limits

from . import implementer, review, store
from .host import host
from .store import log

QUEUE_PATH = store.QUEUE_PATH
# Isolated checkouts for autonomous work. Deliberately OUTSIDE any repo, so a
# worktree never shows up in its own repo's `git status` and is never swept by
# config-autocommit. See ORIGINATION-DESIGN.md §3a.
WORKTREES = store.WORKTREES
REVIEW_QUEUE = review.REVIEW_QUEUE

# The documented header is `### q-YYYYMMDD-HHMM — status: pending`. The dash is
# an em dash in the spec but a plain hyphen is what gets typed half the time,
# so both are accepted rather than silently skipping an entry.
ENTRY_RE = re.compile(r"^###[ \t]+(q-[\w-]+)[ \t]*[—-][ \t]*status:[ \t]*(\S+)[ \t]*$", re.M)


def _strip_fences(text: str) -> str:
    """Blank out fenced code blocks, preserving offsets.

    The queue file documents its own entry format inside a ``` fence, and that
    example is a syntactically perfect entry -- the dispatcher read the
    placeholder as real work on its first live run and rewrote the docs. Lines
    are replaced with empty strings rather than removed so every match offset
    still lines up with the original text, which is what the rewrite path uses.
    """
    out, fenced = [], False
    for line in text.splitlines(keepends=True):
        toggling = line.lstrip().startswith("```")
        if fenced or toggling:
            # Same length, no content: spaces where the text was, newline kept.
            body = line[:-1] if line.endswith("\n") else line
            out.append(" " * len(body) + ("\n" if line.endswith("\n") else ""))
        else:
            out.append(line)
        if toggling:
            fenced = not fenced
    return "".join(out)


def _parse_entries(text: str) -> list[dict]:
    """Split the queue into entries. Body runs to the next header or EOF."""
    matches = list(ENTRY_RE.finditer(_strip_fences(text)))
    entries = []
    for i, match in enumerate(matches):
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entries.append(
            {
                "id": match.group(1),
                "status": match.group(2).lower(),
                "header": match.group(0),
                "body": text[match.end() : body_end],
            }
        )
    return entries


def _field(body: str, name: str) -> str | None:
    """Read a single-line `Name: value` field, case-insensitively."""
    match = re.search(rf"^[ \t]*{name}:[ \t]*(.+)$", body, re.M | re.I)
    return match.group(1).strip() if match else None


def _block(body: str, name: str) -> list[str]:
    """Read a `Name:` label followed by indented or bulleted lines.

    Stops at the next `Word:` label so a Task block cannot swallow the
    Acceptance block that follows it.
    """
    match = re.search(rf"^[ \t]*{name}:[ \t]*$", body, re.M | re.I)
    if not match:
        return []
    lines = []
    for line in body[match.end() :].splitlines():
        if re.match(r"^[ \t]*\w[\w ]*:", line):
            break
        stripped = line.strip().lstrip("-*").strip()
        if stripped:
            lines.append(stripped)
    return lines


def _task_text(body: str) -> str:
    """The Task: block, whether written inline or as following lines."""
    inline = _field(body, "Task")
    if inline:
        return inline
    return "\n".join(_block(body, "Task"))


def _set_status(text: str, entry: dict, status: str, extra_lines: list[str]) -> str:
    """Rewrite one entry's header status and append lines under it.

    Only ever touches the single entry handed in -- the queue is append-only by
    contract, and rewriting neighbours would lose history written by whatever
    else is appending to this file.
    """
    new_header = re.sub(
        r"status:[ \t]*\S+", f"status: {status}", entry["header"], count=1
    )
    updated = text.replace(entry["header"], new_header, 1)

    if extra_lines:
        body = entry["body"]
        addition = "\n".join(extra_lines)
        # Insert directly after the entry body's content, before the blank run
        # that separates entries, so the lines stay visually attached.
        new_body = body.rstrip("\n") + "\n" + addition + "\n"
        updated = updated.replace(body, new_body, 1)
    return updated


def update_queue_entry(task_id: str, status: str, result: str) -> bool:
    """Write an outcome back to a queue entry. Called by the verifier.

    Returns False when the id is not a queue task or is no longer in-progress,
    so the caller can stay quiet rather than report a phantom update.
    """
    if not task_id.startswith("q-") or not QUEUE_PATH.exists():
        return False
    try:
        text = QUEUE_PATH.read_text(encoding="utf-8")
    except OSError:
        return False

    for entry in _parse_entries(text):
        if entry["id"] != task_id or entry["status"] != "in-progress":
            continue
        stamp = time.strftime("%Y-%m-%dT%H:%M")
        lines = (
            [f"Blocker: {result}", f"Completed: {stamp}"]
            if status == "blocked"
            else [f"Result: {result}", f"Completed: {stamp}"]
        )
        try:
            QUEUE_PATH.write_text(_set_status(text, entry, status, lines), encoding="utf-8")
        except OSError as exc:
            log.warning("queue: could not write outcome for %s: %s", task_id, exc)
            return False
        log.info("queue: %s -> %s", task_id, status)
        return True
    return False


async def _git(repo: Path, *args: str) -> tuple[int, str]:
    return await host.run_process(["git", "-C", str(repo), *args], timeout_s=60)


async def _base_branch(repo: Path) -> str:
    """The branch an autonomous task branches off. Read, never guessed.

    `origin/HEAD` when the remote publishes one, else whatever is checked out.
    The fallback matters: `general\\caddy` has no remote at all, and a default
    of "main" would fail there while looking like it worked elsewhere.
    """
    code, out = await _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if code == 0 and out.strip():
        return out.strip().rsplit("/", 1)[-1]
    code, out = await _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if code == 0 and out.strip() else ""


async def _prepare_worktree(repo: Path, task_id: str, base: str | None = None) -> tuple[Path, str, str]:
    """An isolated checkout for one autonomous task.

    Returns (worktree_path, branch, base). Raises RuntimeError on any failure --
    the caller must HOLD the entry rather than dispatch, because the fallback
    would be running the agent in the user's own checkout, which is the exact
    hazard this exists to remove (ORIGINATION-DESIGN.md §3a).

    `base` comes from an entry's `Base:` line and must be a LOCAL branch. The
    originator writes one (2026-09-12) because er-api and er-web keep
    `origin/HEAD` on `staging` while the work happens on `dev` and `dev/1` --
    and with a second remote, a bare `staging` is not even unambiguous. A
    finding describes the branch that was checked out, so that is what the
    task starts from. Without the line, the old rule applies unchanged.
    """
    if not (repo / ".git").exists():
        raise RuntimeError(
            f"{repo} is not a git repo, so it cannot have a worktree - "
            "add 'Worktree: no' to the entry to run in place instead"
        )

    if base:
        code, _ = await _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}")
        if code != 0:
            raise RuntimeError(f"Base: {base} is not a local branch in {repo}")
    else:
        base = await _base_branch(repo)
    if not base:
        raise RuntimeError(f"could not determine a base branch in {repo}")

    branch = f"jarvis/{task_id}"
    worktree = WORKTREES / f"{repo.name}-{task_id}"
    if worktree.exists():
        raise RuntimeError(f"worktree {worktree} already exists - not reusing")

    WORKTREES.mkdir(parents=True, exist_ok=True)
    code, out = await _git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    if code != 0:
        raise RuntimeError(f"git worktree add failed (exit {code}): {out.strip()[:200]}")
    return worktree, branch, base


def append_review_note(
    task_id: str,
    repo: Path,
    branch: str,
    worktree: Path,
    summary: str,
    tip_sha: str = "",
) -> None:
    """One line per finished branch, in the channel the user already reads.

    Records a `review_items` row and re-renders `REVIEW-QUEUE.md` from it. The
    file used to be appended to directly, which meant nothing could ever take a
    line back out -- see `review.py`'s module docstring for why a queue the
    human is expected to act on needs items to be able to leave it.

    `tip_sha` is what makes "deleted unmerged" decidable later. Captured now
    because after the branch is gone there is nothing left to read it from.

    Best-effort: a failure here must never fail the dispatch, because the work
    is already done by the time this is written. It is logged instead.
    """
    try:
        review.add(
            source="branch",
            summary=summary,
            task_id=task_id,
            repo=str(repo),
            branch=branch,
            worktree=str(worktree),
            tip_sha=tip_sha,
        )
        review.render()
    except Exception as exc:  # noqa: BLE001 - the work is already done
        log.warning("queue: could not write review note for %s: %s", task_id, exc)


def entry_blocker(body: str) -> str | None:
    """Why an entry must not run, or None when it may. Pure apart from the
    Repo directory check. Checked before any agent is started."""
    repo = _field(body, "Repo")
    if not repo:
        return "no Repo: line - refusing to guess which repository this means"
    if not Path(repo).is_dir():
        return f"Repo: {repo} does not exist"
    if not _task_text(body):
        return "no Task: text found in the entry"
    # The verifier scores the commit against these. Without them it has nothing to judge against.
    if not any(c.strip() for c in _block(body, "Acceptance")):
        return "no Acceptance: criteria - the work cannot be checked without them"
    if (_field(body, "Risk") or "").lower() == "high":
        return "Risk: high - needs your go-ahead before an agent runs it"
    return None


async def _run_implementer(
    text: str, entry: dict, worktree: Path, repo: str, task_text: str, verdict: Any, route_why: str
) -> str:
    """Hand one entry to the restricted Claude executor, inline, and file its
    outcome where the watcher picks it up. From there the verifier takes over:
    it scores the commit and flips the entry done or blocked."""
    criteria = _block(entry["body"], "Acceptance")
    constraints = _block(entry["body"], "Constraints")
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        QUEUE_PATH.write_text(
            _set_status(text, entry, "in-progress",
                        [f"Dispatched: {time.strftime('%Y-%m-%dT%H:%M')} to the Claude implementer ({route_why[:100]})"]),
            encoding="utf-8",
        )
    except OSError as exc:
        host.record_outcome(verdict.action_id, f"withdrawn: {exc}")
        raise RuntimeError(f"could not mark {entry['id']} in-progress: {exc}")

    outcome = await implementer.run(entry["id"], worktree, WORKTREES, task_text, criteria, constraints)
    try:
        implementer.file_record(entry["id"], worktree, task_text, criteria, constraints, outcome, started)
    except OSError as exc:
        # No record means no verifier, and the entry would sit in flight for good.
        update_queue_entry(entry["id"], "blocked", f"the Claude implementer finished but its record could not be filed: {exc}")
        host.record_outcome(verdict.action_id, f"implemented, record not filed: {exc}")
        raise RuntimeError(f"could not file the implementer record for {entry['id']}: {exc}")

    host.record_outcome(
        verdict.action_id, f"implemented {entry['id']} with claude -> {repo}: {outcome['message'][:100]}"
    )
    log.info("queue: %s implemented by claude in %s: %s", entry["id"], worktree, outcome["message"])
    return f"{entry['id']} implemented by claude ({'ok' if outcome['ok'] else 'FAILED'}): {outcome['message'][:160]}"


async def dispatch(payload: dict) -> str:
    """The `queue-dispatch` job: at most one pending entry to the implementer. Returns the job result."""
    if not QUEUE_PATH.exists():
        return "no queue file"
    try:
        text = QUEUE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"unreadable queue: {exc}")

    entries = _parse_entries(text)
    in_flight = [e["id"] for e in entries if e["status"] == "in-progress"]
    if in_flight:
        return f"{in_flight[0]} still in flight - not dispatching another"

    pending = [e for e in entries if e["status"] == "pending"]
    if not pending:
        return "no pending entries"
    entry = pending[0]

    executor, route_why = implementer.choose(_field(entry["body"], "Executor"))
    if executor == "hold":
        return f"{entry['id']} held - {route_why}"

    # Phase 6 §2. Both the implementer and the verifier's scoring are `claude -p`,
    # so dispatching into a Claude limit only produces failures to clean up.
    # Left pending on purpose: the work is still wanted.
    # Each account has its own quota: a work repo's task waits on the work limit only.
    task_scope = host.scope_of(_field(entry["body"], "Repo"))
    provider = host.provider_of(task_scope)
    claude_limited, until = limits.blocked(provider)
    if claude_limited:
        when = time.strftime("%H:%M", time.localtime(until)) if until else "?"
        return f"{entry['id']} held - {provider} usage limit in effect until {when}"

    # Phase 6 step 4. Left pending, exactly like a usage limit: the work is
    # still wanted, the daemon has just been spending too fast to be trusted
    # with more of it right now.
    braked, brake_state = brake.engaged(scope=task_scope)
    if braked:
        return f"{entry['id']} held - burn brake engaged until {brake_state.get('clears_at_human')}"

    repo = _field(entry["body"], "Repo")
    task_text = _task_text(entry["body"])
    blocker = entry_blocker(entry["body"])

    if blocker:
        try:
            QUEUE_PATH.write_text(
                _set_status(text, entry, "blocked", [f"Blocker: {blocker}"]), encoding="utf-8"
            )
        except OSError as exc:
            raise RuntimeError(f"could not block {entry['id']}: {exc}")
        host.notify(
            f"queue-blocked:{entry['id']}",
            f"Queue entry {entry['id']} blocked: {blocker}",
            severity="warn",
            cooldown_s=3600,
        )
        return f"{entry['id']} blocked: {blocker}"

    wants_worktree = (_field(entry["body"], "Worktree") or "").strip().lower() not in ("no", "false", "0")
    # The worktree IS the implementer's containment (implementer.py), and its
    # `git add -A` would sweep a human's work in anywhere else.
    if not wants_worktree:
        return (
            f"{entry['id']} held - the Claude implementer only runs in a worktree; "
            "remove 'Worktree: no'"
        )
    budget = ledger.job_budget(scope=task_scope)
    if budget["over"]:
        return (
            f"{entry['id']} held - LLM job budget reached "
            f"({task_scope} account: ${budget['spent']:.2f} of ${budget['ceiling']:.2f} this week)"
        )

    # Phase 6 step 5: the autonomy ladder. Asked last, after this handler's own
    # guard rails have already refused the entries they refuse -- the ladder is
    # a second, declarative check on top of them, not a replacement for them.
    #
    # Shadow mode today: `permit()` records its verdict and returns allowed
    # regardless, so this changes nothing yet. When `enforce` is flipped, an
    # undeclared repo will hold the entry instead of dispatching it.
    verdict = host.permit(
        "dispatch-delegation",
        repo,
        evidence=f"{entry['id']}: {task_text[:120]}",
    )
    if not verdict.allowed:
        # Held, not blocked: the work is still wanted and the entry is still
        # valid. What is missing is a declaration, which is a config edit.
        return f"{entry['id']} held - {verdict.reason}"

    # ORIGINATION-DESIGN.md §3a. DEFAULT ON, decided by the user 2026-09-03.
    # Every dispatched task runs in an isolated checkout on its own branch, so
    # it can never sweep unrelated in-progress work into its commit and never
    # disturbs the user's own checkout. `Worktree: no` used to be the escape
    # hatch for Cursor; the Claude implementer refuses it (held above).
    try:
        worktree, branch, base = await _prepare_worktree(
            Path(repo), entry["id"], (_field(entry["body"], "Base") or "").strip() or None
        )
    except (RuntimeError, OSError) as exc:
        # HELD, not blocked and never dispatched. Falling back to the user's
        # own checkout is the single outcome this must not produce, so a
        # worktree failure stops the dispatch rather than degrading it.
        return f"{entry['id']} held - worktree preparation failed: {exc}"
    log.info(
        "queue: %s dispatching into worktree %s on %s (base %s)",
        entry["id"], worktree, branch, base,
    )
    return await _run_implementer(text, entry, worktree, repo, task_text, verdict, route_why)
