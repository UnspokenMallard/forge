"""Forge: the unattended work pipeline (queue, delegation, origination, review ledger, policy, replay).

Import as `from forge import review`. Budget's and Gateway's libraries are siblings on `sys.path`.
"""
import sys
from pathlib import Path

_BUDGET = str(Path.home() / ".budget")  # `from budget import ledger`; BUDGET_HOME (tests) moves only its data
_GATEWAY = str(Path.home() / ".gateway")  # `import gateway` (every model call); GATEWAY_HOME moves only its data
for _p in (_BUDGET, _GATEWAY):
    if _p not in sys.path:
        sys.path.append(_p)
