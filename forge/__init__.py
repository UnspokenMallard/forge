"""Forge: the unattended work pipeline (queue, delegation, origination, review ledger, policy, replay).

Import as `from forge import review`. Budget's library is a sibling on `sys.path`.
"""
import sys
from pathlib import Path

_BUDGET = str(Path.home() / ".budget")  # `from budget import ledger`; BUDGET_HOME (tests) moves only its data
if _BUDGET not in sys.path:
    sys.path.append(_BUDGET)
