from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import types

import haru_mcp.tools as tools
from haru_mcp.settings import load_settings

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "deploy" / "workspace"
SUPERVISOR_PATH = WORKSPACE / "workspace-supervisor.py"
SPEC = importlib.util.spec_from_file_location("haru_workspace_supervisor", SUPERVISOR_PATH)
assert SPEC is not None and SPEC.loader is not None
supervisor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = supervisor
SPEC.loader.exec_module(supervisor)


def text_content(result: types.CallToolResult) -> str:
    return " ".join(item.text for item in result.content if isinstance(item, types.TextContent))


def test_supervisor_discovers_named_backends_and_rejects_log_unsafe_names(tmp_path: Path) -> None:
    config = tmp_path / "servers.json"
    config.write_text(json.dumps({"mcpServers": {"filesystem": {}, "shell": {}, "file-ingress": {}}}))
    assert supervisor.load_backend_names(config) == ["filesystem", "shell", "file-ingress"]

    config.write_text(json.dumps({"mcpServers": {"bad\nname": {}}}))
    with pytest.raises(ValueError, match="invalid server name"):
        supervisor.load_backend_names(config)


@pytest.mark.asyncio
async def test_supervisor_fails_closed_after_consecutive_backend_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "servers.json"
    config.write_text(json.dumps({"mcpServers": {"filesystem": {}, "shell": {}, "file-ingress": {}}}))
    captured: dict[str, object] = {}

    class FakeProcess:
        returncode = None
        pid = 424242
        terminated = False

        def terminate(self) -> None:
            self.terminated = True

    process = FakeProcess()

    async def fake_spawn(*args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        return process

    states = iter(
        [
            {"filesystem": "healthy", "shell": "healthy", "file-ingress": "healthy"},
            {"filesystem": "healthy", "shell": "session_closed", "file-ingress": "healthy"},
            {"filesystem": "healthy", "shell": "session_closed", "file-ingress": "healthy"},
        ]
    )

    async def fake_probe_all(*_args, **_kwargs):
        return next(states)

    async def no_wait(*_args, **_kwargs):
        return None

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supervisor.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(supervisor, "probe_all", fake_probe_all)
    monkeypatch.setattr(supervisor, "_wait_or_stop", no_wait)

    args = supervisor.build_parser().parse_args(
        [
            "--proxy",
            "/opt/example/mcp-proxy",
            "--port",
            "8766",
            "--named-server-config",
            str(config),
            "--failure-threshold",
            "2",
        ]
    )
    rc = await supervisor.supervise(args)
    assert rc == 1
    assert process.terminated is True
    assert captured["cwd"] == str(tmp_path)
    assert "--cwd" not in captured["args"]
    assert captured["args"] == (
        "/opt/example/mcp-proxy",
        "--host",
        "127.0.0.1",
        "--port",
        "8766",
        "--named-server-config",
        str(config.resolve()),
    )


@pytest.mark.asyncio
async def test_workspace_health_rejects_half_dead_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_settings(env={})

    async def proxy(_endpoint: str, _timeout: float) -> bool:
        return True

    async def backend(endpoint: str, _timeout: float = 3.0):
        if "/shell/" in endpoint:
            return tools._BACKEND_SESSION_CLOSED, RuntimeError("closed")
        return tools._BACKEND_HEALTHY, None

    monkeypatch.setattr(tools, "_probe_proxy_reachable", proxy)
    monkeypatch.setattr(tools, "_probe_backend_tools", backend)
    result = await tools.workspace_backend_health(cfg)
    assert result["proxy_reachable"] is True
    assert result["filesystem"] == "healthy"
    assert result["shell"] == "backend_session_closed"
    assert result["file_ingress"] == "healthy"
    assert result["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_backend_exception_does_not_leak_arguments_or_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_command = "printf super-secret-command /private/path"
    secret_url = "https://files.oaiusercontent.com/x?token=super-secret-token"

    class BrokenTransport:
        async def __aenter__(self):
            raise OSError(f"transport exploded near {secret_url}")

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(tools, "streamablehttp_client", lambda _endpoint: BrokenTransport())
    caplog.set_level(logging.ERROR, logger="haru_mcp.tools")
    result = await tools.delegate_backend_tool(
        "http://127.0.0.1:8766/servers/shell/mcp",
        "execute",
        {"command": secret_command, "url": secret_url},
        timeout_seconds=0.1,
    )
    client = text_content(result)
    assert result.isError is True
    assert "[backend_unreachable]" in client
    assert "ref=" in client
    assert "Traceback" not in client and "traceback=" not in client
    for secret in (secret_command, secret_url, "super-secret-token", "/private/path"):
        assert secret not in client
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_healthy_backend_tool_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "caller-secret-command /private/caller/path"

    class Transport:
        async def __aenter__(self):
            return object(), object(), None

        async def __aexit__(self, *_args):
            return False

    class Session:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return types.ListToolsResult(tools=[])

        async def call_tool(self, _tool_name, _arguments):
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"backend echoed {secret}")],
                isError=True,
            )

    monkeypatch.setattr(tools, "streamablehttp_client", lambda _endpoint: Transport())
    monkeypatch.setattr(tools, "ClientSession", Session)
    caplog.set_level(logging.ERROR, logger="haru_mcp.tools")
    result = await tools.delegate_backend_tool(
        "http://127.0.0.1:8766/servers/shell/mcp",
        "execute",
        {"command": secret},
    )
    client = text_content(result)
    assert result.isError is True
    assert "[backend_tool_error]" in client
    assert secret not in client
    assert secret not in caplog.text
    assert "backend echoed" not in caplog.text


def test_managed_shell_patch_is_version_guarded_and_installs(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    repo_workspace = tmp_path / "workspace-files"
    patch_dir = repo_workspace / "shell-exec-mcp-patch"
    patch_dir.mkdir(parents=True)
    shutil.copy2(WORKSPACE / "install-managed-shell-patch.sh", repo_workspace / "install-managed-shell-patch.sh")
    shutil.copy2(WORKSPACE / "shell-exec-mcp-patch" / "bash.mjs", patch_dir / "bash.mjs")
    shutil.copy2(WORKSPACE / "shell-exec-mcp-patch" / "managed-jobs.mjs", patch_dir / "managed-jobs.mjs")

    node_root = tmp_path / "node"
    package = node_root / "node_modules" / "shell-exec-mcp"
    (package / "dist" / "tools").mkdir(parents=True)
    (package / "dist" / "tools" / "bash.js").write_text("old")
    (package / "package.json").write_text(json.dumps({"version": "1.2.0"}))
    env = {**os.environ, "NODE_ROOT": str(node_root), "NODE_BIN": node}

    subprocess.run(
        ["bash", str(repo_workspace / "install-managed-shell-patch.sh")],
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert (package / "dist" / "tools" / "bash.js").read_text() == (patch_dir / "bash.mjs").read_text()
    assert (package / "dist" / "tools" / "managed-jobs.js").read_text() == (patch_dir / "managed-jobs.mjs").read_text()

    (package / "package.json").write_text(json.dumps({"version": "9.9.9"}))
    rejected = subprocess.run(
        ["bash", str(repo_workspace / "install-managed-shell-patch.sh")],
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert rejected.returncode != 0
    assert "version mismatch" in rejected.stderr


def test_managed_shell_node_lifecycle_suite() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    completed = subprocess.run(
        [node, "--test", str(WORKSPACE / "shell-exec-mcp-patch" / "managed-jobs.test.mjs")],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout
