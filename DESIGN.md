# Forge

The unattended work pipeline: queued and delegated tasks, origination of new tasks,
implementation in worktrees, verification policy, the review ledger and the autonomy earned
from it. Split out of Jarvis on 2026-10-17 so Jarvis is an orchestrator that dispatches work to
Forge instead of doing it.

## Decisions (Anze, 2026-10-17)

| Question | Decision |
|---|---|
| Home | `~/.forge`, own git repo, code and `state/` together, own venv |
| Schedule | Jarvis triggers (`queue-dispatch`, `originate-task`, `review-reconcile` stay Jarvis jobs that call Forge) |
| Process | CLI and library, no service; state in `state/forge.db` |
| Sessions | Built last, in its own session, from this design |

## Boundary

Forge **does the work it is handed** and reports what happened. Jarvis decides what work is
allowed and when (scheduler, budget gate through Budget, the brake), and every outward action
Forge takes (commit to a shared branch, merge, push, publish) still goes through Jarvis's action
ladder (`daemon/actions.py`, called as a CLI like `mem export` does). Forge never pings or alerts;
it returns results and Jarvis tells Anze.

## What moves here

- `daemon/handlers/queue.py`, `delegation.py`, `originate.py`
- `daemon/implementer.py`, `proposer.py`, `review.py` (review items, ledger, reconcile),
  `replay.py`, `policy.py` (verification escalation policy), `g15.py` (origination gate),
  `playbook.py`, `earned.py` (autonomy proposals from review outcomes)
- Their tables in `jarvis.db` (queue items, delegations, review items, escalations as needed)
- `REVIEW-QUEUE.md` generation

Stays in Jarvis: the scheduler, `brain.py` (Jarvis's own judgment; Forge asks it through a small
interface), the action ladder and policy declarations in `config.toml` `[[actions]]`, and the
HUD's interactive task sessions (`hud/lib/tasks.js`, `merge.js`): those are Anze's own sessions,
not unattended work. Merging the two worktree mechanisms is a later, separate decision.

## Interface

- CLI `forge dispatch`, `forge originate`, `forge reconcile`, `forge ledger [days]`,
  `forge earned`; exit codes like the handlers' results today.
- Library entry points the three Jarvis handlers call; results as the handler strings today.

## First steps for the build session

1. Read `AUTONOMY-LADDER-DESIGN.md`, `PHASE6-DESIGN.md` and the G15 gate first: this is the
   most guarded part of Jarvis, and the soak gate counts its restarts and failures.
2. Map the coupling to `brain.py`, `actions.py`, `burn`/Budget and `core` before moving code.
3. Move in this order: earned and replay (read-only), review ledger, policy, then the
   implementer and the three handlers.
4. Selftest after every step; no behavior change is the bar for this split.
