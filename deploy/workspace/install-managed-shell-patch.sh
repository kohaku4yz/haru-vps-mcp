#!/usr/bin/env bash
set -euo pipefail

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
: "${NODE_ROOT:=/opt/haru-workspace/node}"
: "${NODE_BIN:=node}"
SHELL_VERSION=1.2.0
PACKAGE_ROOT="$NODE_ROOT/node_modules/shell-exec-mcp"
PATCH_ROOT="$HERE/shell-exec-mcp-patch"

case "$NODE_ROOT" in /*) ;; *) echo "NODE_ROOT must be absolute" >&2; exit 2;; esac
command -v "$NODE_BIN" >/dev/null 2>&1 || { echo "node executable not found" >&2; exit 3; }
test -f "$PACKAGE_ROOT/package.json" || { echo "shell-exec-mcp package missing under $NODE_ROOT" >&2; exit 4; }
test -f "$PACKAGE_ROOT/dist/tools/bash.js" || { echo "shell-exec-mcp bash module missing" >&2; exit 4; }
test -f "$PATCH_ROOT/bash.mjs" || { echo "managed shell bash patch missing" >&2; exit 4; }
test -f "$PATCH_ROOT/managed-jobs.mjs" || { echo "managed shell jobs patch missing" >&2; exit 4; }

"$NODE_BIN" -e 'const p=require(process.argv[1]); if(p.version!==process.argv[2]) throw new Error(`shell-exec-mcp version mismatch: ${p.version}`)' \
  "$PACKAGE_ROOT/package.json" "$SHELL_VERSION"

install -m 0644 "$PATCH_ROOT/bash.mjs" "$PACKAGE_ROOT/dist/tools/bash.js"
install -m 0644 "$PATCH_ROOT/managed-jobs.mjs" "$PACKAGE_ROOT/dist/tools/managed-jobs.js"
printf '%s\n' "installed managed shell patch for shell-exec-mcp@$SHELL_VERSION"
