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

## Decisions at build time (Anze, 2026-10-17)

| Question | Decision |
|---|---|
| Tables | `review_items` and `classifications` move to `state/forge.db`; `escalations` stays with `brain.py`, `actions` with the ladder |
| Coupling | Jarvis injects a small host interface (`forge/host.py`, bound by `~/.jarvis/daemon/forge_host.py`); Forge imports nothing from `~/.jarvis` |
| Files | G15 record, corpora and `REVIEW-QUEUE.md` move to `state/`; corpora copied and checked byte-equal first |
| Queue, worktrees | stay at `~/.jarvis/queue` and `~/.jarvis/worktrees`; Forge reads the paths (`store.QUEUE_PATH`, `store.WORKTREES`) |

## As built (2026-10-17)

- `forge/store.py` (paths from `FORGE_HOME`, the intake and checkouts from `JARVIS_HOME`; tests set both to
  scratch), `host.py`, `review.py`, `earned.py`, `policy.py`, `replay.py`, `g15.py`, `proposer.py`,
  `findings.py`, `implementer.py`, and the job bodies `queue.py` (`dispatch`), `delegation.py` (`verify`),
  `originate.py` (`originate`); `cli.py` behind `bin/forge`; `tests/test_forge.py`.
- The host: `run_process`, `notify`, `record_limit`, `permit`, `record_outcome`, `brain_allowed`,
  `brain_work`, `brain_cli_path`, `ORIGINATION`, `scope_of`, `provider_of`, `env_for`, `env_for_path`,
  `SCOPES`, `load_config` (Jarvis's config.toml), `connect` (jarvis.db: events, actions, brain_calls). An
  unbound name raises `HostError`; only `load_config` and `connect` have a read-only fallback, for the CLI.
  Jarvis's binding forwards at call time, so the selftest's replacements of `core.notify` etc. reach Forge.
- Jarvis keeps thin handlers (`handlers/queue.py`, `delegation.py`, `originate.py`, `review_check.py`,
  `corpus_capture.py` which runs `forge corpus capture` in a child), and imports `forge.review`,
  `forge.policy`, `forge.g15`, `forge.earned` where it read them before (brain, main, ping, webview,
  memorysync, status.py, the morning digest, run-issue-triage, Sentinel's config-drift check).
- Copied with their rows: `review_items` (2, ids kept) and `classifications` (29,390, new ids; a delta
  copy after the restart caught the rows the old process wrote). Files: `state/g15-corpus.json`,
  `state/g15-replay.json` (tracked in this repo by `!` lines, as they were in Jarvis's), `state/corpora/`
  (16 MB, identical). Backup `~/.jarvis/jarvis.db.bak-forge` deleted 2026-10-18. Old tables and file copies stay in Jarvis
  until Anze confirms dropping them.
- Behaviour checks after the restart: G15 still PASS (3 agreed, 0 rejected, 54 never), the policy gate still
  SUPPLY-BLOCKED, burn-replay 0 unexplained false positives, `queue-dispatch` "no pending entries",
  new classifications landing in `forge.db`.

Differs from the design above, on inspection:

- `playbook.py` stays in Jarvis: it is the incident playbook (fixed steps per alert key for the ping and
  the HUD), not part of the work pipeline.
- `review.py` has no second table; the `CREATE TABLE IF NOT EXISTS does` hit was a comment.
- `[implementer]`, `[review]`, `[brain]` and `[[actions]]` stay in Jarvis's `config.toml`, read through the
  host: they are Jarvis's decisions about what work is allowed, and the HUD's config editor edits them there.
  Forge has no config.toml yet.
- `g15-replay.py` and `g15-corpus.py` stay in `~/.jarvis`: they read the portfolio report's history from
  Jarvis's git and call `brain.command`. They now import `forge.g15` / `forge.findings` and write
  `~/.forge/state`.
- `replay.py`'s corpora also feed Jarvis's `burn-replay.py`, which reads `~/.forge/state/corpora` now.
- Jarvis hot-reloads a changed handler file; Forge's modules are not hot-reloaded, so a Forge code change
  needs `~/.jarvis/restart.sh`.
