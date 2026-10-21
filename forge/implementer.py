"""The restricted Claude executor for queue entries -- the only executor.

Decided by the user 2026-09-11 ("Claude, restricted"), first as the fallback
while Cursor was out; since 2026-10-01, when Cursor delegation was removed,
queue-dispatch hands every entry to `claude -p` here, inside the tightest box
the CLI offers. Every choice below was measured on 2026-09-12 in a throwaway repository
rather than assumed:

- `--restricted` confines the file tools to the working directory. A read of
  ~/.jarvis/config.toml and a write to ../outside.txt were both refused, and so
  was an edit to .git/config; creating .gitignore inside the repository was
  allowed, which is the edit this pipeline's first task is most likely to need.
- **So the working directory is the containment, and it is checked in code.**
  `run()` refuses any directory that is not a worktree under the worktrees
  root. The same probe, run once by mistake from the shell's own directory,
  wrote .gitignore and notes.txt into the home folder. That is what the check
  is for.
- `--tools Read,Edit,Write,Glob,Grep` means no shell: no push, no network, no
  test run, no commit. `--safe-mode` loads none of the user's CLAUDE.md,
  skills, plugins or hooks; `--strict-mcp-config` starts no MCP server;
  `--permission-mode dontAsk` refuses whatever the allow rules do not name, and
  those rules scope Edit and Write to the worktree as well.
- The commit is made here, after the agent exits, with the task's first line
  as its message and no trailer. A worktree holds only committed files plus
  the agent's edits, so `git add -A` in it cannot sweep anyone's work in.

The session runs through Gateway (`gateway.run_agent`, surface `job:implementer`, which picks
the model, since 2026-10-17): spend is metered as an LLM job (`job_calls`, "implementer")
against the weekly ceiling, and each task is capped with `--max-budget-usd`. The
outcome is filed into ~/.jarvis/delegation/done/ or failures/, where the
watcher picks it up for the verifier, the review note and the queue write-back.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

import gateway
from budget import ledger
from gateway import agent as gw_agent

from . import store
from .host import host
from .store import log

JOB = "implementer"
TOOLS = "Read,Edit,Write,Glob,Grep"
DELEGATION = store.DELEGATION
DONE = DELEGATION / "done"
FAILED = DELEGATION / "failures"
LOGS = DELEGATION / "logs"


def settings(cfg: Mapping[str, Any] | None = None) -> dict:
    cfg = cfg if cfg is not None else host.load_config()
    section = dict(cfg.get("implementer") or {})
    # False in code as well as in config: a missing key must never switch
    # spending on. Same rule as `[brain] escalation_enabled`.
    section.setdefault("enabled", False)
    section.setdefault("max_usd_per_task", 2.00)
    section.setdefault("timeout_s", 1200)
    return section


def choose(entry_executor: str | None, cfg: Mapping[str, Any] | None = None) -> tuple[str, str]:
    """('claude' | 'hold', why) for one queue entry.

    Claude is the only executor since Cursor delegation was removed on
    2026-10-01. An entry still asking for `Executor: cursor` is held rather
    than silently run by something it did not ask for.
    """
    if (entry_executor or "").strip().lower() == "cursor":
        return "hold", "Executor: cursor, but Cursor delegation was removed 2026-10-01"
    if not settings(cfg)["enabled"]:
        return "hold", "[implementer] enabled is false"
    return "claude", "the Claude implementer"


def _rule_path(path: Path) -> str:
    """C:\\x\\y -> //c/x/y, the only form a permission rule accepts for an
    absolute Windows path (`//C:/...` is silently ignored -- measured 2026-09-11)."""
    posix = path.as_posix()
    if len(posix) > 1 and posix[1] == ":":
        return f"//{posix[0].lower()}{posix[2:]}"
    # macOS port: an absolute POSIX path needs the `//` prefix too - a rule
    # starting with a single `/` is relative to the settings file's project.
    return "/" + posix if posix.startswith("/") and not posix.startswith("//") else posix


FLAGS = ["--restricted", "--safe-mode"]  # --strict-mcp-config comes from gateway.agent.command()


def allow_rules(worktree: Path) -> list[str]:
    root = _rule_path(worktree)
    return ["Read", "Glob", "Grep", f"Edit({root}/**)", f"Write({root}/**)"]


def command(exe: str, model: str, budget_usd: float, worktree: Path, session: str) -> list[str]:
    """The argument list Gateway runs for one task (shown here for the selftest)."""
    return gw_agent.command(exe, model, session, TOOLS, allow_rules(worktree), budget_usd, flags=FLAGS)


def build_prompt(task_text: str, criteria: list[str], constraints: list[str]) -> str:
    listed = lambda items, empty: "\n".join(f"- {i}" for i in items) or empty  # noqa: E731
    return f"""You are implementing one small, already-approved coding task in this git repository. The directory is a git worktree on its own branch. Nothing here is pushed, and a person reviews the result before anything happens to it.

Task:
{task_text.strip()}

Acceptance criteria:
{listed(criteria, "- (none given: make the change the task names, and nothing else)")}

Constraints:
{listed(constraints, "- (none beyond the rules below)")}

How this run works:
- You can read, search and edit files in this directory only. You have no shell: you cannot run commands, tests or git.
- Do not try to commit. When you finish, every change left in this directory is committed for you with the task's first line as the message, so leave nothing behind that is not part of the task - no notes, scratch or summary files.
- If the repository has a CLAUDE.md, AGENTS.md or README, read it first and follow its conventions.
- Make the minimal correct change. Do not reformat, rename or tidy anything the task does not need.
- If the task cannot be done correctly and safely here, change nothing and explain why in your final message.

End with a two- or three-line summary of what you changed.
"""


async def _agent(task_id: str, worktree: Path, prompt: str, conf: Mapping[str, Any], session: str,
                 scope: str) -> dict:
    """The one place a model process is spawned: one `gateway.run_agent` attempt. The selftest replaces it."""
    return await asyncio.to_thread(
        gateway.run_agent, JOB, prompt, TOOLS, allow_rules(worktree), float(conf["max_usd_per_task"]),
        scope=scope, cwd=worktree, flags=FLAGS, session=session, max_attempts=1,
        timeout_s=int(conf["timeout_s"]), note=task_id,
    )


async def _git(worktree: Path, *args: str) -> tuple[int, str]:
    return await host.run_process(["git", "-C", str(worktree), *args], timeout_s=120)


def _outcome(ok: bool, message: str, **extra: Any) -> dict:
    return {"ok": ok, "message": message, "cost_usd": 0.0, "session": "", "log": "", "commit": "", **extra}


async def run(
    task_id: str,
    worktree: Path,
    worktrees_root: Path,
    task_text: str,
    criteria: list[str],
    constraints: list[str],
    cfg: Mapping[str, Any] | None = None,
) -> dict:
    """Implement one task in `worktree` and commit it. Never raises for a task
    failure; the outcome dict says what happened."""
    conf = settings(cfg)
    worktree = Path(worktree).resolve()
    root = Path(worktrees_root).resolve()
    try:
        inside = worktree != root and worktree.is_relative_to(root)
    except ValueError:  # another drive
        inside = False
    if not inside or not (worktree / ".git").is_file():
        return _outcome(False, f"refused: {worktree} is not a git worktree under {root}")

    task_scope = host.scope_of(worktree)
    budget = ledger.job_budget(scope=task_scope)
    if budget["over"]:
        return _outcome(
            False,
            f"LLM job budget reached ({task_scope} account: ${budget['spent']:.2f} of ${budget['ceiling']:.2f} this week)",
        )
    session = str(uuid.uuid4())
    # The worktree resolves to its repo, so a work repo's task runs under claude-work.
    res = await _agent(task_id, worktree, build_prompt(task_text, criteria, constraints), conf, session, task_scope)
    output = res["output"]
    if res["subtype"] == "not-started":
        return _outcome(False, str(res.get("reason") or "not started"))

    log_path = LOGS / f"{task_id}.claude.json"
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
    except OSError as exc:
        log.warning("implementer: could not keep the output of %s: %s", task_id, exc)

    cost, subtype, denials = float(res["cost_usd"] or 0.0), str(res["subtype"]), list(res["denials"])
    summary = " ".join(str(res["result"] or "").split())[:400]
    # Gateway recorded the job call and any usage-limit window in Budget; Jarvis raises the alert.
    hit = res["limit"]
    if hit is not None:
        host.alert_limit(hit.provider)
    ok_call = bool(res["ok"])
    base = {"cost_usd": round(cost, 4), "session": res["session"] or session, "log": str(log_path)}

    if not ok_call:
        why = f"usage limit: {hit.evidence}" if hit is not None else subtype
        return _outcome(False, f"claude did not finish ({why}): {summary[:200]}", **base)

    code, status = await _git(worktree, "status", "--porcelain")
    if code != 0:
        return _outcome(False, f"git status failed in the worktree: {status.strip()[:200]}", **base)
    if not status.strip():
        return _outcome(False, f"made no changes: {summary or 'no explanation given'}", **base)

    message = next((line.strip() for line in task_text.splitlines() if line.strip()), task_id)[:200]
    code, out = await _git(worktree, "add", "-A")
    if code == 0:
        code, out = await _git(worktree, "commit", "-q", "-m", message)
    if code != 0:
        return _outcome(False, f"commit failed: {out.strip()[:300]}", **base)
    _, sha = await _git(worktree, "rev-parse", "--short", "HEAD")
    note = f" ({len(denials)} tool call(s) refused)" if denials else ""
    return _outcome(True, f"committed {sha.strip()}{note}: {summary[:200]}", commit=sha.strip(), **base)


def file_record(
    task_id: str,
    worktree: Path,
    task_text: str,
    criteria: list[str],
    constraints: list[str],
    outcome: dict,
    started_at: str,
) -> Path:
    """Write the result where the watcher would, so `watchers.py` enqueues the
    verifier. Written to a temporary name first: the watch pattern is *.json,
    and a half-written record would be read as a broken task."""
    ok = bool(outcome["ok"])
    folder, suffix = (DONE, "done") if ok else (FAILED, "failed")
    record = {
        "task_id": task_id,
        "repo": str(worktree),
        "task": task_text[:300],
        "acceptance_criteria": criteria,
        "constraints": constraints,
        "status": suffix,
        "message": outcome["message"],
        "executor": "claude",
        "session_id": outcome.get("session", ""),
        "cost_usd": outcome.get("cost_usd", 0.0),
        "commit": outcome.get("commit", ""),
        "stdout_log": outcome.get("log", ""),
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"{task_id}.{suffix}.json"
    staging = folder / f"{task_id}.{suffix}.json.partial"
    staging.write_text(json.dumps(record, indent=2), encoding="utf-8")
    staging.replace(final)
    return final
