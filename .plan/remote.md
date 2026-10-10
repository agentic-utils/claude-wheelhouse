# Remote wheelhouse (design)

Status: design agreed, phase 1 next. No code yet.

## Intent

Use the wheelhouse from a laptop away from home (on a train, say) while the sessions keep
running on the home desktop. Sessions and the core of the wheelhouse always run on the
home machine; only the user interface travels. A dropped signal or a closed lid costs
nothing, and reopening lands where you were.

## Plan in brief

**Split the hub out of the TUI into a home daemon with thin clients, and build it in
phases that each ship.**

1. **Phase 0, no code:** a VPN, sshd and tmux on the home machine. It proves the network
   path, and running today's TUI over SSH gives real evidence about what hurts.
2. **Phase 1:** move the hub logic out of `tui.py` into a service, in process. No visible
   change. This is the service layer already planned in `.plan/suite-architecture.md`.
3. **Phase 2, the first remote slice:** `claude-wheelhouse daemon` owns that service and
   serves it over HTTP plus Server-Sent Events (SSE). The TUI becomes a client of it,
   locally and remotely: `claude-wheelhouse --connect home`. The laptop runs this client
   in its own terminal.
4. **Phase 3:** a web console for the laptop browser, close to the TUI layout.
5. **Phase 4:** a phone layout of the web console.

Why. The sessions already survive any client, because hosts are detached and talk only
through SQLite, so the remote problem is purely about the *hub*. Today the hub lives
inside the TUI process: relaunch orchestration, liveness, transcript reading, the usage
fetch, unsent drafts. Serving that process to a browser keeps it there and gives one hub
per browser tab. Pulling it into a daemon gives one hub, any number of thin clients, and
the web surface the suite architecture already reserved a slot for. Starlette, uvicorn,
sse-starlette and httpx are already installed as dependencies of `mcp`, so the server
adds no new dependency tree.

## 1. Goals and non-goals

Goals:

- **The work carries on when the client goes.** Signal drops, lid shuts, train arrives:
  sessions keep running on the home machine, and nothing the client was doing is lost or
  done twice.
- **The client is light.** A laptop needs a small Python client or a browser, not Claude
  Code, the repos, WSL or the credentials.
- **Reconnect and carry on** within seconds, landing where you were.
- **Several clients at once** (home desk plus laptop, laptop plus phone) without them
  fighting.

Non-goals: access over the open internet; more than one user; running sessions on the
laptop; a full Claude Code terminal in the browser in the first slices; offline *work*
(you can submit answers offline, and nothing reaches a session until you reconnect).

## 2. The baseline: what works with zero build

**The baseline already meets the core goal; it falls short on comfort and on several
clients.** Sessions run as `setsid --fork` hosts, so closing the TUI never stops them. Run
the TUI inside tmux on the home machine, reach it over SSH (or mosh) on the VPN, and a
dropped signal or a closed lid costs nothing: reattach and carry on.

Prerequisites on the home machine: sshd and tmux in WSL, and a VPN that reaches into the
house. WSL in NAT networking mode has no address of its own on the home network, so the
VPN either runs inside WSL (Tailscale does, and gives WSL its own VPN address) or needs a
Windows port proxy or WSL's mirrored networking. The home machine must also not sleep.

What the baseline lacks:

| Gap | Why |
|---|---|
| Paste from the laptop's clipboard | Right-click paste runs `powershell.exe Get-Clipboard`, which reads the *home* machine's clipboard. Copy uses OSC 52, which reaches the laptop only if its terminal supports it |
| Shell and tab sessions | `wt.exe` opens a tab on the home desktop, where nobody is looking |
| Typing feel on a poor link | Every keystroke and repaint round-trips. mosh's local echo helps a line editor, not much a full-screen Textual app (inferred) |
| Browser or phone | Needs an SSH client and a terminal; a phone keyboard on a full-screen TUI is grim |
| Several clients | Two tmux attaches share one screen and one cursor. Two separate TUIs work against SQLite, but each keeps its own relaunch list, drafts and usage fetches |
| Directory picker | Browses the home disk, which is right, but slowly over the link |

None of this stops work happening. All of it is friction, which is what the build removes.

## 3. Options considered

| | (a) textual-serve / textual-web | (b) Daemon, API, thin clients | (c) Hybrid path |
|---|---|---|---|
| What it is | Serve the existing TUI to a browser | Headless hub with HTTP plus SSE; TUI and web as clients | (b) built in phases; (a) usable on top once clients are stateless |
| Effort | Under a day | 2 to 3 weeks of agent time (estimate) | Same as (b), spread over shippable phases |
| Survives a drop | Sessions yes; the TUI process no (below) | Yes, both | Yes from phase 2 |
| Several clients | One hub per tab: duplicated hub logic | One hub | One hub |
| Phone | Works badly (terminal in a browser) | Purpose-built page | From phase 4 |
| Main risk | Unmaintained edges, no auth | Size of the `tui.py` refactor | Same as (b) |

The plan is (c).

**What the Textual projects offer**, checked 2026-10-09:

- **textual-serve** (self-hosted; PyPI 1.1.3, released 2025-11-01). Each browser websocket
  starts a fresh app subprocess with `TEXTUAL_DRIVER=textual.drivers.web_driver:WebDriver`,
  and closing the socket sends that app a quit (`app_service.py`). There is no
  authentication on any route (`server.py`), and no reconnect: a reload is a new app. The
  README calls it a local tool and points at textual-web for public URLs.
  [repo](https://github.com/Textualize/textual-serve)
- **textual-web** (beta; PyPI 0.8.0, released 2024-08-30, nothing since). Publishes apps at
  public URLs through Textualize's service, with an optional account for stable URLs. Its
  README: "if you close the browser tab it will also close the Textual app", with
  resumable sessions listed as future work. [repo](https://github.com/Textualize/textual-web)

So (a) loses the TUI's in-memory state on every drop (unsent text, a relaunch in flight,
which host was left stopped) and runs one hub per tab. textual-web routes through a third
party and looks dormant, so it is out. textual-serve is a reasonable *client* once the hub
is in a daemon, because then the TUI process holds nothing worth losing. That is a bonus
of the hybrid, not part of its plan.

## 4. Design

### Process model

```
 laptop / phone / home desk            home machine (WSL)
 +-------------------------+          +------------------------------------------+
 | TUI client  | browser   |  VPN     | claude-wheelhouse daemon  (systemd user) |
 | (Textual)   | console   | <------> |   service: queries, commands, feed       |
 +-------------------------+ HTTP+SSE |   owns: liveness, relaunch, launch,      |
                                      |   transcripts, stats, usage fetch        |
                                      +-------------------+----------------------+
                                                          | SQLite (WAL)
                                      +-------------------v----------------------+
                                      | hosts (setsid), MCP servers, monitors    |
                                      +------------------------------------------+
```

The daemon is **not in the sessions' data path.** Hosts and MCP servers keep reading and
writing SQLite directly, so a daemon crash or upgrade stops only the clients' view, never
a session. One daemon per user, held by a lock file in the state dir, run as a systemd
user unit (WSL's `systemd=true`), and started by the TUI if it isn't running.

| Concern | Today | After |
|---|---|---|
| Change polling (`PRAGMA data_version`) | TUI, 1 s tick | Daemon, pushes events |
| Liveness, wake detection | TUI | Daemon; status is a field on the session view |
| Relaunch orchestration | TUI memory (`relaunching` dict) | Daemon, state in a DB column so a restart resumes it |
| Launch, restore, adopt | TUI calls `launch.py` | Daemon command |
| Transcript and stats reading | TUI and stats pane workers | Daemon; clients get render-ready data |
| Account usage fetch | Stats pane | Daemon, once, not per client |
| Rendering, animation, keys, drafts in the box | TUI | Client |
| Clipboard | TUI via PowerShell and OSC 52 | Client's own |

Local and remote TUIs both go through the daemon. One code path beats a second in-process
mode that drifts; the cost is that the TUI needs the daemon up, which auto-start covers.

**The wheelhouse still never acts on a session by itself.** The daemon owns liveness, but
not the decision to restart: a host that dies stays dead, shows as dead in every client,
and waits for Restore, exactly as today. Remote changes where the person looks from, not
who decides. A host that crashes while the laptop is shut is seen the next time a client
opens.

### API sketch

Resources are the service's queries, as JSON (the suite plan's frozen dataclasses through
`asdict`). Commands are POSTs. Errors map from the existing hierarchy: `SessionGone` 404,
`StillStarting` and "already decided" 409, `NotAllowed` 403.

```
GET  /sessions                         list, with status, mode, queue count, context size
GET  /sessions/{sid}/conversation      transcript blocks
GET  /items?sid=&finished=             inbox, ranked as today
GET  /items/{sid}/{ref}/thread
POST /sessions                         new session {cwd, name, ticket, brief}
POST /sessions/{sid}/{restore|relaunch|park|end|interrupt|compact|shell|rename|mode}
POST /sessions/{sid}/send              {text, ref?}  immediate, or queued per mode
POST /sessions/{sid}/dispatch          send the queue;  POST /dispatch for Send all
POST /items/{sid}/{ref}/permission     {decision: allow|always|deny, message?}
POST /items/{sid}/{ref}/close
GET  /fs?path=                         directory listing for the New session picker
GET  /modules/{id}/...                 a module's own routes
GET  /events                           SSE stream
```

Every POST carries an `Idempotency-Key` (a client UUID). The daemon remembers keys for a
day, so a send retried over a flaky link lands once.

**Events** are invalidations, not diffs: `{seq, kind: sessions | items | thread |
conversation | stats | module, sid?, ref?}`. The client refetches what it shows. Each
event has a monotonic id; on reconnect the client sends `Last-Event-ID`, and if the
daemon's short ring buffer no longer holds it, the client refetches everything (one user's
data, so this is cheap). SSE rather than WebSocket, because reconnection with
`Last-Event-ID` is built in and commands are plain HTTP; a WebSocket arrives only with a
browser terminal.

**Answers and sends** go through the same store calls as today (`send`, `queue`,
`dispatch`, `answer_permission`), so the host side does not change at all.

**Stats and plugin panes.** The module protocol grows a server half: a module's `service`
runs in the daemon and mounts routes under `/modules/{id}`, and its `tui` surface fetches
from them. For stats, `stats.py`'s parsing and following move to the daemon, and the pane
keeps layout and drawing. This breaks the plugin API, so `WHEELHOUSE_API` goes to 2.

### State and reconnection

| State | Where it lives | On a drop |
|---|---|---|
| Sessions, items, threads, queued answers | SQLite (already) | Nothing lost; refetch on reconnect |
| Relaunch in progress | Daemon, persisted | Carries on without any client |
| Text typed but not submitted | Client, in a local file per target | Survives a closed lid; never synced |
| Submitted while offline | Client outbox, with idempotency keys | Sent as soon as the client reconnects |
| Which item you're looking at | Client | Restored locally |

**Answers submitted offline send on reconnect, with no extra step.** The client is a
window, not a gatekeeper: queued or immediate delivery is the session's mode, decided in
the execution layer. A session in queued mode gets the answer in its queue, as if it had
been typed at home. A session in immediate mode gets it straight away, which is what
immediate mode means; the person chose that mode knowing the session may have moved on.
The idempotency key makes a send retried across a drop land once.

**Two clients.** Messages append, so two answers to one question both arrive, in order.
A permission is decided once: the second Allow or Deny gets 409 and a toast saying another
client decided it, including one replayed from an offline outbox. Mode toggles are
last-write-wins. The queue is shared, since it is in the database: either client can send
it, and both see it go. Unsent box text is per client, by design.

### Transport and access

- **Network:** a VPN reaching the home machine is the protection. The daemon speaks plain
  HTTP inside it; TLS and per-client authentication are deferred and can be added later
  without changing the API.
- **Reaching the daemon:** the daemon binds to `127.0.0.1` by default, with a configured
  address for remote use: the VPN interface's address, or localhost behind a proxy such
  as frp. Never `0.0.0.0`.
- **WSL networking:** a VPN running inside WSL gives the daemon its own address despite
  NAT mode. A VPN on the Windows side needs a port proxy or WSL's mirrored networking.
- **Browser safety:** POSTs check `Origin` and `Host`, so a hostile page open in the
  laptop's browser can't drive the daemon. This is cheap and needs no login.
- **Clipboard in the web console:** browsers grant the async clipboard API only to secure
  contexts, and plain HTTP to a VPN address is not one. Paste through the browser's own
  paste event works regardless; copy falls back to a selection copy until TLS arrives.

### Windows-only features with a remote client

| Feature | Home client | Remote client |
|---|---|---|
| Shell escape hatch | Opens a Windows Terminal tab, as today | Daemon runs `claude --resume <id>` in a tmux window named for the session; the client shows the `ssh -t home tmux attach -t wh-<sid>` line to run. A browser terminal can follow |
| Tab-runner sessions (pre-host) | Unchanged | Visible and answerable; launch and adopt from a remote client always use a host |
| Paste | Client's own clipboard (Textual bracketed paste, browser paste) | Same. The PowerShell read stays only for a client running on the home machine |
| Copy | OSC 52 | OSC 52, or the browser's copy |

### Testing

- **Service:** the existing pytest suite moves with the logic; the hub's rules (park and
  end on a running session, relaunch waits) get tested without Textual.
- **API:** contract tests through httpx's ASGI transport: error mapping, idempotent
  replays, a permission decided twice, an offline outbox replayed, the SSE resume and the
  gap refetch.
- **TUI client:** Textual pilot tests against a fake API, including a dropped stream.
- **End to end:** daemon, one host on the fake SDK client, TUI client, in one test run.
- **Train test:** `tc netem` loss and delay on the loopback, plus a real trip.

### Phases

| Phase | Ships | Done when |
|---|---|---|
| 0 | VPN, sshd, tmux on the home machine. No code | The TUI runs from the laptop on a train |
| 1 | `hub.py` service; `tui.py` keeps only widgets | Suite green, no visible change, `tui.py` loses its process and transcript logic |
| 2 | Daemon, API, SSE; TUI as client; tmux Shell | **First remote slice:** laptop TUI over the VPN, lid closed mid-relaunch, reopened, nothing lost |
| 3 | Web console for the laptop browser: session list, inbox, conversation, answer box, permission buttons, stats over the API (`WHEELHOUSE_API` 2) | The train session runs in a browser with no terminal client |
| 4 | Phone layout: inbox, thread, answer, permission buttons, Send | Answer a permission from the phone |
| 5 | Browser terminal for Shell; TLS and per-client auth when wanted | Shell works from the browser |

## Open

- **Which VPN.** Any VPN that reaches the home machine works with this design; the choice
  is deferred to phase 0.
- **frp with no auth.** If frp forwards through a relay on the open internet rather than
  inside the VPN, the daemon is reachable by anyone who finds the relay's port. Auth (or
  frp's own access control) has to arrive with that route.
- **Telling the person a host died.** Every client shows a dead host as dead. Whether the
  daemon should also post an inbox item when one dies, so it is seen in the inbox rather
  than only in the session list, is not decided.
