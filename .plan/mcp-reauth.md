# MCP reconnect and re-auth (#179)

Superseded in part by [mcp-on-demand.md](mcp-on-demand.md) (#199): the host no longer asks
about every server unprompted, the session asks for the one it needs, and a dialog lists
them all. The reconnect plumbing below stays.

A hosted (SDK) session has no `/mcp`, so an MCP server that needs signing in again (Slack,
a claude.ai connector, just did) leaves the session without its tools. Tab sessions are
not the problem: their terminal has `/mcp`.

## What Claude Code offers (found in CLI 2.1.292 bundled with claude-agent-sdk 0.2.164, and
## the user's CLI 2.1.310)

- `get_mcp_status()` (control request `mcp_status`): each server's `name`, `status`
  (`connected`, `pending`, `failed`, `needs-auth`, `disabled`), `error`, `config.type`
  (`claudeai-proxy` for a claude.ai connector), `scope`.
- `reconnect_mcp_server(name)` (`mcp_reconnect`), `toggle_mcp_server(name, enabled)` (`mcp_toggle`).
- Not wrapped by the SDK, but handled by the CLI's control protocol:
  - `mcp_authenticate {serverName, redirectUri?}` returns `{authUrl, requiresUserAction,
    callbackExpected, callbackPort?}`. For a claude.ai connector the URL is claude.ai's
    `.../mcp/start-auth/<id>`: nothing waits locally, so a `mcp_reconnect` is needed after.
    For a local OAuth server (HTTP/SSE) the CLI starts the flow without opening a browser,
    listens on `localhost:<callbackPort>`, and reconnects the server itself once the browser
    comes back.
  - `mcp_oauth_callback_url {serverName, callbackUrl}`: the redirect URL pasted by hand,
    for when the browser can't reach the callback port.
  - `mcp_clear_auth {serverName}`: revoke stored tokens.
- `claude mcp login --no-browser <name>` signs in outside any session; the session still
  needs a reconnect after.

## Options, lightest first

1. **Shell (H) and `/mcp`.** Works today: the hosted session moves to a terminal tab and
   back. No code, but it costs a hand-over (an interrupt if busy, a cold restart), and
   nothing tells Doug that a server needs him. Kept as the fallback, not the answer.
2. **Host-owned MCP items (chosen).** The host checks `get_mcp_status()` as it connects and
   after every turn. A server in `needs-auth` or `failed` gets one question item, kind
   `question`, marked in its answer column (`{"from": "mcp", "server": ...}`) so it is the
   wheelhouse's, not the session's. For `needs-auth` the body carries the sign-in URL from
   `mcp_authenticate`. Any reply on the item is the host's, never a turn: a pasted redirect
   URL goes to `mcp_oauth_callback_url`, anything else to `mcp_reconnect`. The item closes
   once the server is connected (or disabled), saying so; a failed reconnect is replied on
   the item and it stays open. No TUI, daemon or route change and no `fingerprint.API` bump:
   items and replies already travel everywhere. `PROTOCOL_VERSION` goes to 21, because
   `Store.claim` now holds replies on MCP questions back from every reader but the host, and
   an older monitor would still pass them on as turns.
3. **An MCP panel in Session info** with status rows and Reconnect / Sign in buttons:
   status over a new daemon route, a host command per action. More surface for the same
   two calls, and it only helps when Doug goes looking; an item finds him.

## Details of option 2

- Lifetime: the items belong to one host's Claude Code. `deny_stale` (host start, back from
  a shell tab) closes any left open, and the next check posts fresh ones with fresh URLs (a
  local OAuth flow dies with its process). Shell closes them as it hands over: `/mcp` in
  the tab is the way then.
- A pasted redirect URL goes to `mcp_oauth_callback_url`, then `mcp_reconnect` (the CLI
  leaves the reconnect to its caller then). Its query string is redacted once used. A local
  flow lasts 300 s: a reply after that starts a fresh `mcp_authenticate` and posts its link.
- Replies are acted on once per question per poll, bounded to 20 s; no MCP failure stops the
  host. The host closes its questions when it stops, and a reply on any MCP question, even
  an earlier host's, is never a turn.
- A failed claude.ai connector or HTTP/SSE server gets a sign-in link too (a rejected token
  shows as failed). Checks are serialised. Closing the question means "not now": that
  server is asked about again only after a new failure.
- They are left out of what makes a session "wait on the person" (keepalive) and of the
  open items a session is told after compaction, as the Compact question is.
- A check that fails (old CLI, a fake client) is a nicety lost, never the session.
- SDK only (`when="sdk"` in whatsnew).

## Doug re-authorising Slack with it

The item `slack needs sign-in` appears in the inbox. Open its link, approve in claude.ai,
reply anything (`done`) on the item: the host reconnects Slack and closes the item.
