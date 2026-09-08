# Workspace backends

Haru VPS MCP is a gateway facade. Its filesystem, shell, and file-transfer tools delegate to **separate MCP servers on loopback**. This guide shows a public-safe reference composition using the upstream components recorded in [`THIRD-PARTY.md`](../THIRD-PARTY.md).

The intended boundary is:

```text
Haru MCP gateway
  127.0.0.1:8765
       |
       +--> http://127.0.0.1:8766/servers/filesystem/mcp
       |       -> filesystem MCP stdio process
       |       -> dedicated workspace root only
       |
       +--> http://127.0.0.1:8766/servers/shell/mcp
       |       -> shell MCP stdio process
       |       -> same disposable workspace
       |
       +--> http://127.0.0.1:8766/servers/file-ingress/mcp
               -> Haru bounded file-transfer stdio child
               -> bounded import/export in the same workspace
```

There is **no second public hostname** for this backend layer. Keep the listener on loopback and let only the Haru gateway talk to it.

## Security assumptions

The shell server is a privileged development capability: it can execute commands with the permissions of its service account. Treat the whole backend as a capability boundary, not as a general-purpose login shell.

Use a dedicated unprivileged service identity and a dedicated/disposable workspace. For the basic single-host example, the same non-sudo `haru` account can run the Haru gateway and workspace backend; splitting those two services into separate identities is optional defense in depth. Do not place production credentials, SSH agents, host administration files, container-engine sockets, cloud credentials, or unrelated application data inside that workspace or its service environment.

The filesystem server must be started with the workspace root as its allowed root. The shell server and file-transfer child use the same directory as their working directory. Systemd hardening is useful defense in depth, but it does not make arbitrary shell execution safe against secrets that the service account can already read.

The file-transfer child is intentionally narrow. For import, it accepts the ChatGPT host file-reference shape, requires HTTPS on approved OpenAI storage host patterns, rejects credentials/fragments/non-public DNS answers, pins the connection to a validated public address while preserving TLS hostname verification, follows at most three revalidated redirects, caps each file at 100 MiB, and writes atomically beneath the workspace root. For export, it accepts only workspace-relative regular files that resolve beneath the same root, caps each file at 100 MiB, and returns bytes through MCP resource content. The gateway exposes those bytes through a `ResourceLink`; it does not create a second public file URL. Do not broaden the child into arbitrary URL fetching or host filesystem reads.

## Reference versions

The reproducible reference point used by this guide is:

```text
mcp-proxy==0.12.0
mcp==1.27.1                 # compatibility pin for this proxy composition
@modelcontextprotocol/server-filesystem@2026.7.10
shell-exec-mcp@1.2.0       # base package; Haru applies the reviewed managed-job patch below
```

The exact upstream source commits and license notes are in [`THIRD-PARTY.md`](../THIRD-PARTY.md). Re-check upstream release notes and licenses before changing these pins.

## Example filesystem layout

The paths below are examples, not required Haru paths:

```text
/srv/haru-workspace/              disposable workspace data
/opt/haru-workspace/proxy/        Python virtual environment for mcp-proxy
/opt/haru-workspace/node/         Node package installation
/opt/haru-workspace/file_ingress_server.py  Haru bounded file-transfer child
/opt/haru-workspace/workspace-supervisor.py  backend-aware composition supervisor
/etc/haru-workspace/servers.json  non-secret named-server configuration
/var/lib/haru-workspace/home/     service HOME
/var/lib/haru-workspace/tmp/      service temporary directory
```

Create them so the workspace service user can write only where it needs to write. Keep `/opt` and `/etc` installation/configuration material owner-controlled and non-writable by the runtime account.

## Install the selected upstream packages

One straightforward layout is:

```bash
python3 -m venv /opt/haru-workspace/proxy
/opt/haru-workspace/proxy/bin/pip install \
  'mcp-proxy==0.12.0' \
  'mcp==1.27.1'

mkdir -p /opt/haru-workspace/node
cd /opt/haru-workspace/node
npm init -y
npm install --omit=dev \
  '@modelcontextprotocol/server-filesystem@2026.7.10' \
  'shell-exec-mcp@1.2.0'
```

For a stricter deployment, build/package these dependencies in a separate staging environment and install verified artifacts into an owner-controlled prefix. Do not treat mutable global `pip` or `npm` state as a deployment record.

The selected shell package is the base for Haru's managed-job behavior. After installing exactly `shell-exec-mcp@1.2.0`, apply the repository-owned, version-guarded patch:

From a reviewed Haru VPS MCP checkout:

```bash
cd /path/to/haru-vps-mcp
NODE_ROOT=/opt/haru-workspace/node \
  sh deploy/workspace/install-managed-shell-patch.sh
```

The installer refuses a different package version. The patch keeps `wait_ms` as a response wait budget: a command that outlives that wait stays tracked and returns a job ID. Output is bounded while pipes keep draining; status/stop act on the owned process group; non-persistent jobs are reaped only after the idle lease and lack of output/observation/CPU/I/O progress establish conservative idleness. `persistent=true` exempts intentional long-running work from idle reaping, but not from output/registry bounds or explicit stop. A descendant that deliberately starts a new session/process group is outside this best-effort ownership boundary.

Install reviewed copies of [`deploy/workspace/file_ingress_server.py`](../deploy/workspace/file_ingress_server.py) as `/opt/haru-workspace/file_ingress_server.py` and [`deploy/workspace/workspace-supervisor.py`](../deploy/workspace/workspace-supervisor.py) as `/opt/haru-workspace/workspace-supervisor.py`, owned by the operator and not writable by the runtime service account. The legacy file-ingress filename/backend name is kept for deployment compatibility; the child provides both bounded import and export. For example, while still in the reviewed checkout:

```bash
install -m 0644 deploy/workspace/file_ingress_server.py /opt/haru-workspace/file_ingress_server.py
install -m 0644 deploy/workspace/workspace-supervisor.py /opt/haru-workspace/workspace-supervisor.py
```

## Configure named stdio servers

`mcp-proxy` 0.12.0 supports named stdio servers and mounts each one under `/servers/<name>/`; its Streamable HTTP endpoint for each instance is `/mcp`. The repository also includes the same non-secret composition as a copyable file at [`deploy/workspace/servers.json.example`](../deploy/workspace/servers.json.example); install a reviewed copy as `/etc/haru-workspace/servers.json`. A minimal configuration looks like:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "/opt/haru-workspace/node/node_modules/.bin/mcp-server-filesystem",
      "args": ["/srv/haru-workspace"],
      "env": {
        "HOME": "/var/lib/haru-workspace/home",
        "PATH": "/opt/haru-workspace/node/node_modules/.bin:/usr/bin:/bin",
        "TMPDIR": "/var/lib/haru-workspace/tmp"
      }
    },
    "shell": {
      "command": "/opt/haru-workspace/node/node_modules/.bin/shell-exec-mcp",
      "args": [],
      "env": {
        "HOME": "/var/lib/haru-workspace/home",
        "PATH": "/opt/haru-workspace/node/node_modules/.bin:/usr/bin:/bin",
        "TMPDIR": "/var/lib/haru-workspace/tmp"
      }
    },
    "file-ingress": {
      "command": "/opt/haru-workspace/proxy/bin/python",
      "args": ["/opt/haru-workspace/file_ingress_server.py"],
      "env": {
        "HOME": "/var/lib/haru-workspace/home",
        "PATH": "/opt/haru-workspace/node/node_modules/.bin:/usr/bin:/bin",
        "TMPDIR": "/var/lib/haru-workspace/tmp"
      }
    }
  }
}
```

Keep this file free of credentials. If a future backend genuinely needs a secret, inject it outside Git and review whether that backend still belongs in the same trust boundary.

For the selected `mcp-proxy` 0.12.0 reference, named-server entries consume `command`, `args`, and `env`; they do not provide an effective per-server `cwd`. Named child processes inherit the **proxy process working directory**. Therefore the foreground `cd /srv/haru-workspace` and the systemd `WorkingDirectory=/srv/haru-workspace` below are the controls that establish the working directory for both named children. Do not rely on a JSON `cwd` key or the proxy CLI `--cwd` option for named servers at this version. If a future proxy version adds verified per-server working-directory support, document it only for that verified version/source.

## Start the supervised loopback composition

A foreground canary is useful before systemd. Run the repository-owned supervisor from the workspace directory so the selected proxy and all named stdio children inherit that working directory:

```bash
cd /srv/haru-workspace
HOME=/var/lib/haru-workspace/home \
TMPDIR=/var/lib/haru-workspace/tmp \
/opt/haru-workspace/proxy/bin/python \
  /opt/haru-workspace/workspace-supervisor.py \
  --proxy /opt/haru-workspace/proxy/bin/mcp-proxy \
  --host 127.0.0.1 \
  --port 8766 \
  --named-server-config /etc/haru-workspace/servers.json
```

The supervisor discovers backend names from `servers.json` and probes each named endpoint with MCP `initialize` + `list_tools`. A listening proxy with a dead child is therefore unhealthy. Persistent backend failure exits the supervisor nonzero; under the systemd example below, `Restart=on-failure` and `KillMode=control-group` replace the whole composition rather than leaving a half-dead proxy.

The expected Haru-facing endpoints are then:

```text
http://127.0.0.1:8766/servers/filesystem/mcp
http://127.0.0.1:8766/servers/shell/mcp
http://127.0.0.1:8766/servers/file-ingress/mcp
```

Do not change the bind address to `0.0.0.0` merely to make connectivity easier. If the Haru gateway cannot reach a loopback backend on the same host, fix the local service/configuration problem instead of creating a public backend route.

## Systemd workspace isolation example

After the foreground canary passes, supervise the composition with systemd. The repository includes [`deploy/workspace/haru-workspace.service.example`](../deploy/workspace/haru-workspace.service.example), which is intended to be copied, reviewed, and tuned before installation.

The example makes several boundaries concrete:

- `User=haru` / `Group=haru` use one dedicated non-sudo identity for the basic gateway + workspace deployment. The gateway example uses the same identity. Splitting gateway and workspace users is optional advanced hardening.
- `HOME=/var/lib/haru-workspace/home` and `TMPDIR=/var/lib/haru-workspace/tmp` keep service runtime state away from a normal login home. Setting `HOME` alone is not filesystem isolation.
- `ProtectHome=tmpfs` masks ordinary `/home`, `/root`, and `/run/user` trees inside the service namespace, while `ProtectSystem=strict` makes the host filesystem read-only by default. `ReadWritePaths=` re-opens only the workspace and Haru runtime state for writes. Other system paths may still be readable, so do not describe this as making the entire host invisible.
- `BindPaths=/srv/haru-workspace` makes the intended workspace an explicit mount in the service namespace; the filesystem server is independently scoped to the same root by `servers.json`.
- `WorkingDirectory=/srv/haru-workspace` is inherited by the supervisor, proxy, and selected named servers. There is intentionally no per-server `cwd` field and no `mcp-proxy --cwd` claim.
- The supervisor probes configured named backends, requires consecutive failures before failing closed, and uses bounded probe deadlines. `Restart=on-failure` recovers the composition; `StartLimit*` prevents an unbounded restart storm.
- `KillMode=control-group` keeps the supervisor, proxy, and stdio descendants under one service lifecycle.
- `MemoryMax=1G` and `TasksMax=128` are **example starting points**, not Haru requirements. Tune them to the machine and workload. The workspace is the bursty/disposable part of the stack and should be contained so it is less likely to starve the smaller gateway/tunnel services.

Before starting the service, create `/srv/haru-workspace`, make it owned by the chosen non-sudo service account, install the reviewed named-server config under `/etc/haru-workspace/`, and keep `/opt/haru-workspace` plus `/etc/haru-workspace` non-writable by that runtime account. `StateDirectory=haru-workspace` lets systemd create `/var/lib/haru-workspace`; the unit then creates the service HOME/TMPDIR beneath it.

Adjust hardening for your distribution and required tooling. Test the final unit with `systemd-analyze verify` where available, then perform the foreground/runtime checks below.

## Point the Haru gateway at the backend

The gateway defaults already match this reference composition:

```text
HARU_MCP_WORKSPACE_FILESYSTEM_URL=http://127.0.0.1:8766/servers/filesystem/mcp
HARU_MCP_WORKSPACE_SHELL_URL=http://127.0.0.1:8766/servers/shell/mcp
HARU_MCP_WORKSPACE_FILE_INGRESS_URL=http://127.0.0.1:8766/servers/file-ingress/mcp
```

Restart the gateway only when its environment changed.

## Verify the composition

Check the layers separately:

1. **Listener:** confirm the workspace proxy is listening on loopback only.
2. **Proxy status:** `mcp-proxy` provides a global `/status` endpoint; use it as a local process signal, not as proof that every delegated tool is correct.
3. **Backend health:** call `workspace_backend_health`; filesystem, shell, and file-ingress must all be healthy.
4. **Gateway health:** confirm Haru's `health` tool still works.
5. **Discovery:** from an MCP client through Haru, confirm the workspace filesystem, file-import, file-export, and shell tools are present.
6. **Filesystem smoke:** list or read a harmless file inside the disposable workspace.
7. **File-ingress smoke:** from ChatGPT, pass a harmless current-conversation file to `workspace_import_chatgpt_file` and verify its returned byte count/hash and destination. Confirm a raw unregistered local path is not treated as a file reference by the client host.
8. **File-ingress negative boundary:** reject an absolute/escaping destination, a missing parent directory, an existing destination without `overwrite=true`, and any non-approved/non-HTTPS download host at the child boundary.
9. **File-export smoke:** export a harmless binary file with `workspace_export_file`, read the returned resource, and verify the bytes match. Reject absolute paths, `..` escapes, directories, symlinks that resolve outside the workspace, missing files, and files above 100 MiB.
10. **Shell smoke:** run a harmless command such as `pwd` and verify it resolves inside the intended workspace. Then run a command that exceeds a short `wait_ms`; verify it returns a running job, remains alive, and can be observed/stopped with `shell_job_status` / `shell_job_stop`.
11. **Negative filesystem boundary:** verify a filesystem request outside the configured root is rejected by the filesystem server.

A healthy Haru gateway does not prove the delegated backend is healthy. Conversely, a backend failure should make delegated calls fail; it should not make the gateway start exposing a different/public backend.

## Stop, recover, and update

Stopping the workspace service should terminate the supervisor, proxy, and stdio descendants as one control group. Check for leftover child processes after abnormal tests or browser/shell probes.

For recovery, keep the backend private while you inspect the local service, package install, configuration, filesystem permissions, and logs. Do not create a temporary public listener as a recovery shortcut.

For upgrades, change one pin set deliberately, rebuild cleanly, repeat the acceptance checks above, and keep the previous known-good install available until the new composition passes. Record both package versions and exact upstream source commits so a future operator can reproduce the same stack.
