from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "deploy" / "workspace"


def test_workspace_servers_example_is_scoped_and_has_no_fake_cwd():
    data = json.loads((WORKSPACE / "servers.json.example").read_text())
    servers = data["mcpServers"]
    assert set(servers) == {"filesystem", "shell", "file-ingress"}
    assert servers["filesystem"]["args"] == ["/srv/haru-workspace"]
    assert servers["file-ingress"]["command"] == "/opt/haru-workspace/proxy/bin/python"
    assert servers["file-ingress"]["args"] == ["/opt/haru-workspace/file_ingress_server.py"]
    for child in servers.values():
        assert "cwd" not in child
        assert child["env"]["HOME"] == "/var/lib/haru-workspace/home"
        assert child["env"]["TMPDIR"] == "/var/lib/haru-workspace/tmp"
        assert not any("TOKEN" in key or "KEY" in key or "SECRET" in key for key in child["env"])


def test_workspace_unit_represents_isolation_and_containment_boundaries():
    unit = (WORKSPACE / "haru-workspace.service.example").read_text()
    required = (
        "User=haru",
        "Group=haru",
        "WorkingDirectory=/srv/haru-workspace",
        "Environment=HOME=/var/lib/haru-workspace/home",
        "Environment=TMPDIR=/var/lib/haru-workspace/tmp",
        "--host 127.0.0.1",
        "test -r /opt/haru-workspace/file_ingress_server.py",
        "test -r /opt/haru-workspace/workspace-supervisor.py",
        "/opt/haru-workspace/proxy/bin/python /opt/haru-workspace/workspace-supervisor.py",
        "StartLimitIntervalSec=30s",
        "StartLimitBurst=5",
        "Restart=on-failure",
        "KillMode=control-group",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "PrivateDevices=true",
        "ProtectSystem=strict",
        "ProtectHome=tmpfs",
        "BindPaths=/srv/haru-workspace",
        "ReadWritePaths=/srv/haru-workspace /var/lib/haru-workspace",
        "ReadOnlyPaths=/opt/haru-workspace /etc/haru-workspace",
        "CapabilityBoundingSet=",
        "RestrictSUIDSGID=true",
        "MemoryMax=1G",
        "TasksMax=128",
    )
    for value in required:
        assert value in unit
    assert "0.0.0.0" not in unit
    assert "--cwd" not in unit
    assert "--named-server-config /etc/haru-workspace/servers.json" in unit


def test_basic_gateway_example_uses_same_non_sudo_identity():
    unit = (ROOT / "deploy" / "haru-mcp.service.example").read_text()
    assert "User=haru\n" in unit
    assert "Group=haru\n" in unit
