# MCP sign-in on demand, and the MCP servers dialog (#199)

Replaces the eager "one question per server" of [mcp-reauth.md](mcp-reauth.md) option 2.
The reconnect plumbing it built (`mcp_authenticate`, `mcp_oauth_callback_url`,
`mcp_reconnect`, reply handling, the `mcp_question` marker) stays and is reused.

Doug's org has many claude.ai connectors installed, most of which he doesn't use and some
he has no login for (Clay). A question for each was noise, and the questions he did act on
never closed. So the wheelhouse now asks about a server only when the session needs it,
and a dialog lists every server with a button to refresh it.

## Decisions (Doug, Q1 to Q4)

- **Q1. The dialog is per session**: the servers of the session in context, as its host
  last saw them. Refresh on a claude.ai connector also reconnects it in every other hosted
  session that has it, as its sign-in is account-wide. Local servers stay per session (per
  project config).
- **Q2. Alt+Shift+M** opens it ("MCP", beside Alt+M Model).
- **Q3. Disabled servers are listed, with no button.** Disabling stays a choice made in
  `/mcp` or the settings: a toggle here would write Claude Code state other sessions share.
- **Q4. A tab session's dialog says "Runs in a tab: /mcp there"**, and lists nothing: a
  tab's Claude Code has no control channel to the wheelhouse.

## Why the old questions never closed

- The only thing that closed a question was a check (`check_mcp_now`) reading
  `get_mcp_status()`: it settles a question whose server reads `connected` or `disabled`.
  Nothing in that check reconnected anything. `mcp_reconnect` went only on a reply.
- For a claude.ai connector, Claude Code's `mcp_authenticate` only builds the claude.ai
  `.../mcp/start-auth/<id>` URL (`callbackExpected: false`); nothing waits locally. The
  connector stays `needs-auth` in that Claude Code until someone reconnects it, however
  long ago it was authorised on claude.ai. A local HTTP/SSE server instead runs Claude
  Code's own OAuth flow, which reconnects it when the browser comes back.
- So the body's "This closes by itself once it's connected" held for a local OAuth server
  and was false for a connector. The test that should have caught it flipped its fake
  connector to `connected` with no reconnect.

Unproven, so nothing depends on them:
- H1: the CLI never re-reads a connector's sign-in by itself.
- H2: reconnecting a connector that isn't authorised is harmless (returns it `needs-auth`).

## What was built

### (a) The session asks when it needs a server

- `request_mcp_signin(server, why)`, a wheelhouse tool for a hosted session only, shaped
  like `request_compact`. It reads the host's last look at the servers
  (`sessions.mcp_status`): an unknown name is refused, naming the servers that need the
  person; one connected, pending or disabled is said so and nothing is asked; one with an
  open question gets its ref. Otherwise it leaves an ask (`do: ask`) in `sessions.mcp_asks`
  (JSON list, taken whole by the host's next poll; the same ask about the same server
  replaces the one waiting). A name without Claude Code's `claude.ai ` prefix or
  its case still matches, once unambiguous.
- The host, on that ask, reads the status afresh and posts the marked question as before
  (`mcp_item`, `mcp_question(name)`), the session's `why` at the top. A server no longer
  needing the person, already asked about, or whose question the person closed in the same
  state (`mcp_quiet`) gets nothing.
- `check_mcp` no longer posts on its own. It still runs at connect, after every turn and
  every 30 s while a question is open, settles questions, and writes the status for the
  dialog and the tool when it changes (`show_mcp`).
- `protocol_sdk.md` tells the session to call it, rather than telling the person to
  authorise elsewhere, and not to ask about servers it doesn't need. Claude Code already
  tells the model which servers need authentication, so no list is injected.
- `PROTOCOL_VERSION` 22: a new tool and store request, and an older host still asks eagerly.

### (b) The MCP servers dialog

- Alt+Shift+M, over the inbox, for the session in context. Shown in F1.
- Rows from `sessions.mcp_status`, which reaches the app with the session's row: name,
  kind (`claude.ai` or `local`), status, and the error, link or result of the last press.
  Needs-auth and failed sort first. Opening it asks the host for a fresh check.
- A button per row (`Hub.mcp`, the daemon's `POST /sessions/<id>/mcp`, an ask in
  `sessions.mcp_asks`: `check`, `reconnect`, `signin` or `callback`, `host.MCP_DOS`). The
  host's answer, the link or the error, lands in the row (`sessions.mcp_status`), never in
  the session's turns. A pasted address sits in `mcp_asks` only until the next poll takes it. Per kind:
  - claude.ai connector, connected or failed: Reconnect, here and in every other hosted
    session that has it (Q1).
  - claude.ai connector, needs auth: Sign in runs `mcp_authenticate`; the link shows in
    the row and opens in the browser; the button then reads Reconnect, as above.
  - local HTTP/SSE, needs auth: Sign in, the same; Claude Code waits for the browser and
    connects it itself. If the browser can't reach it, a box takes the address the browser
    ends on (`mcp_oauth_callback_url`, then a reconnect).
  - local stdio, or anything failed: Reconnect (a stdio server's process restarts).
  - disabled: no button (Q3).
- The session not running: the last look, dated, with no buttons. A tab session: Q4.
- Each look is written only when it changes, or when the dialog asked for it (dated then).

### (c) A connector's question closes once it is authorised

While a question about a claude.ai connector in `needs-auth` is open, the check reconnects
it before reading the status: at each 30 s recheck for the first 10 minutes, then every 5
minutes, and not after 2 hours (`MCP_RETRY_*`). Each try is cut off after 5 s, and one that
fails is logged, never replied: if H2 is false, the cost is a log line every few minutes
for two hours, and a reply still reconnects at once. The question's body says so. The test
fake keeps a connector `needs-auth` until it is reconnected.

### (d) Today's open questions

`deny_stale` already closes every MCP question a new host doesn't own, so Relaunch (or
Relaunch all, Alt+Shift+L) clears the eager ones. Its note now says that servers are asked
about only when a session needs one, and that Alt+Shift+M lists them. A host still on the
old code keeps asking until it is relaunched; the release note says to Relaunch all.

## Not changing

Reply handling on an MCP question (a pasted URL, a reconnect, never a turn), `Store.claim`
holding those replies for the host, and closing them on Shell and when the host stops.
