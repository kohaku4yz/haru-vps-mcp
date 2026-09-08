# Haru VPS MCP

**A mini tunnel to a mini computer for ChatGPT.**

**Setup guide:** [Notion-style web note (中文 / English)](https://kohaku4yz.github.io/haru-vps-mcp/)

Haru VPS MCP is a small self-hosted MCP gateway for an isolated VPS workspace. It gives an MCP client a narrow filesystem/shell/file-transfer facade without making the host itself the workspace.

```text
ChatGPT / MCP client
        |
 authenticated/private tunnel
        |
        v
 Haru MCP gateway
   127.0.0.1:8765
        |
   +------+------+
   |      |      |
   v      v      v
filesystem managed-shell file-transfer
 backend      backend     child
 loopback  loopback loopback
   |      |      |
   +------v------+
 isolated workspace
```

## Security boundary

Haru MCP exposes powerful workspace filesystem and shell tools, so treat the endpoint as privileged.

- The gateway refuses non-loopback bind addresses.
- Workspace backend URLs must be explicit loopback HTTP endpoints and cannot contain credentials.
- ChatGPT file ingress accepts only host-supplied file references, restricts downloads to approved OpenAI storage hosts over HTTPS, pins validated public DNS addresses before connecting, caps imports at 100 MiB, and writes only beneath the workspace root.
- Workspace file export accepts only workspace-relative regular files that resolve beneath the workspace root, caps exports at 100 MiB, and returns an MCP `ResourceLink` instead of creating a public download URL.
- Do not hand-craft file download URLs or treat raw client/sandbox paths as file references. The MCP host is responsible for supplying the `file` object declared through `openai/fileParams`.
- The backend itself gets no second public hostname; it stays behind the gateway on loopback.
- Optional public Host/Origin allowlists are request/transport hardening only. **They are not authentication.**
- `deploy/Caddyfile.example` fails closed with HTTP 403. Replace it only for a separately reviewed authenticated ingress design.
- For a private/on-prem/local MCP server used from ChatGPT, see [`docs/SECURE-TUNNEL.md`](docs/SECURE-TUNNEL.md) and the current OpenAI Secure MCP Tunnel documentation instead of binding Haru to `0.0.0.0`.
- Keep the delegated workspace disposable and separate from host configuration, credentials, home directories, and production data.

This public repository is a clean reference distribution, not a mirror of a private production host. It intentionally excludes private domains, machine identity, credentials, incident evidence, production deployment state, and owner-specific workspace contents.

## Quick start: gateway

Python 3.10+ is required.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
pytest
./deploy/verify.sh
```

`deploy/haru-mcp.env.example` is a non-secret environment template. The gateway reads its process environment directly; it does **not** auto-load a `.env` file.

For a local foreground run, copy/edit the template and explicitly export it into the shell before starting Haru:

```bash
cp deploy/haru-mcp.env.example .env
# edit .env as needed
set -a
. ./.env
set +a
haru-mcp
```

For systemd, install the reviewed values into the `EnvironmentFile=` used by your service unit instead.

By default, the gateway listens at `127.0.0.1:8765/mcp` and delegates to:

```text
http://127.0.0.1:8766/servers/filesystem/mcp
http://127.0.0.1:8766/servers/shell/mcp
http://127.0.0.1:8766/servers/file-ingress/mcp
```

Those endpoints are a **separate workspace-backend composition**. To build them from the selected upstream components plus Haru's bounded file-transfer child, follow [`docs/WORKSPACE-BACKENDS.md`](docs/WORKSPACE-BACKENDS.md).

The public tool surface is deliberately small: gateway and workspace-backend health, workspace directory listing/read/write/edit/move/stat, ChatGPT file import, workspace file export, managed shell execution, and shell job status/stop. `workspace_import_chatgpt_file` is declared with `openai/fileParams` so the ChatGPT host can replace a current-conversation file with a short-lived file reference before the MCP call. `workspace_export_file` returns an MCP `ResourceLink`; a compatible client can read that resource and present the workspace file as a downloadable file.

For shell work, the caller's `wait_ms` is only how long to wait synchronously. A command that is still running after that budget returns a tracked job instead of being killed; `shell_job_status` observes it and `shell_job_stop` explicitly terminates the owned process group. Output and registry retention are bounded, while automatic idle cleanup is conservative and `persistent=true` exempts intentional long-running work from idle reaping.

The reference workspace service runs a small backend-aware supervisor. It probes the configured named MCP backends rather than trusting proxy liveness alone; persistent child failure makes the composition fail closed so systemd can restart it as one control group.

## Operator documentation

- [`docs/WORKSPACE-BACKENDS.md`](docs/WORKSPACE-BACKENDS.md) — build and operate the loopback filesystem/shell/file-transfer composition.
- [`docs/SECURE-TUNNEL.md`](docs/SECURE-TUNNEL.md) — server-side Secure MCP Tunnel boundary, service supervision, fail-closed recovery, and real-client acceptance.
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — gateway service operations, layered health, upgrades/rollback, repository exact-head discipline, process hygiene, and secrets.
- [`deploy/haru-mcp.service.example`](deploy/haru-mcp.service.example) — minimal hardened systemd starting point.
- [`deploy/Caddyfile.example`](deploy/Caddyfile.example) — intentionally fail-closed public reverse-proxy example.

## Upstream projects

The gateway imports the MCP Python SDK, AnyIO, HTTPX, and typing-extensions. The optional reference workspace composes `mcp-proxy`, the Model Context Protocol filesystem server, and `shell-exec-mcp`; Haru applies a narrow repository-owned managed-job patch to the selected shell package.

Exact selected workspace versions/commits, the `mcp==1.27.1` proxy-stack compatibility pin, and upstream license notes are recorded in [`THIRD-PARTY.md`](THIRD-PARTY.md).

## License

Haru VPS MCP code owned by this repository is available under the [MIT License](LICENSE).

Third-party components keep their own licenses; see [`THIRD-PARTY.md`](THIRD-PARTY.md).
