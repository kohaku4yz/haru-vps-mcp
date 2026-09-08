#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import builtins
import json
import logging
import os
import signal
from pathlib import Path
from urllib.parse import quote

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger("haru_workspace_supervisor")

DEFAULT_STARTUP_TIMEOUT_SECONDS = 20.0
DEFAULT_PROBE_INTERVAL_SECONDS = 1.0
DEFAULT_PROBE_TIMEOUT_SECONDS = 2.0
DEFAULT_FAILURE_THRESHOLD = 2
DEFAULT_STOP_GRACE_SECONDS = 5.0
_BASE_EXCEPTION_GROUP = getattr(builtins, "BaseExceptionGroup", None)


def _classify_exception(exc: BaseException) -> str:
    if _BASE_EXCEPTION_GROUP is not None and isinstance(exc, _BASE_EXCEPTION_GROUP):
        categories = {_classify_exception(child) for child in exc.exceptions}
        for preferred in ("timeout", "unreachable", "session_closed"):
            if preferred in categories:
                return preferred
        return "session_closed"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, ConnectionError)):
        return "unreachable"
    if isinstance(exc, (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)):
        return "session_closed"
    if type(exc).__name__ in {"McpError", "SessionError"}:
        return "protocol_error"
    return "protocol_error"


async def probe_backend(endpoint: str, timeout_seconds: float) -> str:
    try:
        with anyio.fail_after(timeout_seconds):
            async with streamablehttp_client(endpoint) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.list_tools()
        return "healthy"
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        return _classify_exception(exc)


def load_backend_names(config_path: Path) -> list[str]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        raise ValueError("named-server config must contain at least one mcpServers entry")
    names: list[str] = []
    for name in servers:
        if not isinstance(name, str) or not name or not all(ch.isalnum() or ch in "_-" for ch in name):
            raise ValueError("named-server config contains an invalid server name")
        names.append(name)
    return names


async def probe_all(host: str, port: int, backend_names: list[str], timeout_seconds: float) -> dict[str, str]:
    async def one(name: str) -> tuple[str, str]:
        endpoint = f"http://{host}:{port}/servers/{quote(name, safe='')}/mcp"
        return name, await probe_backend(endpoint, timeout_seconds)

    pairs = await asyncio.gather(*(one(name) for name in backend_names))
    return dict(pairs)


async def _wait_or_stop(stop_requested: asyncio.Event, delay_seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_requested.wait(), timeout=delay_seconds)
    except (TimeoutError, asyncio.TimeoutError):
        pass


def _nudge_proxy_to_stop(process: asyncio.subprocess.Process) -> None:
    """Best-effort only; systemd owns final cgroup cleanup after supervisor failure."""
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass


async def _terminate_proxy_group(process: asyncio.subprocess.Process, grace_seconds: float) -> None:
    """Clean shutdown path used for deliberate supervisor stop, not backend failure."""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except (TimeoutError, asyncio.TimeoutError):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=max(1.0, grace_seconds))
    except (TimeoutError, asyncio.TimeoutError):
        logger.error("event=workspace_supervisor category=proxy_stop_timeout")


async def supervise(args: argparse.Namespace) -> int:
    config_path = Path(args.named_server_config).resolve()
    backend_names = load_backend_names(config_path)
    workspace_cwd = str(Path.cwd())
    proxy_command = [
        args.proxy,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--named-server-config",
        str(config_path),
    ]
    process = await asyncio.create_subprocess_exec(*proxy_command, cwd=workspace_cwd, start_new_session=True)
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_requested.set)
        except NotImplementedError:
            pass

    logger.info("event=workspace_supervisor category=proxy_started backends=%s", ",".join(sorted(backend_names)))
    startup_deadline = loop.time() + args.startup_timeout_seconds
    last_startup: dict[str, str] = {}

    while loop.time() < startup_deadline:
        if stop_requested.is_set():
            await _terminate_proxy_group(process, args.stop_grace_seconds)
            return 0
        if process.returncode is not None:
            logger.error("event=workspace_supervisor category=proxy_exited phase=startup returncode=%s", process.returncode)
            return 1
        last_startup = await probe_all(args.host, args.port, backend_names, args.probe_timeout_seconds)
        if all(state == "healthy" for state in last_startup.values()):
            logger.info("event=workspace_supervisor category=healthy phase=startup")
            break
        await _wait_or_stop(stop_requested, args.probe_interval_seconds)
    else:
        logger.error("event=workspace_supervisor category=startup_unhealthy action=fail_workspace states=%s", json.dumps(last_startup, sort_keys=True))
        _nudge_proxy_to_stop(process)
        return 1

    consecutive_failures = 0
    while True:
        if stop_requested.is_set():
            logger.info("event=workspace_supervisor category=stopping reason=signal")
            await _terminate_proxy_group(process, args.stop_grace_seconds)
            return 0
        if process.returncode is not None:
            logger.error("event=workspace_supervisor category=proxy_exited phase=monitor returncode=%s", process.returncode)
            return 1

        states = await probe_all(args.host, args.port, backend_names, args.probe_timeout_seconds)
        if all(state == "healthy" for state in states.values()):
            if consecutive_failures:
                logger.info("event=workspace_supervisor category=health_recovered")
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            logger.warning(
                "event=workspace_supervisor category=backend_unhealthy failures=%s threshold=%s states=%s",
                consecutive_failures,
                args.failure_threshold,
                json.dumps(states, sort_keys=True),
            )
            if consecutive_failures >= args.failure_threshold:
                logger.error("event=workspace_supervisor category=backend_failure action=fail_workspace")
                _nudge_proxy_to_stop(process)
                return 1
        await _wait_or_stop(stop_requested, args.probe_interval_seconds)


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--named-server-config", required=True)
    parser.add_argument("--startup-timeout-seconds", type=_positive_float, default=DEFAULT_STARTUP_TIMEOUT_SECONDS)
    parser.add_argument("--probe-interval-seconds", type=_positive_float, default=DEFAULT_PROBE_INTERVAL_SECONDS)
    parser.add_argument("--probe-timeout-seconds", type=_positive_float, default=DEFAULT_PROBE_TIMEOUT_SECONDS)
    parser.add_argument("--failure-threshold", type=_positive_int, default=DEFAULT_FAILURE_THRESHOLD)
    parser.add_argument("--stop-grace-seconds", type=_positive_float, default=DEFAULT_STOP_GRACE_SECONDS)
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args()
    return asyncio.run(supervise(args))


if __name__ == "__main__":
    raise SystemExit(main())
