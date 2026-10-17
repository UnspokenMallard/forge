"""What Forge asks of the program that runs it (Jarvis), as one small interface.

Forge does the work it is handed; Jarvis decides what work is allowed and when, keeps the
ladder, the brain and the process runner, and tells Anze. Forge never imports any of that: the
host binds the callables once at startup (`daemon/forge_host.py`), and a test binds fakes.

    run_process(cmd, timeout_s=, cwd=, input_text=, env=) -> (code, output)    async
    notify(key, message, severity=, cooldown_s=)            Jarvis's alert path; Forge never pings
    alert_limit(provider)                                   alert a usage limit Gateway recorded in Budget
    permit(action, target, cfg=, evidence=) -> Decision     the autonomy ladder; .allowed .reason .action_id
    record_outcome(action_id, text)
    brain_allowed(cfg, purpose, scope) -> (ok, why)         the brain's caps, breaker, limit and brake
    brain_work(prompt, **kw) -> dict                        one isolated model call
    brain_cli_path() -> str | None
    ORIGINATION                                             the brain purpose constant
    scope_of(path), provider_of(scope), env_for(scope), env_for_path(path), SCOPES
    load_config() -> dict                                   Jarvis's config.toml ([actions], [brain], ...)
    connect()                                               Jarvis's jarvis.db (events, actions, brain_calls)

Unbound names raise HostError, never return a default that looks like an answer. Two have a
read-only stdlib fallback so the CLI works without a daemon: `load_config` and `connect`.
"""
from __future__ import annotations

import contextlib
import sqlite3
import tomllib

from . import store


class HostError(RuntimeError):
    """Forge was asked to do something that needs Jarvis, and no host is bound."""


def _load_config() -> dict:
    try:
        with open(store.JARVIS / "config.toml", "rb") as fh:
            return tomllib.load(fh)
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def _connect():
    conn = sqlite3.connect(store.JARVIS / "jarvis.db", timeout=30)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class _Host:
    def __init__(self) -> None:
        self._bound: dict[str, object] = {"load_config": _load_config, "connect": _connect}

    def bind(self, **names: object) -> None:
        self._bound.update(names)

    def unbind(self) -> None:
        self.__init__()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._bound[name]
        except KeyError:
            raise HostError(
                f"forge host has no {name!r}: Jarvis binds it at startup (daemon/forge_host.py)"
            ) from None


host = _Host()
