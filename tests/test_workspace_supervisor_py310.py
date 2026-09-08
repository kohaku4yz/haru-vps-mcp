from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR_PATH = ROOT / "deploy" / "workspace" / "workspace-supervisor.py"
SPEC = importlib.util.spec_from_file_location("haru_workspace_supervisor_py310", SUPERVISOR_PATH)
assert SPEC is not None and SPEC.loader is not None
supervisor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = supervisor
SPEC.loader.exec_module(supervisor)


@pytest.mark.asyncio
async def test_wait_or_stop_real_timeout_returns_normally() -> None:
    await supervisor._wait_or_stop(asyncio.Event(), 0.001)


@pytest.mark.asyncio
async def test_proxy_stop_grace_catches_asyncio_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_wait_for(awaitable, *, timeout):
        nonlocal calls
        calls += 1
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        raise asyncio.TimeoutError

    class FakeProcess:
        returncode = None
        pid = 424242

        async def wait(self):
            return 0

    monkeypatch.setattr(supervisor.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(supervisor.os, "killpg", lambda *_args: None)

    await supervisor._terminate_proxy_group(FakeProcess(), 0.001)
    assert calls == 2
