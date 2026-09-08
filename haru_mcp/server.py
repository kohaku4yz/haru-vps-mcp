"""FastMCP Streamable HTTP gateway with loopback-first transport security."""
from __future__ import annotations

import base64
import binascii
import logging
import sys
from importlib.resources import files

from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .settings import SERVICE_NAME, Settings, load_settings
from .tools import (
    HealthResult,
    OpenAIFileRef,
    WorkspaceBackendHealthResult,
    health,
    shell_execute,
    shell_job_status,
    shell_job_stop,
    workspace_backend_health,
    workspace_edit_file,
    workspace_get_file_info,
    workspace_import_chatgpt_file,
    workspace_list_directory,
    workspace_move_file,
    workspace_prepare_file_export,
    workspace_read_file_export_resource,
    workspace_read_text_file,
    workspace_write_file,
)

logger = logging.getLogger(__name__)
INSTRUCTIONS_RESOURCE = "HARU-INSTRUCTIONS.md"
_EXPORT_RESOURCE_TEMPLATE = "haru-workspace://export/{token}"
_MAX_EXPORT_TOKEN_CHARS = 8192
_MAX_EXPORT_FILE_BYTES = 100 * 1024 * 1024


def _load_instructions() -> str:
    text = files("haru_mcp").joinpath(INSTRUCTIONS_RESOURCE).read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError("Haru MCP instructions resource is empty")
    return text


def _transport_security(cfg: Settings) -> TransportSecuritySettings:
    allowed_hosts = [f"127.0.0.1:{cfg.port}", f"localhost:{cfg.port}", f"[::1]:{cfg.port}"]
    allowed_origins: list[str] = []
    if cfg.public_host and cfg.public_origin:
        allowed_hosts.extend([cfg.public_host, f"{cfg.public_host}:443"])
        allowed_origins.append(cfg.public_origin)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _structured_payload(result: types.CallToolResult) -> dict[str, object]:
    payload = result.structuredContent
    if not isinstance(payload, dict):
        raise RuntimeError("workspace file-transfer backend returned no structured payload")
    return payload


def _encode_export_path(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_export_token(token: str) -> str:
    if not isinstance(token, str) or not token or len(token) > _MAX_EXPORT_TOKEN_CHARS:
        raise ValueError("invalid workspace export resource token")
    padding = "=" * (-len(token) % 4)
    try:
        raw = base64.b64decode(token + padding, altchars=b"-_", validate=True)
        path = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise ValueError("invalid workspace export resource token") from None
    if not path:
        raise ValueError("invalid workspace export resource token")
    return path


def build_server(settings: Settings | None = None) -> FastMCP:
    cfg = settings if settings is not None else load_settings()
    server = FastMCP(
        name=SERVICE_NAME,
        instructions=_load_instructions(),
        host=cfg.host,
        port=cfg.port,
        streamable_http_path=cfg.path,
        stateless_http=True,
        json_response=True,
        log_level="WARNING",
        transport_security=_transport_security(cfg),
    )

    @server.resource(
        _EXPORT_RESOURCE_TEMPLATE,
        name="workspace-export",
        description="Bounded workspace file exported through MCP resource content.",
        mime_type="application/octet-stream",
    )
    async def workspace_export_resource(token: str) -> bytes:
        _decode_export_token(token)
        result = await workspace_read_file_export_resource(cfg, token)
        if len(result.contents) != 1 or not isinstance(result.contents[0], types.BlobResourceContents):
            raise RuntimeError("workspace file-transfer backend returned invalid resource content")
        encoded = result.contents[0].blob
        try:
            data = base64.b64decode(encoded, validate=True)
        except binascii.Error:
            raise RuntimeError("workspace file-transfer backend returned invalid base64 content") from None
        if len(data) > _MAX_EXPORT_FILE_BYTES:
            raise RuntimeError("workspace file-transfer backend exceeded export size limit")
        return data

    @server.tool(name="health", description="Return a static gateway health snapshot. Takes no arguments.")
    def health_tool() -> HealthResult:
        return health()

    @server.tool(
        name="workspace_backend_health",
        description="Probe the configured filesystem, shell, and file-transfer backends so a live proxy with a dead named backend is reported unhealthy.",
    )
    async def workspace_backend_health_tool() -> WorkspaceBackendHealthResult:
        return await workspace_backend_health(cfg)

    @server.tool(name="workspace_list_directory")
    async def list_directory_tool(path: str) -> types.CallToolResult:
        return await workspace_list_directory(cfg, path)

    @server.tool(name="workspace_read_text_file")
    async def read_text_file_tool(path: str) -> types.CallToolResult:
        return await workspace_read_text_file(cfg, path)

    @server.tool(name="workspace_write_file")
    async def write_file_tool(path: str, content: str) -> types.CallToolResult:
        return await workspace_write_file(cfg, path, content)

    @server.tool(name="workspace_edit_file")
    async def edit_file_tool(path: str, edits: list[dict[str, str]]) -> types.CallToolResult:
        return await workspace_edit_file(cfg, path, edits)

    @server.tool(name="workspace_move_file")
    async def move_file_tool(source: str, destination: str) -> types.CallToolResult:
        return await workspace_move_file(cfg, source, destination)

    @server.tool(name="workspace_get_file_info")
    async def get_file_info_tool(path: str) -> types.CallToolResult:
        return await workspace_get_file_info(cfg, path)

    @server.tool(
        name="workspace_import_chatgpt_file",
        description=(
            "Import one file attached to or generated in the current ChatGPT conversation "
            "into the Haru workspace. Pass a relative destination path whose parent "
            "directory already exists. The ChatGPT host supplies the file reference "
            "automatically; do not construct download URLs manually."
        ),
        meta={"openai/fileParams": ["file"]},
    )
    async def import_chatgpt_file_tool(
        file: OpenAIFileRef,
        destination: str,
        overwrite: bool = False,
    ) -> types.CallToolResult:
        return await workspace_import_chatgpt_file(cfg, file, destination, overwrite)

    @server.tool(
        name="workspace_export_file",
        description=(
            "Export one regular file from the Haru workspace to the MCP client. "
            "Pass a path relative to the workspace root."
        ),
        structured_output=False,
    )
    async def export_file_tool(path: str) -> types.CallToolResult:
        prepared = await workspace_prepare_file_export(cfg, path)
        if prepared.isError:
            return prepared
        payload = _structured_payload(prepared)
        export_path = payload.get("path")
        name = payload.get("name")
        mime_type = payload.get("mime_type")
        size = payload.get("bytes")
        if (
            not isinstance(export_path, str)
            or not isinstance(name, str)
            or not isinstance(mime_type, str)
            or not isinstance(size, int)
            or size < 0
            or size > _MAX_EXPORT_FILE_BYTES
        ):
            raise RuntimeError("workspace file-transfer backend returned invalid export metadata")
        token = _encode_export_path(export_path)
        return types.CallToolResult(
            content=[
                types.ResourceLink(
                    type="resource_link",
                    uri=f"haru-workspace://export/{token}",
                    name=name,
                    description="Haru workspace file export.",
                    mimeType=mime_type,
                    size=size,
                )
            ],
            isError=False,
        )

    @server.tool(
        name="shell_execute",
        description="Run shell work as a managed job. wait_ms controls only synchronous waiting; if work is still running, a job ID is returned instead of killing it. timeout_ms is a compatibility alias for wait_ms.",
    )
    async def shell_execute_tool(
        command: str,
        wait_ms: int = 5000,
        persistent: bool = False,
        hard_timeout_ms: int | None = None,
        timeout_ms: int | None = None,
    ) -> types.CallToolResult:
        return await shell_execute(cfg, command, wait_ms, persistent, hard_timeout_ms, timeout_ms)

    @server.tool(name="shell_job_status", description="Inspect a managed shell job. Status reads refresh the observation lease for running jobs.")
    async def shell_job_status_tool(job_id: str) -> types.CallToolResult:
        return await shell_job_status(cfg, job_id)

    @server.tool(name="shell_job_stop", description="Explicitly stop a managed shell job and its owned process group.")
    async def shell_job_stop_tool(job_id: str) -> types.CallToolResult:
        return await shell_job_stop(cfg, job_id)

    return server


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_settings()
    logger.info("event=startup category=starting")
    build_server(cfg).run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    sys.exit(main())
