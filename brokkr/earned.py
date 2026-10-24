"""Autonomy earned from approvals -- proposals only.

The design Anze asked for on 2026-09-05, built 2026-09-13: repeated ACCEPTED
reviews of autonomous work in one repo propose giving that repo more room;
repeated DISCARDS propose taking room away. The input is the review items in Mimir, whose
outcome git decides (merged into the base branch is accepted, deleted unmerged
is discarded) -- so every approval counted here is one the human made by
merging, not one this module inferred.

**Nothing here writes config.toml.** Acting on a proposal is the same act as
every other approval on the ladder: editing a declaration by hand. A proposal
that could apply itself would be the approval channel AUTONOMY-LADDER-DESIGN.md
§5 decided against, and it would break the standing rule that anything making
Jarvis do more stays a config edit by Anze.

    python daemon\\earned.py
"""

from __future__ import annotations

import os
import time
from typing import Any, Mapping

from . import review
from .host import host

# Trailing window, long enough that a slow month still accumulates a record.
WINDOW_S = 60 * 86400
# Accepted with no discard before proposing more room. Three, not one: a single
# good merge is an anecdote, and the declaration it would widen runs unattended.
EARN_AFTER = 3
# Discarded, and at least as many as accepted, before proposing less.
NARROW_AFTER = 2


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path)) if path else ""


def proposals(cfg: Mapping[str, Any] | None = None, now: float | None = None) -> list[str]:
    """One line per repo whose review record argues for a change, else []."""
    cfg = cfg if cfg is not None else host.load_config()
    now = now if now is not None else time.time()
    rows = [(i.get("repo"), i.get("outcome")) for i in review.cleared_since(now - WINDOW_S)
            if i.get("outcome") in (review.ACCEPTED, review.DISCARDED)]

    tally: dict[str, list] = {}
    for repo, outcome in rows:
        if not repo:
            continue
        entry = tally.setdefault(_key(repo), [repo, 0, 0])
        entry[1 if outcome == review.ACCEPTED else 2] += 1

    declared = {
        _key(str(d.get("target") or "")): d
        for d in cfg.get("actions", [])
        if d.get("name") == "originate-task"
    }

    out = []
    for key, (repo, accepted, discarded) in sorted(tally.items()):
        decl = declared.get(key)
        record = f"{accepted} accepted, {discarded} discarded in {repo} over 60 days"
        if accepted >= EARN_AFTER and discarded == 0:
            if decl:
                cap = int(decl.get("max_per_day", 1))
                out.append(f"earned: {record} - consider raising its originate-task max_per_day "
                           f"from {cap} to {cap + 1}")
            else:
                out.append(f"earned: {record}, and it is not an origination target - "
                           "consider declaring it one")
        elif discarded >= NARROW_AFTER and discarded >= accepted:
            if decl:
                out.append(f"narrow: {record} - consider lowering or removing its originate-task declaration")
            else:
                out.append(f"narrow: {record} - consider whether work for it should be delegated at all")
    return out


if __name__ == "__main__":
    lines = proposals()
    print("\n".join(lines) if lines else
          "no proposals - no repo has 3+ accepted reviews without a discard, or 2+ discards, in 60 days")
