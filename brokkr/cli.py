"""`forge` CLI (bin/forge). Each command is the library call a Jarvis job makes, with the same output.

  forge dispatch                       queue-dispatch: at most one pending entry to the implementer
  forge originate                      originate-task: at most one finding becomes one pending entry
  forge reconcile                      ask git what happened to each waiting branch, re-render REVIEW-QUEUE.md
  forge ledger [days]                  review ledger: accept rate, spend, minutes
  forge review [render | minutes <id> <min> | ledger [days]]   the review queue; exit 1 when something waits
  forge earned                         autonomy proposals from review outcomes (proposals only)
  forge policy                         the Phase 4 dry-run gate; exit 0 pass, 1 fail, 2 supply-blocked
  forge g15                            the origination gate, recomputed; exit 0 pass, 1 not
  forge corpus [list | capture | show <name> | export <name>]

`dispatch` and `originate` spend a model and write to the ladder, so they run on Jarvis's host
binding (`~/.jarvis/daemon/brokkr_host.py`, import it with Jarvis's interpreter). The rest read and
write Brokkr's own store and need no host beyond a git runner, which this module binds.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys

from . import earned, g15, policy, replay, review
from ._mimir import records as mr
from .host import host


async def _run_process(cmd, timeout_s=900, cwd=None, input_text=None, env=None):
    """A plain runner for `git` reads when no daemon is bound; Jarvis's replaces it."""
    def run():
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, cwd=cwd,
                                  input=input_text, env=env)
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        except OSError as exc:
            return 127, str(exc)
    return await asyncio.get_running_loop().run_in_executor(None, run)


def _policy() -> int:
    passed, lines = policy.gate()
    blocked, detail = policy.supply()
    print(f"policy dry-run gate — window {policy.MIN_DAYS}d\n")
    print("\n".join(lines))
    print(f"\n  supply: {detail}")
    if passed:
        verdict, code = "PASS", 0
    elif blocked:
        # Exit 2 for "cannot be judged yet", matching soak-report.py's convention, so a caller can
        # tell "not proven safe" from "waiting on a sample that is not coming".
        verdict, code = "SUPPLY-BLOCKED — waiting will not open this gate", 2
    else:
        verdict, code = "FAIL", 1
    print(f"\nVERDICT: {verdict}")
    return code


def _earned() -> int:
    lines = earned.proposals()
    print("\n".join(lines) if lines else
          "no proposals - no repo has 3+ accepted reviews without a discard, or 2+ discards, in 60 days")
    return 0


def _g15() -> int:
    result = g15.evaluate()
    print(("PASS: " if result.get("passed") else "NOT PASSED: ") + str(result.get("reason", "")))
    return 0 if result.get("passed") else 1


def _jarvis_job(name: str) -> int:
    """Run `dispatch` or `originate` on Jarvis's host, as the daemon would."""
    from . import store
    sys.path.insert(0, str(store.JARVIS / "daemon"))
    try:
        import brokkr_host  # noqa: F401 - binds on import
    except ImportError as exc:
        print(f"forge {name}: needs Jarvis's host binding ({exc}); run it with Jarvis's interpreter")
        return 2
    from . import originate, queue
    fn = {"dispatch": queue.dispatch, "originate": originate.originate}[name]
    print(asyncio.run(fn({})))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Exit 3 when Mimir does not answer: Brokkr's items and classifications live there."""
    try:
        return _main(argv)
    except mr.MimirDown as exc:
        print(f"forge: Mimir unreachable ({exc})")
        return 3


def _main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    host.bind(run_process=_run_process)
    cmd, rest = (argv[0], argv[1:]) if argv else ("review", [])
    if cmd in ("dispatch", "originate"):
        return _jarvis_job(cmd)
    if cmd == "reconcile":
        return review._cli(["reconcile"])
    if cmd == "ledger":
        return review._cli(["ledger", *rest])
    if cmd == "review":
        return review._cli(rest)
    if cmd == "earned":
        return _earned()
    if cmd == "policy":
        return _policy()
    if cmd == "g15":
        return _g15()
    if cmd == "corpus":
        return replay._cli(rest)
    print(__doc__)
    return 1
