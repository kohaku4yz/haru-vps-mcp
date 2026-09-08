from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
GATEWAY_UNIT = DEPLOY / "haru-mcp.service.example"
WORKSPACE_UNIT = DEPLOY / "workspace" / "haru-workspace.service.example"
SERVERS_JSON = DEPLOY / "workspace" / "servers.json.example"


def require_lines(text: str, expected: tuple[str, ...], label: str) -> None:
    lines = set(text.splitlines())
    missing = [line for line in expected if line not in lines]
    if missing:
        raise SystemExit(f"{label}: missing expected lines: {missing}")


def _verify_public_page() -> None:
    page = (ROOT / "docs" / "index.html").read_text()
    required = ("filesystem", "shell", "file transfer", "file-ingress")
    missing = [term for term in required if term not in page]
    if missing:
        raise SystemExit(f"docs/index.html: missing current workspace capabilities: {missing}")


def verify_static() -> None:
    gateway = GATEWAY_UNIT.read_text(encoding="utf-8")
    workspace = WORKSPACE_UNIT.read_text(encoding="utf-8")
    config = json.loads(SERVERS_JSON.read_text(encoding="utf-8"))

    require_lines(
        gateway,
        (
            "User=haru",
            "Group=haru",
            "WorkingDirectory=/opt/haru-mcp",
            "UMask=0077",
            "NoNewPrivileges=true",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            "PrivateTmp=true",
            "PrivateDevices=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "ProtectKernelTunables=true",
            "ProtectKernelModules=true",
            "ProtectControlGroups=true",
            "RestrictSUIDSGID=true",
            "LockPersonality=true",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "IPAddressAllow=127.0.0.0/8",
            "IPAddressAllow=::1/128",
            "IPAddressDeny=any",
            "ReadOnlyPaths=/opt/haru-mcp",
        ),
        "gateway unit",
    )

    require_lines(
        workspace,
        (
            "User=haru",
            "Group=haru",
            "WorkingDirectory=/srv/haru-workspace",
            "KillMode=control-group",
            "NoNewPrivileges=true",
            "PrivateDevices=true",
            "ProtectSystem=strict",
            "ProtectHome=tmpfs",
            "ReadWritePaths=/srv/haru-workspace /var/lib/haru-workspace",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            "RestrictSUIDSGID=true",
            "LockPersonality=true",
            "ExecStartPre=/usr/bin/test -r /opt/haru-workspace/file_ingress_server.py",
            "ExecStartPre=/usr/bin/test -r /opt/haru-workspace/workspace-supervisor.py",
            "StartLimitIntervalSec=30s",
            "StartLimitBurst=5",
        ),
        "workspace unit",
    )

    servers = config.get("mcpServers")
    if set(servers or {}) != {"filesystem", "shell", "file-ingress"}:
        raise SystemExit("servers.json: expected exactly filesystem, shell, and file-ingress")
    if servers["filesystem"].get("args") != ["/srv/haru-workspace"]:
        raise SystemExit("servers.json: filesystem must be scoped to /srv/haru-workspace")
    if servers["shell"].get("args") != []:
        raise SystemExit("servers.json: shell example should not inject extra arguments")
    if servers["file-ingress"].get("command") != "/opt/haru-workspace/proxy/bin/python":
        raise SystemExit("servers.json: file-ingress must use the workspace Python environment")
    if servers["file-ingress"].get("args") != ["/opt/haru-workspace/file_ingress_server.py"]:
        raise SystemExit("servers.json: file-ingress must point at the reviewed child script")
    if any("cwd" in server for server in servers.values()):
        raise SystemExit("servers.json: mcp-proxy 0.12.0 named servers must not claim per-server cwd support")
    if "--cwd" in workspace:
        raise SystemExit("workspace unit: do not use mcp-proxy --cwd for named servers")

    print("public_example_static=PASS")


def normalized_unit(text: str) -> str:
    """Normalize host-specific paths/identity while retaining systemd directives."""
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("User="):
            out.append("User=root")
        elif line.startswith("Group="):
            out.append("Group=root")
        elif line.startswith("EnvironmentFile="):
            out.append("EnvironmentFile=-/dev/null")
        elif line.startswith("ExecStartPre="):
            out.append("ExecStartPre=/bin/true")
        elif line.startswith("ExecStart="):
            out.append("ExecStart=/bin/true")
        elif line.startswith("WorkingDirectory="):
            out.append("WorkingDirectory=/tmp")
        elif line.startswith("ConditionPathIsDirectory="):
            out.append("ConditionPathIsDirectory=/tmp")
        elif line.startswith("BindPaths="):
            out.append("BindPaths=/tmp")
        elif line.startswith("ReadWritePaths="):
            out.append("ReadWritePaths=/tmp")
        elif line.startswith("ReadOnlyPaths="):
            out.append("ReadOnlyPaths=/tmp")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def verify_systemd_if_available() -> None:
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        print("public_example_systemd=SKIP (systemd-analyze not available)")
        return

    with tempfile.TemporaryDirectory(prefix="haru-systemd-verify-") as tmp:
        tmp_path = Path(tmp)
        unit_paths = []
        for name, source in (
            ("haru-mcp-example.service", GATEWAY_UNIT),
            ("haru-workspace-example.service", WORKSPACE_UNIT),
        ):
            target = tmp_path / name
            target.write_text(normalized_unit(source.read_text(encoding="utf-8")), encoding="utf-8")
            unit_paths.append(str(target))

        result = subprocess.run(
            [analyzer, "verify", *unit_paths],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if result.returncode:
            raise SystemExit(f"systemd-analyze verify failed:\n{result.stdout}")

    print("public_example_systemd=PASS")


if __name__ == "__main__":
    _verify_public_page()
    verify_static()
    verify_systemd_if_available()
