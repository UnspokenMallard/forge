# Brokkr (Forge)

The unattended work pipeline: queued and delegated tasks, origination of new tasks,
implementation in worktrees, verification policy, the review ledger and the autonomy earned
from it. Split out of Jarvis on 2026-10-17 so Jarvis is an orchestrator that dispatches work to
Brokkr instead of doing it.

## Decisions (Anze, 2026-10-17)

| Question | Decision |
|---|---|
| Home | `~/.forge`, own git repo, code and `state/` together, own venv |
| Schedule | Jarvis triggers (`queue-dispatch`, `originate-task`, `review-reconcile` stay Jarvis jobs that call Brokkr) |
| Process | CLI and library, no service; no database of its own (review items and classifications live in Mimir, since 2026-10-24) |
| Sessions | Built last, in its own session, from this design |

## Boundary

Brokkr **does the work it is handed** and reports what happened. Jarvis decides what work is
allowed and when (scheduler, budget gate through Fafnir, the brake), and every outward action
Brokkr takes (commit to a shared branch, merge, push, publish) still goes through Jarvis's action
ladder (`daemon/actions.py`, called as a CLI like `mem export` does). Brokkr never pings or alerts;
it returns results and Jarvis tells Anze.

## What moves here

- `daemon/handlers/queue.py`, `delegation.py`, `originate.py`
- `daemon/implementer.py`, `proposer.py`, `review.py` (review items, ledger, reconcile),
  `replay.py`, `policy.py` (verification escalation policy), `g15.py` (origination gate),
  `playbook.py`, `earned.py` (autonomy proposals from review outcomes)
- Their tables in `jarvis.db` (queue items, delegations, review items, escalations as needed)
- `REVIEW-QUEUE.md` generation

Stays in Jarvis: the scheduler, `brain.py` (Jarvis's own judgment; Brokkr asks it through a small
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
| Coupling | Jarvis injects a small host interface (`brokkr/host.py`, bound by `~/.jarvis/daemon/brokkr_host.py`); Brokkr imports nothing from `~/.jarvis` |
| Files | G15 record, corpora and `REVIEW-QUEUE.md` move to `state/`; corpora copied and checked byte-equal first |
| Queue, worktrees | stay at `~/.jarvis/queue` and `~/.jarvis/worktrees`; Brokkr reads the paths (`store.QUEUE_PATH`, `store.WORKTREES`) |

## As built (2026-10-17)

- `brokkr/store.py` (paths from `BROKKR_HOME`, the intake and checkouts from `JARVIS_HOME`; tests set both to
  scratch), `host.py`, `review.py`, `earned.py`, `policy.py`, `replay.py`, `g15.py`, `proposer.py`,
  `findings.py`, `implementer.py`, and the job bodies `queue.py` (`dispatch`), `delegation.py` (`verify`),
  `originate.py` (`originate`); `cli.py` behind `bin/forge`; `tests/test_brokkr.py`.
- The host: `run_process`, `notify`, `record_limit`, `permit`, `record_outcome`, `brain_allowed`,
  `brain_work`, `brain_cli_path`, `ORIGINATION`, `scope_of`, `provider_of`, `env_for`, `env_for_path`,
  `SCOPES`, `load_config` (Jarvis's config.toml). (`connect` to jarvis.db is gone: events, actions and
  brain_calls are read from Mimir.) An unbound name raises `HostError`; only `load_config` has a read-only
  fallback, for the CLI.
  Jarvis's binding forwards at call time, so the selftest's replacements of `core.notify` etc. reach Brokkr.
- Jarvis keeps thin handlers (`handlers/queue.py`, `delegation.py`, `originate.py`, `review_check.py`,
  `corpus_capture.py` which runs `forge corpus capture` in a child), and imports `brokkr.review`,
  `brokkr.policy`, `brokkr.g15`, `brokkr.earned` where it read them before (brain, main, ping, webview,
  memorysync, status.py, the morning digest, run-issue-triage, Heimdall's config-drift check).
- Copied with their rows: `review_items` (2, ids kept) and `classifications` (29,390, new ids; a delta
  copy after the restart caught the rows the old process wrote). Files: `state/g15-corpus.json`,
  `state/g15-replay.json` (tracked in this repo by `!` lines, as they were in Jarvis's), `state/corpora/`
  (16 MB, identical). Backup `~/.jarvis/jarvis.db.bak-forge` deleted 2026-10-18.
- Cleanup done 2026-10-18 (Anze confirmed): `review_items` and `classifications` dropped from `jarvis.db`; the 655
  classification rows older than Brokkr's 30-day retention exported to `state/classifications-pre-cutover.json`
  first; `~/.jarvis/corpora`, `REVIEW-QUEUE.md`, `g15-corpus.json`, `g15-replay.json` deleted. Since then other
  sessions changed `store.REPORT` (Odin's report) and the host's `record_limit` to `alert_limit` (Bifrost).
- Behaviour checks after the restart: G15 still PASS (3 agreed, 0 rejected, 54 never), the policy gate still
  SUPPLY-BLOCKED, burn-replay 0 unexplained false positives, `queue-dispatch` "no pending entries",
  new classifications landing in `forge.db`.

## Storage in Mimir (2026-10-24)

Mimir is the only store (RECORDS-DESIGN.md in `~/.mind`). Brokkr opens no SQLite file and keeps files only for
the G15 record, the corpora and `REVIEW-QUEUE.md`, which are not data other parts need.

- Review items: records `brokkr.review_item`, key = item id. An open item carries `open: true` and
  `open_key: <item_key>`, so "one open item per task" is a lookup, not a scan; clearing sets `cleared_at`,
  `outcome`, `evidence` and removes both fields. Writes that must not be lost or doubled (`add`, `clear`,
  `attach_verdict`) go straight to Mimir and raise `MimirDown`; `add` logs and returns None instead.
- Classifications: telemetry stream `brokkr.classification` with `event_id`, `kind`, `decision`, `reason`,
  `eligible`, `ts` and `wanted` (the escalate/not verdict, stored so the gate need not re-derive it).
  Retention is Mimir's 90 days for telemetry (was 30 in `forge.db`); there is no prune job.
- Jarvis's events are read from the `jarvis.events` queue; the replay's anti-join (`event_id`) is done in
  Python over `mr.queue_all` and a grouped `mr.agg`. Reads raise `MimirDown`; the CLI prints "Mimir
  unreachable" and exits 3, and `capture_standard` reports each corpus it could not read.
- The ledger's spend comes from `mr.agg` over telemetry streams `fafnir.job_call`, `jarvis.brain_calls`
  and `fafnir.provider_call`.
- `tests/test_brokkr.py` runs against a scratch Mimir store (`MEMORY_DB`), in-process.
- Cutover: `~/.mind/tools/import-records.py brokkr` copies the old `forge.db` rows (classifications with
  `wanted`, items with `open`/`open_key`), checks counts, and renames it `forge.db.archived-2026-10-24`.

Differs from the design above, on inspection:

- `playbook.py` stays in Jarvis: it is the incident playbook (fixed steps per alert key for the ping and
  the HUD), not part of the work pipeline.
- `review.py` has no second table; the `CREATE TABLE IF NOT EXISTS does` hit was a comment.
- `[implementer]`, `[review]`, `[brain]` and `[[actions]]` stay in Jarvis's `config.toml`, read through the
  host: they are Jarvis's decisions about what work is allowed, and the HUD's config editor edits them there.
  Brokkr has no config.toml yet.
- `g15-replay.py` and `g15-corpus.py` stay in `~/.jarvis`: they read the portfolio report's history from
  Jarvis's git and call `brain.command`. They now import `brokkr.g15` / `brokkr.findings` and write
  `~/.forge/state`.
- `replay.py`'s corpora also feed Jarvis's `burn-replay.py`, which reads `~/.forge/state/corpora` now.
- Jarvis hot-reloads a changed handler file; Brokkr's modules are not hot-reloaded, so a Brokkr code change
  needs `~/.jarvis/restart.sh`.
