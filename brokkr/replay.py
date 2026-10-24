"""A corpus store and a replay runner, so history outlives the tables it came from.

Four replay harnesses exist in this repo and no two of them agree on anything:
`ladder-replay.py` walks the `actions` table, `burn-replay.py` walks
`provider_calls`, `g15-corpus.py` builds a labelled file from 12 nightly
`portfolio-report.md` versions in git history, and 2026-09-05's fixtures were
hand-written dicts inside the test. Each was invented at the moment it was
needed, which is why `NEXT-PROMPT.md` calls making "ships with a replay over
retained history" a standing requirement the cheapest quality gate left.

This is the shared half. It does not replace those four -- they are working
gates and rewriting a working gate to prove a refactor is a bad trade -- it
gives the fifth one somewhere to live.

### The property that matters: a corpus never shrinks

`core.prune_events` deletes events at 14 days. `burn` and `actions` rows age out
the same way. So the naive shape -- dump the table to a file, overwrite the file
next time -- is not a snapshot store, it is a *delayed* copy of the same
disappearing data. Run it twice a month apart and the second run silently
deletes what the first one saved.

That is not hypothetical here. `PHASE6-DESIGN.md` §5a already records the
dry-run gate's sample being carried by 37 replayed events against a floor of 25,
and says in as many words that the support shrinks as pruning proceeds and will
not clear the floor forever.

So `capture()` **merges by key and never removes**. A record whose key is
already stored is refreshed; a key that has vanished from the source stays.
Deleting from a corpus is a deliberate act -- `drop()` -- and there is no code
path that reaches it. The cost is that a corpus can hold a row whose source
truth has changed in a way nobody re-captured. That is the right way round:
a stale row is visible and arguable, a missing row is neither.

### Why every record must carry a key

Merging is impossible without identity, and the failure of guessing one is
silent duplication -- the corpus grows, the counts look healthy, and every
statistic computed over it is wrong in the direction that looks like success.
`capture()` raises on a record with no `key` rather than synthesising one from a
hash of the contents, because contents legitimately change between captures
(that is the point of refreshing) and a content hash would make every refresh a
new row.

### Corpora are gitignored by default

A corpus built from `events` or `provider_calls` carries payloads, and
`.gitignore`'s own comment gives the reason the database is excluded: it
"contains every event payload". `corpora/` is therefore ignored wholesale.

A corpus that is *ground truth* -- hand-written labels that would cost a person
hours to reproduce -- is the exception, and the exception is made one file at a
time with an explicit `!` line, exactly as `/g15-corpus.json` already is. Use
`export()` to write one out for that treatment, and read the labels before you
commit them.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import store
from ._mimir import records as mr
from .store import log

CORPUS_DIR = store.STATE / "corpora"

# events.json was 2.6 MB at its first capture (2026-09-06) and 4.5 MB a week
# later. Rewritten whole each night, it stays cheap well past this; this is the
# point to split it by month rather than keep rewriting one file.
SHARD_AT_BYTES = 25_000_000

# A record's identity. Chosen over an implicit index because a corpus is merged
# across captures taken at different times, and position is not identity.
KEY = "key"
# Ground truth, when a corpus has any. `run()` compares against this and stays
# silent about accuracy when it is absent, rather than reporting 100%.
LABEL = "label"


def _path(name: str) -> Path:
    """One file per corpus. Names are filesystem-simple on purpose.

    A corpus name reaches this function from a CLI argument and from module
    constants. Anything that is not a plain identifier is refused rather than
    sanitised: a corpus called `../../config` is not a naming accident worth
    quietly correcting.
    """
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        raise ValueError(f"corpus name must be alphanumeric/-/_ , got {name!r}")
    return CORPUS_DIR / f"{name}.json"


def load(name: str) -> dict | None:
    """The whole corpus document, or None if it could not be read.

    Three-valued in the same sense as `actions.py`'s exit 2, `inbound`'s failed
    poll and the incident resolvers: **"could not read" is its own answer and it
    is never the good one.** A caller that cannot tell an unreadable corpus from
    an empty one will report a gate as passing over zero records.
    """
    try:
        doc = json.loads(_path(name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("replay: corpus %s is unreadable: %s", name, exc)
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("records"), list):
        log.warning("replay: corpus %s is not a corpus document", name)
        return None
    return doc


def records(name: str) -> list[dict] | None:
    """Just the rows, or None. See `load` for why None is not `[]`."""
    doc = load(name)
    return None if doc is None else list(doc["records"])


def capture(
    name: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    source: str,
    note: str = "",
) -> dict:
    """Merge `rows` into the corpus by key. Returns a summary of what changed.

    Never shrinks -- see the module docstring. The returned dict reports `added`
    and `refreshed` separately so a capture that found nothing new is visible as
    such, rather than as a successful-looking no-op.
    """
    prepared: dict[str, dict] = {}
    for row in rows:
        record = dict(row)
        key = record.get(KEY)
        if key is None or str(key) == "":
            raise ValueError(
                f"corpus {name!r}: a record has no {KEY!r} -- identity cannot be "
                "guessed, and guessing it duplicates rows silently"
            )
        prepared[str(key)] = record

    existing = load(name)
    stored: dict[str, dict] = {}
    labels_kept = 0
    if existing is not None:
        for record in existing["records"]:
            key = str(record.get(KEY, ""))
            if key:
                stored[key] = record

    added, refreshed = 0, 0
    for key, record in prepared.items():
        previous = stored.get(key)
        if previous is None:
            stored[key] = record
            added += 1
            continue
        # A hand-written label is the expensive part of a corpus and is never
        # present in a fresh capture from a table. Carrying it across a refresh
        # is what lets a labelled corpus be re-captured at all.
        if LABEL in previous and LABEL not in record:
            record[LABEL] = previous[LABEL]
            labels_kept += 1
        if record != previous:
            refreshed += 1
        stored[key] = record

    doc = {
        "name": name,
        "source": source,
        "note": note or (existing or {}).get("note", ""),
        "captured_at": time.time(),
        "first_captured_at": (existing or {}).get("first_captured_at", time.time()),
        "captures": int((existing or {}).get("captures", 0)) + 1,
        "records": [stored[k] for k in sorted(stored)],
    }
    _write(name, doc)
    return {
        "name": name,
        "added": added,
        "refreshed": refreshed,
        "labels_carried": labels_kept,
        "total": len(stored),
        # The number that says the no-shrink promise held on this capture.
        "retained": len(stored) - len(prepared) if len(stored) > len(prepared) else 0,
    }


def _write(name: str, doc: dict) -> None:
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def capture_stream(name: str, rows: Iterable[Mapping[str, Any]], cols: tuple[str, ...], *, key_col: str,
                   source: str = "", note: str = "") -> dict:
    """Capture rows read from Mimir, keeping only `cols` and giving each the string key of `key_col`."""
    prepared = []
    for row in rows:
        record = {c: row.get(c) for c in cols}
        record[KEY] = str(record.get(key_col))
        prepared.append(record)
    return capture(name, prepared, source=source, note=note)


# --------------------------------------------------------------------------
# The standing corpora
#
# The four existing harnesses all read data that is being deleted underneath
# them: `ladder-replay.py` and `burn-replay.py` both walk `events`, which
# Mimir's queue prunes at 14 days, and `PHASE6-DESIGN.md` 5a already
# records the dry-run gate's sample resting on 37 replayed events against a
# floor of 25, with the note that it "clears the floor comfortably today and
# will not forever".
#
# Capturing on a schedule is what turns that from a countdown into a growing
# archive. It does not change any gate's arithmetic today; it means the evidence
# still exists the day someone needs to re-run one.
#
# Since 2026-10-24 the sources are Mimir's: Jarvis's events queue, and the
# `jarvis.action` records (a verdict's outcome is filled in later, so they are records) and the
# `fafnir.provider_call` telemetry stream. A record keeps
# the columns the old tables had, so a corpus captured before the move merges
# with one captured after it.
# --------------------------------------------------------------------------

EVENTS_QUEUE = "jarvis.events"
EVENT_COLS = ("id", "kind", "state", "payload", "result", "created_at", "updated_at", "started_at")


def _events() -> Iterable[Mapping[str, Any]]:
    for e in mr.queue_all(store.SOURCE, EVENTS_QUEUE):
        # The queue parses the payload; the corpus holds it as the text the events table held.
        yield {**e, "payload": json.dumps(e["payload"])}


def _stream(stream: str) -> Callable[[], Iterable[Mapping[str, Any]]]:
    """Every row of a telemetry stream, oldest first, as the data the table row had. A row Mimir
    holds without the table's own `id` (written after the move) gets `m<telemetry id>`, so keys stay unique."""
    def rows() -> Iterable[Mapping[str, Any]]:
        since = None
        seen: set = set()
        while True:
            page = mr.query(stream=stream, since=since, limit=5000)
            fresh = [r for r in page if r["id"] not in seen]
            for r in fresh:
                seen.add(r["id"])
                yield {**r["data"], "id": r["data"].get("id", f"m{r['id']}")}
            if len(page) < 5000 or not fresh:
                return
            since = page[-1]["at"]
    return rows


def _records(kind: str) -> Callable[[], Iterable[Mapping[str, Any]]]:
    """Every record of a kind, oldest first, as its data with the record's key as `id`."""
    def rows() -> Iterable[Mapping[str, Any]]:
        # shortcut: one page of 5000; Jarvis keeps its actions 90 days (~700 rows). Page by `since` if that grows 7x.
        for r in mr.query(kind=kind, order="asc", limit=5000):
            yield {**r["data"], "id": r["key"]}
    return rows


# (corpus, rows, columns, key column, note). The events corpus keeps `started_at` since 2026-09-13:
# burn-replay.py times a stuck flag from it, the way the live detector does, and rows captured
# without it can only be replayed on the queue-wait-inclusive upper bound.
STANDARD = (
    (
        "events",
        _events,
        EVENT_COLS,
        "id",
        "every event, the queue Mimir prunes at 14 days. Read by "
        "ladder-replay.py and burn-replay.py.",
    ),
    (
        "actions",
        _records("jarvis.action"),
        ("id", "ts", "action", "target", "tier", "authorized_by", "decision",
         "shadow", "outcome", "evidence"),
        "id",
        "every ladder verdict. The record of what was permitted, refused or "
        "consulted, and the input to ladder-replay.py.",
    ),
    (
        "provider-calls",
        _stream("fafnir.provider_call"),
        ("id", "ts", "surface", "model", "ok", "duration_s", "cost_usd"),
        "id",
        "spend, per call. The input to burn-replay.py and to the review "
        "ledger's amortised cost. `detail` is deliberately left out - it is the "
        "one column here that can carry free text from a provider.",
    ),
)


def capture_standard() -> list[str]:
    """Capture every standing corpus. Reports each one, including its failures.

    A source that cannot be read is a real answer and not an error: it is a line
    in a job result rather than a crash that stops the other captures. Mimir
    being down is the usual cause.
    """
    out = []
    for name, rows, cols, key_col, note in STANDARD:
        try:
            result = capture_stream(name, rows(), cols, key_col=key_col, note=note, source="mimir")
        except Exception as exc:  # noqa: BLE001 - one corpus must not stop the rest
            log.warning("replay: capture of %s failed: %s", name, exc)
            out.append(f"{name}: COULD NOT CAPTURE")
            continue
        line = (
            f"{name}: {result['total']} total (+{result['added']}, "
            f"~{result['refreshed']}, {result['retained']} retained past the source)"
        )
        # Every capture rewrites its file whole, and a corpus never shrinks, so
        # its size only grows. Printed nightly so the growth is on record; past
        # SHARD_AT_BYTES the line says what to do instead of trimming, which is
        # the one move this module exists to prevent.
        try:
            size = _path(name).stat().st_size
        except OSError:
            size = None
        if size is not None:
            line += f", {size / 1e6:.1f} MB"
            if size > SHARD_AT_BYTES:
                line += f" -- PAST {SHARD_AT_BYTES / 1e6:.0f} MB, shard it by month; never trim it"
        out.append(line)
    return out


class Verdict:
    """What a replay found. Truthy only when it actually ran and matched."""

    def __init__(self, name: str, total: int) -> None:
        self.name = name
        self.total = total
        self.ran = 0
        self.errors: list[str] = []
        self.mismatches: list[str] = []
        self.labelled = 0

    @property
    def ok(self) -> bool:
        return self.ran == self.total and not self.errors and not self.mismatches

    def lines(self) -> list[str]:
        out = [f"replay {self.name}: {self.ran}/{self.total} record(s)"]
        if self.labelled:
            matched = self.labelled - len(self.mismatches)
            out.append(f"  labelled  : {matched}/{self.labelled} matched")
        else:
            # The same rule the criteria scorer and the review ledger follow: a
            # check that did not run must never render as a check that passed.
            out.append("  labelled  : none - this replay proves the code RUNS over "
                       "real history, not that it is CORRECT over it")
        for bad in self.mismatches[:10]:
            out.append(f"  mismatch  : {bad}")
        for err in self.errors[:10]:
            out.append(f"  error     : {err}")
        return out


def run(name: str, fn: Callable[[dict], Any]) -> Verdict | None:
    """Apply `fn` to every record. None if the corpus could not be read.

    Where a record carries a `label`, `fn`'s return value is compared against it
    and a difference is a mismatch. Where it does not, the replay still proves
    the code survives real history -- which is what caught the two silent bugs
    on 2026-09-03 -- and `Verdict.lines()` says so rather than implying accuracy
    it did not measure.
    """
    rows = records(name)
    if rows is None:
        return None
    verdict = Verdict(name, len(rows))
    for record in rows:
        key = record.get(KEY)
        try:
            result = fn(record)
        except Exception as exc:  # noqa: BLE001 - surviving history is the test
            verdict.errors.append(f"{key}: {type(exc).__name__}: {exc}")
            continue
        verdict.ran += 1
        if LABEL in record:
            verdict.labelled += 1
            if result != record[LABEL]:
                verdict.mismatches.append(f"{key}: got {result!r}, labelled {record[LABEL]!r}")
    return verdict


def catalogue() -> list[dict]:
    """Every corpus on disk, with the facts that decide whether to trust it."""
    out = []
    if not CORPUS_DIR.exists():
        return out
    for path in sorted(CORPUS_DIR.glob("*.json")):
        doc = load(path.stem)
        if doc is None:
            out.append({"name": path.stem, "records": None, "note": "UNREADABLE"})
            continue
        rows = doc["records"]
        out.append({
            "name": doc.get("name", path.stem),
            "records": len(rows),
            "labelled": sum(1 for r in rows if LABEL in r),
            "captures": doc.get("captures", 0),
            "captured_at": doc.get("captured_at"),
            "first_captured_at": doc.get("first_captured_at"),
            "source": doc.get("source", ""),
            "note": doc.get("note", ""),
        })
    return out


def export(name: str, dest: Path | None = None) -> Path | None:
    """Copy a corpus to the repo root so it can be committed deliberately.

    Ground-truth labels are the only thing that earns a place in git here, and
    they earn it one file at a time with its own `!` line in `.gitignore` --
    same treatment `/g15-corpus.json` already has, for the same reason.
    """
    doc = load(name)
    if doc is None:
        return None
    target = dest or (store.ROOT / f"{name}-corpus.json")
    target.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    return target


def drop(name: str) -> bool:
    """Delete a corpus. Nothing in this module calls it; that is the point."""
    try:
        _path(name).unlink()
        return True
    except FileNotFoundError:
        return False


def _age(seconds: float | None) -> str:
    if not seconds:
        return "never"
    delta = time.time() - float(seconds)
    if delta < 3600:
        return f"{delta / 60:.0f}m ago"
    if delta < 86400:
        return f"{delta / 3600:.0f}h ago"
    return f"{delta / 86400:.0f}d ago"


def report() -> str:
    rows = catalogue()
    if not rows:
        return ("no corpora captured\n\n"
                "  Nothing here yet. A corpus is how a gate keeps its evidence after\n"
                "  core.prune_events deletes the rows it was computed from (14 days).")
    lines = [f"corpora ({len(rows)})", ""]
    for row in rows:
        if row["records"] is None:
            lines.append(f"  {row['name']:24} UNREADABLE")
            continue
        label = f", {row['labelled']} labelled" if row["labelled"] else ""
        lines.append(
            f"  {row['name']:24} {row['records']:5} record(s){label}"
            f"  captured {row['captures']}x, last {_age(row['captured_at'])}"
        )
        if row["note"]:
            lines.append(f"  {'':24} {row['note']}")
    return "\n".join(lines)


def _cli(argv: list[str]) -> int:
    if not argv or argv[0] == "list":
        print(report())
        return 0
    if argv[0] == "capture":
        # The nightly `corpus-capture` job runs this in a child process, so the
        # rows it loads never become the daemon's resident memory.
        for line in capture_standard():
            print(line)
        return 0
    if argv[0] == "show" and len(argv) > 1:
        doc = load(argv[1])
        if doc is None:
            print(f"corpus {argv[1]!r}: unreadable or absent")
            return 2  # could not check, never confused with "empty"
        print(json.dumps(doc, indent=2, sort_keys=True)[:20000])
        return 0
    if argv[0] == "export" and len(argv) > 1:
        target = export(argv[1])
        if target is None:
            print(f"corpus {argv[1]!r}: unreadable or absent")
            return 2
        print(f"wrote {target}")
        print("Add an explicit '!' line to .gitignore if this is to be committed, "
              "and read the labels first.")
        return 0
    print("usage: forge corpus [list | capture | show <name> | export <name>]")
    return 1

