# Third-party software and upstream attribution

Haru VPS MCP has two different dependency layers. Keeping them separate matters for both maintenance and licensing:

1. **Gateway package dependencies** are Python packages imported by this repository itself.
2. **Optional workspace-backend composition** is a separately installed set of upstream MCP programs used to provide the loopback filesystem and development-shell endpoints behind the gateway; Haru's file-transfer child is repository-owned and not a third-party server.

The proxy and filesystem components below are composed without source changes. The selected shell package is installed at the recorded upstream version and then receives a narrow repository-owned managed-job patch; its upstream source and license remain with its upstream project.

## Gateway package dependencies

The direct runtime ranges are declared in `pyproject.toml`.

| Package | Upstream | Range in this repository | Upstream license |
| --- | --- | --- | --- |
| `mcp` | [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) | `>=1.27,<2` | MIT |
| `anyio` | [agronholm/anyio](https://github.com/agronholm/anyio) | `>=4,<5` | MIT |
| `httpx` | [encode/httpx](https://github.com/encode/httpx) | `>=0.28,<1` | BSD-3-Clause |
| `typing-extensions` | [python/typing_extensions](https://github.com/python/typing_extensions) | `>=4.12,<5` | Python Software Foundation License Version 2 |

Build and test dependencies (`setuptools`, `wheel`, `pytest`, and `pytest-asyncio`) are development/package-build dependencies rather than workspace servers. Their own upstream license notices apply when they are installed or redistributed.

## Reference workspace-backend composition

This reference composition selects the following upstream versions and source commits. They are recorded as a reproducible reference point, not as a promise that these versions remain the newest releases.

| Role | Upstream | Selected package/version | Selected source commit | License/provenance note |
| --- | --- | --- | --- | --- |
| stdio → Streamable HTTP proxy | [sparfenyuk/mcp-proxy](https://github.com/sparfenyuk/mcp-proxy) | `mcp-proxy==0.12.0` | [`f1ae01420086011ae53e6d895b1cd02838b34f42`](https://github.com/sparfenyuk/mcp-proxy/commit/f1ae01420086011ae53e6d895b1cd02838b34f42) | MIT |
| filesystem MCP server | [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers) (`src/filesystem`) | `@modelcontextprotocol/server-filesystem@2026.7.10` | [`9a96ea6e5913736f92b88345bf51caeaaa8e719f`](https://github.com/modelcontextprotocol/servers/commit/9a96ea6e5913736f92b88345bf51caeaaa8e719f) | The upstream repository license file documents an ongoing MIT → Apache-2.0 licensing transition; the selected package says `SEE LICENSE IN LICENSE`. Consult that upstream license file for the exact terms applicable to the selected source. |
| development shell base | [domdomegg/shell-exec-mcp](https://github.com/domdomegg/shell-exec-mcp) | `shell-exec-mcp@1.2.0` | [`70510f1e734daf6da392db7365eabd38e34cdbb0`](https://github.com/domdomegg/shell-exec-mcp/commit/70510f1e734daf6da392db7365eabd38e34cdbb0) | MIT (declared by the upstream package metadata); Haru then applies `deploy/workspace/shell-exec-mcp-patch/` |

The selected proxy composition also used `mcp==1.27.1` as a **compatibility pin for that workspace stack**. That exact pin is intentionally distinct from the public Haru gateway's broader `mcp>=1.27,<2` dependency range.

## Updating upstream pins deliberately

When changing the reference composition:

1. Read the upstream release notes and repository license at the candidate source revision.
2. Record both the package version and the exact source commit that produced or corresponds to it.
3. Rebuild the workspace environment from clean package stores rather than mutating a long-lived install in place.
4. Re-run the version-guarded managed-shell patch and its lifecycle tests against the candidate shell package.
5. Verify the proxy still exposes the named Streamable HTTP endpoints expected by Haru MCP.
6. Verify filesystem scoping and perform only harmless shell acceptance calls in a disposable workspace.
7. Update this file and `docs/WORKSPACE-BACKENDS.md` together if the composition or endpoint shape changes.

The MIT license in this repository's `LICENSE` applies to Haru VPS MCP code owned by this repository. It does not replace, relicense, or supersede third-party licenses.
