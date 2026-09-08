# Haru VPS MCP operator guidance

Use this gateway as a narrow bridge into an isolated VPS workspace. Treat the workspace filesystem and shell tools as privileged capabilities.

- Keep the Haru gateway and workspace backends bound to loopback.
- Put any Internet-facing ingress behind authentication supplied by your tunnel or reverse proxy.
- Do not treat Host, Origin, or DNS-rebinding checks as authentication.
- Keep the delegated workspace root disposable and separate from production application data, credentials, home directories, and host configuration.
- Prefer read-only inspection before mutation. Keep changes scoped to the isolated workspace unless the operator explicitly authorizes a wider boundary.

- For `workspace_import_chatgpt_file`, pass a current-conversation file through the tool's `file` parameter and let the ChatGPT host supply the file reference. Never construct or paste a download URL manually.
- Keep file-import destinations relative to the workspace root. The parent directory must already exist, and overwrite remains opt-in.
- For `workspace_export_file`, pass a workspace-relative regular file path. The client may turn the returned MCP resource link into a downloadable file.
- Treat `shell_execute.wait_ms` as a response wait budget, not a process-lifetime timeout. If it returns a running job, use `shell_job_status` to observe it or `shell_job_stop` to stop it explicitly.
- Use `persistent=true` only for intentional long-running work that should be exempt from ordinary idle reaping; output and registry bounds still apply.
