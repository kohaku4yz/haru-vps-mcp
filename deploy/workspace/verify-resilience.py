#!/usr/bin/env python3
"""Disposable real-backend resilience verification for the public workspace example."""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcp import types

import haru_mcp.tools as gateway_tools
from haru_mcp.settings import load_settings
from haru_mcp.tools import (
    shell_execute,
    shell_job_status,
    shell_job_stop,
    workspace_backend_health,
    workspace_prepare_file_export,
    workspace_read_text_file,
    workspace_write_file,
)

ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = ROOT / "deploy" / "workspace" / "workspace-supervisor.py"
FILE_TRANSFER = ROOT / "deploy" / "workspace" / "file_ingress_server.py"


def structured_result(result: types.CallToolResult) -> dict[str, object]:
    payload = result.structuredContent
    if not isinstance(payload, dict):
        raise AssertionError(f"expected structured tool result, got {result!r}")
    return dict(payload)


def text_content(result: types.CallToolResult) -> str:
    return " ".join(item.text for item in result.content if isinstance(item, types.TextContent))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def descendants(pid: int) -> set[int]:
    seen: set[int] = set()
    todo = [pid]
    while todo:
        parent = todo.pop()
        try:
            raw = Path(f"/proc/{parent}/task/{parent}/children").read_text().strip()
        except OSError:
            continue
        for item in raw.split():
            child = int(item)
            if child not in seen:
                seen.add(child)
                todo.append(child)
    return seen


def cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def find_backend(supervisor_pid: int, needle: str, label: str) -> int:
    matches = [(pid, cmdline(pid)) for pid in descendants(supervisor_pid)]
    found = [pid for pid, line in matches if needle in line]
    if len(found) != 1:
        raise RuntimeError(
            f"expected one disposable {label} backend, found {[(pid, line[:160]) for pid, line in matches]}"
        )
    return found[0]


def make_config(
    path: Path,
    *,
    workspace: Path,
    filesystem: Path,
    shell: Path,
    python: Path,
    home: Path,
    tmpdir: Path,
) -> None:
    child_path = os.pathsep.join([str(shell.parent), os.environ.get("PATH", "/usr/bin:/bin")])
    common_env = {
        "HOME": str(home),
        "PATH": child_path,
        "TMPDIR": str(tmpdir),
    }
    payload = {
        "mcpServers": {
            "filesystem": {
                "command": str(filesystem),
                "args": [str(workspace)],
                "env": common_env,
            },
            "shell": {
                "command": str(shell),
                "args": [],
                "env": common_env,
            },
            "file-ingress": {
                "command": str(python),
                "args": [str(FILE_TRANSFER)],
                "env": {**common_env, "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            },
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def supervisor_command(proxy: Path, config: Path, port: int) -> list[str]:
    return [
        sys.executable,
        str(SUPERVISOR),
        "--proxy",
        str(proxy),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--named-server-config",
        str(config),
        "--startup-timeout-seconds",
        "12",
        "--probe-interval-seconds",
        "0.35",
        "--probe-timeout-seconds",
        "0.5",
        "--failure-threshold",
        "5",
        "--stop-grace-seconds",
        "2",
    ]


def _log_path(process: subprocess.Popen[str]) -> Path:
    return getattr(process, "_haru_log_path")


def _close_log(process: subprocess.Popen[str]) -> None:
    handle = getattr(process, "_haru_log_handle", None)
    if handle is not None and not handle.closed:
        handle.close()


def _peek_log(process: subprocess.Popen[str]) -> str:
    handle = getattr(process, "_haru_log_handle", None)
    if handle is not None and not handle.closed:
        handle.flush()
    try:
        return _log_path(process).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_log(process: subprocess.Popen[str]) -> str:
    _close_log(process)
    try:
        return _log_path(process).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def start_supervisor(workspace: Path, proxy: Path, config: Path, port: int, runtime: Path) -> subprocess.Popen[str]:
    home = runtime / "home"
    tmpdir = runtime / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmpdir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "TMPDIR": str(tmpdir),
        "PYTHONNOUSERSITE": "1",
        "LANG": "C.UTF-8",
    }
    log_path = runtime / f"workspace-supervisor-{port}-{time.monotonic_ns()}.log"
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        supervisor_command(proxy, config, port),
        cwd=workspace,
        env=env,
        text=True,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    setattr(process, "_haru_log_path", log_path)
    setattr(process, "_haru_log_handle", log_handle)
    return process


def settings_for(port: int):
    return load_settings(
        env={
            "HARU_MCP_WORKSPACE_FILESYSTEM_URL": f"http://127.0.0.1:{port}/servers/filesystem/mcp",
            "HARU_MCP_WORKSPACE_SHELL_URL": f"http://127.0.0.1:{port}/servers/shell/mcp",
            "HARU_MCP_WORKSPACE_FILE_INGRESS_URL": f"http://127.0.0.1:{port}/servers/file-ingress/mcp",
        }
    )


async def wait_health(cfg, process: subprocess.Popen[str], timeout: float = 12.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"supervisor exited before healthy rc={process.returncode}\n{_read_log(process)}")
        last = await workspace_backend_health(cfg)
        if last["status"] == "healthy" and "category=healthy phase=startup" in _peek_log(process):
            return last
        await asyncio.sleep(0.05)
    raise TimeoutError(f"workspace never became healthy: {last}\n{_peek_log(process)[-12000:]}")


async def wait_half_dead(cfg, process: subprocess.Popen[str], failed_key: str, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    last = None
    keys = {"filesystem", "shell", "file_ingress"}
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"supervisor exited before half-dead {failed_key} state was observable")
        last = await workspace_backend_health(cfg)
        healthy_others = all(last[key] == "healthy" for key in keys - {failed_key})
        if last["proxy_reachable"] and healthy_others and last[failed_key] != "healthy":
            return last
        await asyncio.sleep(0.02)
    raise TimeoutError(f"half-dead health state not observed for {failed_key}: {last}")


def wait_exit(process: subprocess.Popen[str], timeout: float = 8.0) -> tuple[int, str]:
    try:
        rc = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        log = _read_log(process)
        raise RuntimeError(f"supervisor did not exit within {timeout}s\n{log[-12000:]}") from exc
    return rc, _read_log(process)


def assert_fail_closed_monitor_exit(log: str) -> None:
    threshold_failure = "category=backend_failure action=fail_workspace" in log
    proxy_loss = "category=proxy_exited phase=monitor" in log
    assert threshold_failure or proxy_loss, f"unexpected supervisor exit reason:\n{log[-12000:]}"
    assert "category=proxy_exited phase=startup" not in log
    assert "category=startup_unhealthy action=fail_workspace" not in log
    assert "category=stopping reason=signal" not in log


def cleanup(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        try:
            os.kill(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
    _close_log(process)


async def verify(args: argparse.Namespace) -> None:
    workspace = args.workspace.resolve()
    proxy = args.proxy.resolve()
    filesystem = args.filesystem.resolve()
    shell = args.shell.resolve()
    for path, label in ((workspace, "workspace"), (proxy, "proxy"), (filesystem, "filesystem"), (shell, "shell")):
        if not path.exists():
            raise FileNotFoundError(f"{label} path does not exist: {path}")

    with tempfile.TemporaryDirectory(prefix="haru-public-resilience-") as raw:
        runtime = Path(raw)
        (runtime / "home").mkdir()
        (runtime / "tmp").mkdir()
        config = runtime / "servers.json"
        make_config(
            config,
            workspace=workspace,
            filesystem=filesystem,
            shell=shell,
            python=Path(sys.executable),
            home=runtime / "home",
            tmpdir=runtime / "tmp",
        )

        port = free_port()
        cfg = settings_for(port)
        first: subprocess.Popen[str] | None = start_supervisor(workspace, proxy, config, port, runtime)
        second: subprocess.Popen[str] | None = None
        third: subprocess.Popen[str] | None = None
        marker = workspace / "resilience-marker.txt"
        try:
            healthy = await wait_health(cfg, first)
            assert healthy["proxy_reachable"] is True
            assert healthy["filesystem"] == "healthy"
            assert healthy["shell"] == "healthy"
            assert healthy["file_ingress"] == "healthy"

            wrote = await workspace_write_file(cfg, str(marker), "survives workspace restart\n")
            assert wrote.isError is not True
            quick = structured_result(await shell_execute(cfg, "printf pre-crash"))
            assert quick["exitCode"] == 0 and quick["stdout"] == "pre-crash"

            shell_pid = find_backend(first.pid, "shell-exec-mcp", "shell")
            os.kill(shell_pid, signal.SIGKILL)
            shell_half_dead = await wait_half_dead(cfg, first, "shell")
            assert shell_half_dead["filesystem"] == "healthy"
            assert shell_half_dead["file_ingress"] == "healthy"

            fs_during = await workspace_read_text_file(cfg, str(marker))
            assert fs_during.isError is not True

            log_buffer = io.StringIO()
            handler = logging.StreamHandler(log_buffer)
            old_level = gateway_tools.logger.level
            gateway_tools.logger.addHandler(handler)
            gateway_tools.logger.setLevel(logging.ERROR)
            try:
                failed = await shell_execute(cfg, "printf public-resilience-secret-command", wait_ms=10)
            finally:
                gateway_tools.logger.removeHandler(handler)
                gateway_tools.logger.setLevel(old_level)
            client_failure = text_content(failed)
            assert failed.isError is True
            assert "public-resilience-secret-command" not in client_failure
            assert "public-resilience-secret-command" not in log_buffer.getvalue()
            assert "ref=" in client_failure

            rc, log = wait_exit(first)
            assert rc != 0
            assert_fail_closed_monitor_exit(log)
            first = None

            second = start_supervisor(workspace, proxy, config, port, runtime)
            await wait_health(cfg, second)
            read_after_restart = await workspace_read_text_file(cfg, str(marker))
            assert read_after_restart.isError is not True

            ingress_pid = find_backend(second.pid, "file_ingress_server.py", "file-ingress")
            os.kill(ingress_pid, signal.SIGKILL)
            ingress_half_dead = await wait_half_dead(cfg, second, "file_ingress")
            assert ingress_half_dead["filesystem"] == "healthy"
            assert ingress_half_dead["shell"] == "healthy"

            secret_path = "private/public-resilience-secret-export.bin"
            log_buffer = io.StringIO()
            handler = logging.StreamHandler(log_buffer)
            old_level = gateway_tools.logger.level
            gateway_tools.logger.addHandler(handler)
            gateway_tools.logger.setLevel(logging.ERROR)
            try:
                failed_export = await workspace_prepare_file_export(cfg, secret_path)
            finally:
                gateway_tools.logger.removeHandler(handler)
                gateway_tools.logger.setLevel(old_level)
            export_failure = text_content(failed_export)
            assert failed_export.isError is True
            assert secret_path not in export_failure
            assert secret_path not in log_buffer.getvalue()
            assert "ref=" in export_failure

            rc, log = wait_exit(second)
            assert rc != 0
            assert_fail_closed_monitor_exit(log)
            second = None

            third = start_supervisor(workspace, proxy, config, port, runtime)
            recovered = await wait_health(cfg, third)
            assert recovered["status"] == "healthy"

            post = structured_result(await shell_execute(cfg, "printf post-recovery"))
            assert post["exitCode"] == 0 and post["stdout"] == "post-recovery"

            managed = structured_result(
                await shell_execute(
                    cfg,
                    "python3 -c 'import time; time.sleep(0.25); print(\"managed-after-recovery\")'",
                    wait_ms=10,
                )
            )
            assert managed["running"] is True
            await asyncio.sleep(0.4)
            completed = structured_result(await shell_job_status(cfg, str(managed["jobId"])))
            assert completed["running"] is False and completed["exitCode"] == 0
            assert "managed-after-recovery" in str(completed["stdout"])

            stoppable = structured_result(await shell_execute(cfg, "sleep 10", wait_ms=10))
            assert stoppable["running"] is True
            stopped = structured_result(await shell_job_stop(cfg, str(stoppable["jobId"])))
            assert stopped["running"] is False and stopped["state"] == "stopped"

            os.kill(third.pid, signal.SIGTERM)
            rc, _log = wait_exit(third)
            assert rc == 0
            third = None
        finally:
            cleanup(first)
            cleanup(second)
            cleanup(third)
            marker.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--proxy", type=Path, required=True)
    parser.add_argument("--filesystem", type=Path, required=True)
    parser.add_argument("--shell", type=Path, required=True)
    return parser


def main() -> int:
    asyncio.run(verify(build_parser().parse_args()))
    print("PUBLIC_WORKSPACE_RESILIENCE_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
