# Extra tool servers (MCP)

Valerie can use outside tool servers that speak MCP over HTTP. Add them to `mcp_servers` in your private
`~/.config/valerie-assistant/config.json` (never in the repo). Tools: `mcp_servers`, `mcp_tools`, `mcp_call`.

Fields: `name`, `url`, `enabled`, `headers` (literal, non-secret), `secret_headers` (header name -> path of a file
that holds the value; read at call time, never stored in the config or logs), `allow_tools` (empty = all) and
`auto_approve` (regexes of read-only tool names). Any tool that matches no `auto_approve` pattern needs your
approval first (a dialog on the desktop, or the one-time code `approve <code>` in phone chat, the same as free jobs).
Everything a server returns is treated as untrusted data and cut to 4000 characters.

## SkillsMP (public, read-only, no sign-in)

```json
{"name": "skillsmp", "url": "https://skillsmp.com/mcp", "enabled": true,
 "headers": {}, "secret_headers": {}, "allow_tools": [],
 "auto_approve": ["^search_", "^get_", "^list_"]}
```

Valerie's built-in skill finder (`skill_search`) keeps working on its own; this entry is optional.

## Composio (placeholder URL, secret from a file)

```json
{"name": "composio", "url": "https://<your-composio-mcp-url>", "enabled": false,
 "headers": {}, "secret_headers": {"x-api-key": "~/.config/valerie-assistant/composio.key"},
 "allow_tools": [], "auto_approve": ["SEARCH_", "GET_SCHEMA"]}
```

Put the key alone in that file (`chmod 600`). Everything else (sending, creating, deleting) asks you first.

## Edge worker and network watch

See the README: `edge_worker` (an AI helper on another home device) and `network_watch.services` (devices whose
ports Valerie checks). Real addresses belong only in your private config.
