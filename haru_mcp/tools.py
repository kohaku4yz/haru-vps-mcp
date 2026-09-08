"""Gateway health tools plus thin MCP delegation into workspace backends."""
from __future__ import annotations

import asyncio
import builtins
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import anyio
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client
from typing_extensions import NotRequired, TypedDict

from . import __version__
from .settings import SERVICE_NAME, Settings

logger = logging.getLogger(__name__)

_BACKEND_HEALTHY: Final[str] = "healthy"
_BACKEND_TIMEOUT: Final[str] = "backend_timeout"
_BACKEND_UNREACHABLE: Final[str] = "backend_unreachable"
_BACKEND_SESSION_CLOSED: Final[str] = "backend_session_closed"
_BACKEND_PROTOCOL_ERROR: Final[str] = "backend_protocol_error"
_BACKEND_TOOL_ERROR: Final[str] = "backend_tool_error"
_BACKEND_CALL_TIMEOUT_SECONDS: Final[float] = 30.0
_FILE_TRANSFER_CALL_TIMEOUT_SECONDS: Final[float] = 90.0
_BACKEND_HEALTH_TIMEOUT_SECONDS: Final[float] = 3.0
_SHELL_DEFAULT_WAIT_MS: Final[int] = 5000
_SHELL_MAX_WAIT_MS: Final[int] = 25000
_SHELL_MAX_HARD_TIMEOUT_MS: Final[int] = 7 * 24 * 60 * 60 * 1000
_SAFE_TRACEBACK_FRAMES: Final[int] = 12
_BASE_EXCEPTION_GROUP = getattr(builtins, "BaseExceptionGroup", None)


class HealthResult(TypedDict):
    service: str
    version: str
    status: str
    timestamp_utc: str


class OpenAIFileRef(TypedDict):
    download_url: str
    file_id: str
    mime_type: NotRequired[str]
    file_name: NotRequired[str]


class WorkspaceBackendHealthResult(TypedDict):
    status: str
    proxy_reachable: bool
    filesystem: str
    shell: str
    file_ingress: str
    timestamp_utc: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def health() -> HealthResult:
    return {"service": SERVICE_NAME, "version": __version__, "status": "ok", "timestamp_utc": _utc_now_iso()}


def _backend_identity(endpoint: str) -> str:
    try:
        parts = [part for part in urlsplit(endpoint).path.split("/") if part]
    except ValueError:
        return "workspace"
    for index, part in enumerate(parts[:-1]):
        if part == "servers" and index + 1 < len(parts):
            candidate = parts[index + 1]
            if candidate and all(ch.isalnum() or ch in "_-" for ch in candidate):
                return candidate
    return "workspace"


def _traceback_frames(exc: BaseException) -> list[str]:
    frames: list[str] = []

    def collect(current: BaseException) -> None:
        for frame in traceback.extract_tb(current.__traceback__):
            frames.append(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}")
        if _BASE_EXCEPTION_GROUP is not None and isinstance(current, _BASE_EXCEPTION_GROUP):
            for child in current.exceptions:
                collect(child)

    collect(exc)
    return frames[-_SAFE_TRACEBACK_FRAMES:]


def _classify_backend_exception(exc: BaseException) -> str:
    if _BASE_EXCEPTION_GROUP is not None and isinstance(exc, _BASE_EXCEPTION_GROUP):
        categories = {_classify_backend_exception(child) for child in exc.exceptions}
        for preferred in (_BACKEND_TIMEOUT, _BACKEND_UNREACHABLE, _BACKEND_SESSION_CLOSED):
            if preferred in categories:
                return preferred
        return _BACKEND_SESSION_CLOSED
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return _BACKEND_TIMEOUT
    if isinstance(exc, (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)):
        return _BACKEND_SESSION_CLOSED
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, ConnectionError, OSError)):
        return _BACKEND_UNREACHABLE
    if type(exc).__name__ in {"McpError", "SessionError"}:
        return _BACKEND_PROTOCOL_ERROR
    return _BACKEND_PROTOCOL_ERROR


def _backend_failure_text(category: str, backend: str, failure_id: str) -> str:
    detail = {
        _BACKEND_UNREACHABLE: "backend is unreachable",
        _BACKEND_SESSION_CLOSED: "backend session closed or crashed",
        _BACKEND_TIMEOUT: "backend timed out",
        _BACKEND_PROTOCOL_ERROR: "backend protocol error",
        _BACKEND_TOOL_ERROR: "backend tool rejected the request",
    }.get(category, "backend failure")
    return f"Haru workspace backend error [{category}]: {backend} {detail}. ref={failure_id}"


def _backend_failure(category: str, backend: str, failure_id: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_backend_failure_text(category, backend, failure_id))],
        isError=True,
    )


def _log_backend_exception(*, backend: str, tool_name: str, category: str, failure_id: str, exc: BaseException, source: str) -> None:
    logger.error(
        "event=workspace_backend_failure backend=%s tool=%s category=%s failure_id=%s source=%s exception_type=%s traceback=%s",
        backend,
        tool_name,
        category,
        failure_id,
        source,
        type(exc).__name__,
        " > ".join(_traceback_frames(exc)) or "<none>",
    )


async def _probe_backend_tools(endpoint: str, timeout_seconds: float = _BACKEND_HEALTH_TIMEOUT_SECONDS) -> tuple[str, BaseException | None]:
    try:
        with anyio.fail_after(timeout_seconds):
            async with streamablehttp_client(endpoint) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.list_tools()
        return _BACKEND_HEALTHY, None
    except Exception as exc:
        return _classify_backend_exception(exc), exc


def _proxy_status_url(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    return urlunsplit((parts.scheme, parts.netloc, "/status", "", ""))


async def _probe_proxy_reachable(endpoint: str, timeout_seconds: float) -> bool:
    try:
        with anyio.fail_after(timeout_seconds):
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds), follow_redirects=False, trust_env=False) as client:
                response = await client.get(_proxy_status_url(endpoint))
        return 200 <= response.status_code < 300
    except Exception:
        return False


async def workspace_backend_health(settings: Settings) -> WorkspaceBackendHealthResult:
    timeout = _BACKEND_HEALTH_TIMEOUT_SECONDS
    proxy_reachable, filesystem_probe, shell_probe, file_ingress_probe = await asyncio.gather(
        _probe_proxy_reachable(settings.workspace_shell_url, timeout),
        _probe_backend_tools(settings.workspace_filesystem_url, timeout),
        _probe_backend_tools(settings.workspace_shell_url, timeout),
        _probe_backend_tools(settings.workspace_file_ingress_url, timeout),
    )
    filesystem_state = filesystem_probe[0]
    shell_state = shell_probe[0]
    file_ingress_state = file_ingress_probe[0]
    overall = (
        "healthy"
        if proxy_reachable
        and filesystem_state == _BACKEND_HEALTHY
        and shell_state == _BACKEND_HEALTHY
        and file_ingress_state == _BACKEND_HEALTHY
        else "unhealthy"
    )
    return {
        "status": overall,
        "proxy_reachable": proxy_reachable,
        "filesystem": filesystem_state,
        "shell": shell_state,
        "file_ingress": file_ingress_state,
        "timestamp_utc": _utc_now_iso(),
    }


def _shell_input_failure(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], isError=True)


async def delegate_backend_tool(
    endpoint: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout_seconds: float = _BACKEND_CALL_TIMEOUT_SECONDS,
) -> types.CallToolResult:
    backend = _backend_identity(endpoint)
    try:
        with anyio.fail_after(timeout_seconds):
            async with streamablehttp_client(endpoint) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
    except Exception as exc:
        category = _classify_backend_exception(exc)
        failure_id = uuid4().hex[:12]
        _log_backend_exception(
            backend=backend,
            tool_name=tool_name,
            category=category,
            failure_id=failure_id,
            exc=exc,
            source="delegate_exception",
        )
        return _backend_failure(category, backend, failure_id)

    if result.isError:
        health_category, health_exc = await _probe_backend_tools(endpoint)
        if health_category != _BACKEND_HEALTHY:
            failure_id = uuid4().hex[:12]
            if health_exc is not None:
                _log_backend_exception(
                    backend=backend,
                    tool_name=tool_name,
                    category=health_category,
                    failure_id=failure_id,
                    exc=health_exc,
                    source="tool_error_health_probe",
                )
            else:
                logger.error(
                    "event=workspace_backend_failure backend=%s tool=%s category=%s failure_id=%s source=tool_error_health_probe",
                    backend,
                    tool_name,
                    health_category,
                    failure_id,
                )
            return _backend_failure(health_category, backend, failure_id)
        failure_id = uuid4().hex[:12]
        logger.error(
            "event=workspace_backend_failure backend=%s tool=%s category=%s failure_id=%s source=tool_error",
            backend,
            tool_name,
            _BACKEND_TOOL_ERROR,
            failure_id,
        )
        return _backend_failure(_BACKEND_TOOL_ERROR, backend, failure_id)

    return result


async def delegate_backend_resource(
    endpoint: str,
    uri: str,
    *,
    timeout_seconds: float = _FILE_TRANSFER_CALL_TIMEOUT_SECONDS,
) -> types.ReadResourceResult:
    backend = _backend_identity(endpoint)
    try:
        with anyio.fail_after(timeout_seconds):
            async with streamablehttp_client(endpoint) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.read_resource(uri)
    except Exception as exc:
        category = _classify_backend_exception(exc)
        failure_id = uuid4().hex[:12]
        _log_backend_exception(
            backend=backend,
            tool_name="read_resource",
            category=category,
            failure_id=failure_id,
            exc=exc,
            source="delegate_exception",
        )
        raise RuntimeError(_backend_failure_text(category, backend, failure_id)) from None


async def workspace_list_directory(settings: Settings, path: str):
    return await delegate_backend_tool(settings.workspace_filesystem_url, "list_directory", {"path": path})


async def workspace_read_text_file(settings: Settings, path: str):
    return await delegate_backend_tool(settings.workspace_filesystem_url, "read_text_file", {"path": path})


async def workspace_write_file(settings: Settings, path: str, content: str):
    return await delegate_backend_tool(settings.workspace_filesystem_url, "write_file", {"path": path, "content": content})


async def workspace_edit_file(settings: Settings, path: str, edits: list[dict[str, str]]):
    return await delegate_backend_tool(settings.workspace_filesystem_url, "edit_file", {"path": path, "edits": edits})


async def workspace_move_file(settings: Settings, source: str, destination: str):
    return await delegate_backend_tool(settings.workspace_filesystem_url, "move_file", {"source": source, "destination": destination})


async def workspace_get_file_info(settings: Settings, path: str):
    return await delegate_backend_tool(settings.workspace_filesystem_url, "get_file_info", {"path": path})


async def workspace_import_chatgpt_file(
    settings: Settings,
    file: OpenAIFileRef,
    destination: str,
    overwrite: bool = False,
):
    return await delegate_backend_tool(
        settings.workspace_file_ingress_url,
        "import_chatgpt_file",
        {"file": dict(file), "destination": destination, "overwrite": overwrite},
        timeout_seconds=_FILE_TRANSFER_CALL_TIMEOUT_SECONDS,
    )


async def workspace_prepare_file_export(settings: Settings, path: str):
    return await delegate_backend_tool(
        settings.workspace_file_ingress_url,
        "prepare_workspace_file_export",
        {"path": path},
        timeout_seconds=_FILE_TRANSFER_CALL_TIMEOUT_SECONDS,
    )


async def workspace_read_file_export_resource(settings: Settings, token: str):
    return await delegate_backend_resource(
        settings.workspace_file_ingress_url,
        f"haru-workspace-file://export/{token}",
        timeout_seconds=_FILE_TRANSFER_CALL_TIMEOUT_SECONDS,
    )


async def shell_execute(
    settings: Settings,
    command: str,
    wait_ms: int = _SHELL_DEFAULT_WAIT_MS,
    persistent: bool = False,
    hard_timeout_ms: int | None = None,
    timeout_ms: int | None = None,
):
    effective_wait = timeout_ms if timeout_ms is not None else wait_ms
    if not isinstance(effective_wait, int) or isinstance(effective_wait, bool) or effective_wait < 0 or effective_wait > _SHELL_MAX_WAIT_MS:
        return _shell_input_failure(f"wait_ms must be an integer between 0 and {_SHELL_MAX_WAIT_MS}")
    if hard_timeout_ms is not None and (
        not isinstance(hard_timeout_ms, int)
        or isinstance(hard_timeout_ms, bool)
        or hard_timeout_ms < 1
        or hard_timeout_ms > _SHELL_MAX_HARD_TIMEOUT_MS
    ):
        return _shell_input_failure(f"hard_timeout_ms must be an integer between 1 and {_SHELL_MAX_HARD_TIMEOUT_MS}")
    arguments: dict[str, Any] = {"command": command, "wait_ms": effective_wait, "persistent": persistent}
    if hard_timeout_ms is not None:
        arguments["hard_timeout_ms"] = hard_timeout_ms
    return await delegate_backend_tool(settings.workspace_shell_url, "execute", arguments)


async def shell_job_status(settings: Settings, job_id: str):
    return await delegate_backend_tool(settings.workspace_shell_url, "get_job_status", {"jobId": job_id})


async def shell_job_stop(settings: Settings, job_id: str):
    return await delegate_backend_tool(settings.workspace_shell_url, "stop_job", {"jobId": job_id})


__all__ = [
    "HealthResult",
    "OpenAIFileRef",
    "WorkspaceBackendHealthResult",
    "health",
    "workspace_backend_health",
    "delegate_backend_tool",
    "delegate_backend_resource",
    "workspace_list_directory",
    "workspace_read_text_file",
    "workspace_write_file",
    "workspace_edit_file",
    "workspace_move_file",
    "workspace_get_file_info",
    "workspace_import_chatgpt_file",
    "workspace_prepare_file_export",
    "workspace_read_file_export_resource",
    "shell_execute",
    "shell_job_status",
    "shell_job_stop",
]
