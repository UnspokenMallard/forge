# Next prompt: Forge

DONE 2026-10-18: Forge is built and live (`~/.forge/DESIGN.md`, "As built"; Jarvis commit "Move the work
pipeline, review ledger, policy and corpora into Forge"). The first-night check passed and the cleanup is
done (old tables dropped, 655 old classification rows exported to `state/classifications-pre-cutover.json`,
old files deleted). Nothing is left; the table below is the record.

UPDATE 2026-10-24: Forge (Brokkr) no longer has `state/forge.db`; its rows live in Mimir (`DESIGN.md`, "Storage in
Mimir"). Read `state/forge.db` and `classifications` below as `brokkr.review_item` records and
`brokkr.classification` telemetry. After the import the old file is `state/forge.db.archived-2026-10-24`.

## When to fire this

Earliest-useful dates, not deadlines; being late costs nothing.

| When | Step | Waits on |
|---|---|---|
| 2026-10-18 08:00 | first-night check: the 03:20 `corpus-capture` result in `status.py` reads `events: ... total` (from `~/.forge/state/corpora`, which `forge corpus list` shows captured today); `review-reconcile` and `queue-dispatch` results normal; new rows in `~/.forge/state/forge.db` `classifications`, none newer than 2026-10-17 21:46 in `~/.jarvis/jarvis.db`; morning digest still has its review section when something waits; no `handler-reload` or `handler-failed:queue-dispatch` incident | the first night |
| after that check, Anze | drop `review_items` and `classifications` from `~/.jarvis/jarvis.db`; delete `jarvis.db.bak-forge`, `~/.jarvis/corpora/`, `~/.jarvis/REVIEW-QUEUE.md`, and `git rm` `~/.jarvis/g15-corpus.json` and `g15-replay.json` (Forge's copies are tracked in this repo) | Anze's confirmation |

## Audit 2026-10-21: your items

From the Jarvis audit (HUD task audit-jarvis-and-all-its-com-e5a9). The evidence, effort, risk and
done-criteria of each item are in `~/.jarvis/AUDIT-2026-10-21.md`; the phases and Anze's decisions
are in `~/.jarvis/NEXT-PROMPT.md`. Build an item only after Anze approves its phase. Prompt A below
covers these rows too. The rule stays: this component measures or knows; Jarvis alerts and acts.

- **A09 / A10 (Phase 1, Jarvis side)** Remediation diagnoses cannot read where incidents happen and
  have fixed nothing. When the read rules widen (D4), the Forge review queue should stop filling
  with duplicates: 4 of its items are the same config-drift incident.
- **A16 (Phase 1)** Proposer feedback: 55 of 57 decisions were accepts, the 2 rejects have no
  reason, and no proposer prompt reads the "Jarvis decisions" notes. Feed recent decisions and every
  reject into the review and originate prompts; count "accepted" only when merged and not reverted
  after 7 days.
- **A25 (Phase 2)** originate-task has been disabled since 10-01 (0 picks in 14 runs) and
  queue-dispatch polls an empty queue every 5 minutes. Decided 2026-10-21 (D9): retire originate-task;
  keep queue-dispatch for hand-written entries, polled less often (hourly).
- **A38 (Phase 3)** issue-triage is designated to move here from Analyst.
- **A39 (Phase 4)** `brokkr/delegation.py:172` docstring is out of date.

| When (earliest useful, not a deadline) | Step | Waits on |
|---|---|---|
| with Phase 1 | A16 | Phase 1 approval |
| with Phase 2 | A25: remove originate-task, slow queue-dispatch to hourly | Phase 2 approval |

## A: advance the work

> Read `~/.forge/DESIGN.md` ("As built") and this file. Do the first row whose date has passed and report.
> Rules: Forge never alerts, pings or holds work; Jarvis triggers it and acts. Forge imports nothing from
> `~/.jarvis`: what it needs from Jarvis goes through `brokkr/host.py`, bound by `~/.jarvis/daemon/brokkr_host.py`.
> An unbound host name raises; never give it a default that looks like an answer. Never widen `[[actions]]`
> targets, never add a push or open-pull-request action, never delete anything under `~/.jarvis/worktrees`.
> G15 is recomputed, never stored; do not edit `proposer.PROMPT` and re-run the replay against the same labels.
> A corpus never shrinks. Never drop a table or delete a backup or a file copy without Anze's confirmation.
> A Forge code change needs `~/.jarvis/restart.sh` (Forge is not hot-reloaded). Stop and tell me if anything
> differs from what this file expects.

## B: status only

> Run `~/.forge/bin/forge review`, `~/.forge/bin/forge ledger`, `~/.forge/bin/forge g15`,
> `~/.forge/bin/forge policy`, `~/.forge/bin/forge corpus list`, `~/.forge/.venv/bin/python
> ~/.forge/tests/test_brokkr.py` and `~/.jarvis/.venv/bin/python ~/.jarvis/selftest.py`. Change nothing.

## C: something is broken

> Forge or the Jarvis daemon broke after the cutover. First, do not delete `state/forge.db*` (or its archive), `jarvis.db*`, a
> backup, a corpus or a worktree: they are the evidence. Copy the db with `sqlite3 .backup`, run
> `~/.jarvis/.venv/bin/python ~/.jarvis/status.py` and the selftest, read `~/.jarvis/logs/jarvis.log`, and
> report before fixing. `git -C ~/.jarvis log` and `git -C ~/.forge log` show the cutover commits.
