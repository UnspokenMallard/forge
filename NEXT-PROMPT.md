# Next prompt: Forge

DONE 2026-10-18: Forge is built and live (`~/.forge/DESIGN.md`, "As built"; Jarvis commit "Move the work
pipeline, review ledger, policy and corpora into Forge"). The first-night check passed and the cleanup is
done (old tables dropped, 655 old classification rows exported to `state/classifications-pre-cutover.json`,
old files deleted). Nothing is left; the table below is the record.

## When to fire this

Earliest-useful dates, not deadlines; being late costs nothing.

| When | Step | Waits on |
|---|---|---|
| 2026-10-18 08:00 | first-night check: the 03:20 `corpus-capture` result in `status.py` reads `events: ... total` (from `~/.forge/state/corpora`, which `forge corpus list` shows captured today); `review-reconcile` and `queue-dispatch` results normal; new rows in `~/.forge/state/forge.db` `classifications`, none newer than 2026-10-17 21:46 in `~/.jarvis/jarvis.db`; morning digest still has its review section when something waits; no `handler-reload` or `handler-failed:queue-dispatch` incident | the first night |
| after that check, Anze | drop `review_items` and `classifications` from `~/.jarvis/jarvis.db`; delete `jarvis.db.bak-forge`, `~/.jarvis/corpora/`, `~/.jarvis/REVIEW-QUEUE.md`, and `git rm` `~/.jarvis/g15-corpus.json` and `g15-replay.json` (Forge's copies are tracked in this repo) | Anze's confirmation |

## A: advance the work

> Read `~/.forge/DESIGN.md` ("As built") and this file. Do the first row whose date has passed and report.
> Rules: Forge never alerts, pings or holds work; Jarvis triggers it and acts. Forge imports nothing from
> `~/.jarvis`: what it needs from Jarvis goes through `forge/host.py`, bound by `~/.jarvis/daemon/forge_host.py`.
> An unbound host name raises; never give it a default that looks like an answer. Never widen `[[actions]]`
> targets, never add a push or open-pull-request action, never delete anything under `~/.jarvis/worktrees`.
> G15 is recomputed, never stored; do not edit `proposer.PROMPT` and re-run the replay against the same labels.
> A corpus never shrinks. Never drop a table or delete a backup or a file copy without Anze's confirmation.
> A Forge code change needs `~/.jarvis/restart.sh` (Forge is not hot-reloaded). Stop and tell me if anything
> differs from what this file expects.

## B: status only

> Run `~/.forge/bin/forge review`, `~/.forge/bin/forge ledger`, `~/.forge/bin/forge g15`,
> `~/.forge/bin/forge policy`, `~/.forge/bin/forge corpus list`, `~/.forge/.venv/bin/python
> ~/.forge/tests/test_forge.py` and `~/.jarvis/.venv/bin/python ~/.jarvis/selftest.py`. Change nothing.

## C: something is broken

> Forge or the Jarvis daemon broke after the cutover. First, do not delete `state/forge.db`, `jarvis.db`, a
> backup, a corpus or a worktree: they are the evidence. Copy the db with `sqlite3 .backup`, run
> `~/.jarvis/.venv/bin/python ~/.jarvis/status.py` and the selftest, read `~/.jarvis/logs/jarvis.log`, and
> report before fixing. `git -C ~/.jarvis log` and `git -C ~/.forge log` show the cutover commits.
