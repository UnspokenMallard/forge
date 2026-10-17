"""Ladder step 5: the proposer's judgment -- a report goes in, at most one task comes out.

ORIGINATION-DESIGN.md §4-§6. This module is the judgment alone: the prompt, the
candidate list, and a strict reader for the answer. It imports nothing that can
call a model, write a file or touch the queue, so the G15 replay
(`g15-replay.py`) and the nightly originator run exactly the same judgment
through different doors -- the replay metered as an LLM job, the originator
through `brain.work()` behind `brain.allowed(purpose=ORIGINATION)`.

**The prompt is frozen by hash.** `PROMPT_SHA` covers the template, the model
and the call profile. G15 records the hash it measured, and `g15.status()`
refuses a pass recorded against any other hash: an edited prompt is a
different proposer, and a pass earned by the old one says nothing about it.
Do not tune the prompt against the labels and re-run the replay -- that
measures the tuning, not the proposer. A changed prompt needs labels it has not
been fitted to, which means new report revisions.

**Why every candidate gets a verdict, not just the pick.** The originator acts
on one pick a night, but a finding marked "propose" and not picked tonight is
picked on a later night while it stays in the report. So the set of "propose"
verdicts is the set of things it would eventually originate, and that set is
what G15 scores -- about 140 verdicts across the history, where picks alone
would give 7.

**No tools.** The call runs with `--tools ""` and `--safe-mode` (`brain.work`
with `isolated=True`). Measured 2026-09-12: asked to use its Read tool under
those flags, the model made no tool call and instead wrote a fake tool block
and invented the file's first line. So the prompt says outright that it has no
tools, and `parse()` accepts one JSON object and nothing else.
"""

from __future__ import annotations

import hashlib
import sys
import json
from pathlib import Path
from typing import Iterable

from . import findings

MODEL = "sonnet"
CALL_PROFILE = "isolated"  # brain.command(isolated=True): no tools, no CLAUDE.md, no MCP

# Where every proposer call runs, replay and originator alike. Outside any git
# repository on purpose: Claude Code writes the working directory's git status
# into its system prompt, and the proposer must judge the report, not the
# state of ~/.jarvis's own tree. It stays empty -- the call has no tools.
WORKDIR = (Path.home() / "Library" / "Caches" / "jarvis-proposer") if sys.platform == "darwin" \
    else (Path.home() / "AppData" / "Local" / "jarvis-proposer")
VERDICTS = ("propose", "never")
RISKS = ("low", "medium")
MAX_CRITERIA = 5
MAX_TASK_CHARS = 300
MAX_WHY_CHARS = 300

PROMPT = """You are the task proposer for Jarvis, a daemon that works unattended on one developer's machine. Below is tonight's portfolio report: a nightly review of the developer's git repositories, written by another model.

What your answer does: a finding you pick becomes a coding task that runs with no human approval. An agent implements it in a fresh git worktree - a new branch off the default branch, holding only committed files - and commits there. Nothing is pushed. The developer reviews the finished commit later, and that review is the only checkpoint. A wrong pick costs them review time; a pick that needed their decision takes the decision away from them.

You have no tools and cannot read anything. Everything you get is in this message, and your answer is read by a program, not a person.

Step 1. Give every candidate finding a verdict.

"propose" means an agent could turn the finding into a committed change right now, and the developer would want that change without being asked first. All five must hold:
  1. The change is an edit to files tracked in one repository - code, tests, configuration or documentation - small enough to review in one sitting.
  2. Nobody has to decide anything first: no product, security-policy, release or ownership call, and no choice between options the report leaves open.
  3. It needs nothing outside the repository: no pushing, publishing, releasing or deploying, no creating remotes or accounts, no credentials, no network.
  4. It leaves work in progress alone: it does not commit, discard or depend on uncommitted or untracked files (the agent cannot see them), and it does not merge, rebase, pull, sync or reconcile branches.
  5. Whether it succeeded can be checked from the diff or by running the repository's own tests.

"never" is everything else. That includes a finding that is really a question for the developer ("decide whether", "confirm", "worth a decision"); a status observation (dormant, ahead of or behind its upstream, unpushed, no upstream, stale branch); anything about uncommitted or untracked files; anything that means pushing, releasing or publishing; a security finding whose fix needs a policy call; and broad audits, migrations or cross-repository changes.

Most findings are "never". A report where nothing qualifies is normal, and "never" for every candidate is a correct answer. When unsure, the answer is "never".

Step 2. Pick at most one finding to originate: the "propose" finding whose change you are most confident the developer wants. If no finding is "propose", the pick is null. Do not pick a finding that is the same work as something listed under "Already originated".

Answer with exactly one JSON object - no prose before or after it, no code fence. One verdict per candidate, in the order given:

{"verdicts": [{"id": "F1", "verdict": "never", "why": "<one short sentence>"}, {"id": "F2", "verdict": "propose", "why": "<one short sentence>"}], "pick": null}

With a pick, add three fields:

{"verdicts": [...], "pick": "F2", "task": "<one imperative sentence naming the change>", "acceptance_criteria": ["<checkable criterion>"], "risk": "low"}

"risk" is "low" or "medium"; a finding you would rate higher is "never". Give 1 to 5 acceptance criteria.

Candidates (id | repo | finding):
@@CANDIDATES@@

Already originated and awaiting review:
@@ALREADY@@

The report:
<report>
@@REPORT@@
</report>
"""

PROMPT_SHA = hashlib.sha256(f"{MODEL}\n{CALL_PROFILE}\n{PROMPT}".encode("utf-8")).hexdigest()[:12]


class OutputError(ValueError):
    """The model's answer broke the contract. Such an answer is never acted on."""


def _one_line(value: str) -> str:
    return " ".join(value.split())


def candidates(report_text: str, repos: Iterable[str] | None = None) -> list[dict]:
    """Numbered candidates from one report, one per corpus key.

    `repos` narrows them to those report sections -- the originator passes the
    sections of the repos it may originate in, the replay passes nothing.
    """
    allowed = None if repos is None else {r.lower() for r in repos}
    out: list[dict] = []
    seen: set[str] = set()
    for repo, text in findings.extract(report_text):
        k = findings.key(repo, text)
        if k in seen or (allowed is not None and repo.lower() not in allowed):
            continue
        seen.add(k)
        out.append({"id": f"F{len(out) + 1}", "repo": repo, "text": _one_line(text), "key": k})
    return out


def build_prompt(report_text: str, cands: list[dict], already: Iterable[str] = ()) -> str:
    if not cands:
        raise ValueError("no candidates: a proposer call with nothing to judge must not be made")
    listed = "\n".join(f"{c['id']} | {c['repo']} | {c['text']}" for c in cands)
    open_tasks = "\n".join(f"- {_one_line(a)}" for a in already) or "(none)"
    # The report goes in last, so nothing inside it can be taken for a placeholder.
    return (
        PROMPT.replace("@@CANDIDATES@@", listed)
        .replace("@@ALREADY@@", open_tasks)
        .replace("@@REPORT@@", report_text.strip())
    )


def _strip_fence(text: str) -> str:
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rstrip()
        if body.endswith("```"):
            body = body[:-3]
    return body.strip()


def parse(text: str, cands: list[dict]) -> dict:
    """Read the model's answer, or raise OutputError. Never guesses.

    Returns `{"verdicts": {id: (verdict, why)}, "pick": id | None, "task": str,
    "acceptance_criteria": [str], "risk": str}`. A missing, duplicated or
    unknown verdict, a pick that is not a "propose" candidate, or a pick
    without a usable task is a broken answer, not a partial one.
    """
    try:
        data = json.loads(_strip_fence(text))
    except ValueError as exc:
        raise OutputError(f"not a JSON object: {exc}") from None
    if not isinstance(data, dict):
        raise OutputError("not a JSON object")

    ids = [c["id"] for c in cands]
    raw = data.get("verdicts")
    if not isinstance(raw, list):
        raise OutputError("'verdicts' is not a list")
    verdicts: dict[str, tuple[str, str]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise OutputError("a verdict is not an object")
        vid, verdict = item.get("id"), item.get("verdict")
        if vid not in ids:
            raise OutputError(f"verdict for unknown id {vid!r}")
        if vid in verdicts:
            raise OutputError(f"two verdicts for {vid}")
        if verdict not in VERDICTS:
            raise OutputError(f"{vid}: verdict {verdict!r} is not one of {VERDICTS}")
        verdicts[vid] = (verdict, _one_line(str(item.get("why") or ""))[:MAX_WHY_CHARS])
    missing = [i for i in ids if i not in verdicts]
    if missing:
        raise OutputError(f"no verdict for {', '.join(missing)}")

    result = {"verdicts": verdicts, "pick": None, "task": "", "acceptance_criteria": [], "risk": ""}
    pick = data.get("pick")
    if pick is None:
        return result
    if pick not in ids:
        raise OutputError(f"pick {pick!r} is not a candidate")
    if verdicts[pick][0] != "propose":
        raise OutputError(f"pick {pick} has verdict {verdicts[pick][0]!r}, not 'propose'")

    task = data.get("task")
    if not isinstance(task, str) or not task.strip():
        raise OutputError("a pick needs a 'task' sentence")
    task = _one_line(task)
    if len(task) > MAX_TASK_CHARS:
        raise OutputError(f"'task' is {len(task)} characters, over {MAX_TASK_CHARS}")
    criteria = data.get("acceptance_criteria")
    if (
        not isinstance(criteria, list)
        or not 1 <= len(criteria) <= MAX_CRITERIA
        or not all(isinstance(c, str) and c.strip() for c in criteria)
    ):
        raise OutputError(f"a pick needs 1-{MAX_CRITERIA} non-empty 'acceptance_criteria' strings")
    risk = data.get("risk")
    if risk not in RISKS:
        raise OutputError(f"'risk' {risk!r} is not one of {RISKS}")

    result.update(
        pick=pick,
        task=task,
        acceptance_criteria=[_one_line(c) for c in criteria],
        risk=risk,
    )
    return result
