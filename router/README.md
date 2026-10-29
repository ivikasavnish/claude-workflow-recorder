# mcp-router

One MCP server in front of many. Clients see 4 tools instead of every backend's full list:

| Tool | Does |
|---|---|
| `search_tools(query, server?, limit=5)` | ranked keyword search over all backend tools; returns `server__tool` + inputSchema |
| `call_tool(tool, arguments)` | proxies to the backend (content, images, errors passed through) |
| `list_servers()` | backends, tool counts, running state |
| `refresh_index(server?)` | re-read tool lists after a backend update |

- `servers.json` — backends (stdio only). `"enabled": false` to park one. Edit, then `refresh_index`.
- `index.json` — cached tool lists, keyed by a hash of each server's config (auto-rebuilt on change).
- Backends start lazily on first search/call and stay up for the session (browser state persists).
- Pure stdlib, Python 3.9+. Logs go to stderr.

Register: `claude mcp add -s user router -- /usr/bin/python3 ~/mcp-router/router.py`
(any MCP client: same command, stdio transport).

Remote claude.ai connectors (Gmail, Drive, Calendar, Docs, Atlassian) can't be proxied — their auth lives in claude.ai.
