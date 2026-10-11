# Remote wheelhouse (design)

Status: design agreed. Phase 1 built: `hub.py` is the service, owned by the TUI in
process. Phase 2 under way: the hub is ready for remote clients, and the daemon serves it
over HTTP and SSE, and the TUI is its client (2c), with a bearer token on every request.

**Phase 1 as built.** `hub.Hub` holds the hub logic `tui.py` had: liveness and wake
detection, Relaunch from the stop to the start again, launch, restore and adopt, the
transcript followers (conversations, context sizes, subagents), the account usage fetch
(the stats pane draws the hub's, through `Context.usage`), and the person's commands on
sessions and items (send, queue, dispatch, mode, Park and End, rename, close and reopen,
permissions, host commands). It never imports Textual. Its owner gives it `spawn` (work on
a worker thread) and `post` (a callback back on the owner's thread), and listens for its
`Event`s: `notice`, a toast for what the hub did by itself (a relaunch's outcome, a
subagent sync failing), and `landed`, a transcript read that brought something. Those are
the seams phase 2 puts behind HTTP and SSE. The TUI keeps the widgets, the dialogs and
their wording, the key dispatch (take_key, land, settle), selection, ranking and the
dwell, the tutorial checklist and the splitter sizes. It still reads plain rows straight
from the store and re-queries on its 1 s tick; phase 2 puts the query surface and the
change feed in front of both.

**Phase 2 so far: the hub ready for remote clients.** Still in process, no daemon, no
visible change. A command returns what it did (`Done`): whether it acted, its own notices,
for the surface that gave it alone (behind HTTP, its response), and for submit whether it
denied a permission; only what the hub does by itself goes to every listener (behind SSE,
the events). Commands and status helpers take a session's id and an item's ref, not their
rows. The unsent text per target is the client's, as the design below has it
(`unsent.Unsent`, owned by the TUI), in memory for now: the local file per target comes
when the client goes remote.

**Phase 2 so far: the daemon (2b).** `claude-wheelhouse daemon` (`daemon.py`) owns a hub of
its own and serves the API sketch below, but for the module routes, with Starlette and
uvicorn, and the events with sse-starlette, all now declared in `pyproject.toml` at the
versions already locked. The TUI is unchanged and still owns its own hub; it becomes a
client in 2c, and Shell's tmux window comes in 2d. As built:

- **Binding and the lock.** `127.0.0.1:7428` by default; `--address` takes an IP address,
  never an unspecified one (`0.0.0.0`, `::`). It's meant for the VPN interface's: bound to
  a LAN address, the daemon is open to everyone on that LAN, with nothing but the Host
  check between them and every session. One per user: `daemon.lock` in the state dir
  (the database's directory), held with `flock`, says which process holds it and where.
- **Host and Origin.** Every request, GETs and the stream too, must name the daemon in its
  `Host` (its address and port, `localhost` too on the loopback, and each `--name` given,
  such as a VPN's name for the machine), else 421: a page rebound by DNS to the daemon's
  address can neither read nor drive it. A POST with an `Origin` must come from the
  daemon's own (403); a client that sends none, as httpx doesn't, is accepted. The
  `Idempotency-Key` every command needs is the cross-site guard too: a page can send a
  custom header only after a CORS preflight, which the daemon never grants, so it can't
  send a command at all. The key must never become optional.
- **Threads.** The event loop only serves. The hub runs on a thread of its own, its
  tick, commands and callbacks one at a time as on the TUI's loop; the queries that read
  the store or the disk (`/items`, a thread, a conversation, `/fs`) run on starlette's
  threads; and `/sessions` answers the view the hub's thread made at its last refresh. A
  slow mount holds up neither the hub's tick and commands, nor `/sessions`, nor the
  streams' pings; a locked database neither `/sessions` nor the pings. A conversation
  already being read waits for that read up to a second, then reads for itself, so a
  client refetching on an event never gets the blocks from before it; past the second it
  answers 503 with `Retry-After`, so a hung transcript can't drain starlette's threads.
- **Queries** answer the store's rows as JSON, with what the hub works out beside them: a
  session's shown status, mode, busy, stale, sends_now and context size (from its
  transcript, or its host's count after a compaction); an item's standing, and whether
  it's queued, processing or awaiting a reply, ranked as the TUI ranks them (the selected
  item's pin stays the client's). The dataclass views the suite plan has are not built.
- **Commands** answer the hub's `Done` as `{ok, notices, denied, sid?}` (`sid` for a new
  session). Park and End take `how` (ask, now, cancel, force), the TUI's choice of dialog
  still the client's; Unpark is `POST /sessions/{sid}/unpark`; Relaunch with `host`
  `[pid, start]`, the host the client showed, stops it and starts it again (with
  `compact: true`, once the session is compacted: a session's `compact_first` says when
  to offer that), and without it brings back a dead session; mode takes the mode wanted, so a repeat does nothing
  (last write wins). A new session needs an existing directory's absolute path, and runs
  by a host: `runner` `tab` is refused, as a tab would open on the home machine's screen.
  Errors: `SessionGone` and an item not there 404, a permission decided already 409, a
  bad body (a decision for an item that isn't a permission too) 400. Anything else is the
  daemon's own failure, 500, as JSON too, kept with the key if and only if the command
  wrote anything first: a retry then replays the 500 rather than act twice, and one that
  wrote nothing tries again. `StillStarting` and `NotAllowed` don't exist yet.
- **Idempotency.** Every POST needs an `Idempotency-Key` (400 without). Each answer is kept
  in the database's `idempotency` table for a day, so a request sent again, across a
  daemon restart too, is answered the same and done once; the same key on another request
  is 422. Commands run one at a time, so the same key sent twice at once is done once too.
  An answer the database can't take when given (locked) is kept in memory and written at
  a later tick: only a restart before then could let a retry act twice.
- **Events.** `{seq, kind, sid?, ref?}`, kinds `sessions`, `items`, `thread`,
  `conversation`, `stats`, `notice` (the hub's own, with its text and severity) and
  `refetch`. Seqs start from the clock in milliseconds, so they rise across a restart. The
  last 1024 are kept: a client reconnecting with `Last-Event-ID` gets what it missed, and
  one connecting afresh, or whose id the ring no longer holds (or never held), gets
  `refetch` at the latest seq. A client reading slower than events come falls behind the
  ring the same way. A signal to stop (Ctrl+C, SIGTERM) ends each stream cleanly first.
- **Change detection.** Each second the daemon reads the marks of the `items` and
  `messages` tables, a count of writes to each kept in a `marks` table by triggers on
  every insert, update and delete. The triggers live in the schema, which every `Store`
  creates on open, so every writer bumps them (hosts, MCP servers, the TUI, the daemon,
  and older code once any newer has opened the database), and any change a client could
  see moves a mark (two edits in the same second, a draft taken back and queued again
  under its id). Reading them costs the same whatever the history; then it refreshes the
  hub as the TUI's tick does. The sessions' mark is the rows that refresh read but for `heartbeat_at`,
  so the heartbeat every host writes each second publishes nothing. Each kind whose mark
  moved is published for all: the sessions' rows `sessions` (and `items` if a name or
  parked changed, as the inbox shows the name and leaves a parked session's items out,
  and `conversation` if a name or runner changed), items `items` and `thread`, messages
  those and `conversation`. The
  marks are read before the refresh, so the rows `/sessions` serves always hold what an
  event said, and a write after them is published at the next tick. A command refreshes
  the hub and publishes at once, without waiting for the tick. A session whose shown status changes with no write (a host dying,
  a Relaunch) publishes `sessions` for it, and a transcript read that brought something
  `conversation` and `stats` for it.
- **Relaunch in progress** is kept in a `sessions.relaunch` column (the stopped host, the
  deadline, and, compacting first, when Compact was asked for), so any hub started again in time, the daemon's or the TUI's, carries it
  on. One found past its deadline (the wheelhouse was down, the machine off) is let go
  with "<name> wasn't relaunched: Restore it", its host not started: the wheelhouse never
  restores a dead host by itself.
- **Still to come:** the routes the TUI will need beyond the sketch (adopt, restore all,
  take back a queued answer, mark seen, the tutorial offer, the account usage), the module
  routes and `WHEELHOUSE_API` 2 (phase 3), a systemd unit and the TUI starting the daemon.

**Phase 2c: the TUI a client of the daemon (built, Q48 option A).** `claude-wheelhouse`
starts the daemon on `127.0.0.1:7428`, or joins the one running, and reads and acts
through it over HTTP and SSE rather than owning a hub. `claude-wheelhouse --connect
host:port` joins a daemon on another machine. `claude-wheelhouse --local` is the TUI as it
was before 2c, its hub in process: the way back, at once.

- **The token.** Every request needs `Authorization: Bearer <token>`, GETs, commands and
  the event stream alike, else 401 (with `WWW-Authenticate: Bearer`). The token is 32
  random bytes, URL-safe base64, in `daemon.token` in the state dir (the database's
  directory, beside `daemon.lock`), mode 0600. The daemon makes it the first time it
  starts, under its lock, so two starting at once can't make two. One found readable by
  group or others may have leaked, so the daemon replaces it, and says so; one owned by
  another user stops it starting. It's compared in constant time (`hmac.compare_digest`).
  The Host check (D47) comes first, so a page rebound by DNS learns nothing, then the
  token, then Origin and the Idempotency-Key, which stay as they are: the key is still
  the cross-site guard for a browser that holds the token one day. The token is a
  password, not encryption: inside the VPN it travels in the tunnel's encryption, on a
  LAN address in the clear, and TLS is still phase 5.
  - **Rotation:** `claude-wheelhouse token --rotate` writes a new one in its place (a new
    file renamed over the old, 0600). The daemon reads it again at its next tick when the
    file changes, so from then on the old one gets 401. The local TUI reads the file
    again on a 401 and carries on; a remote client must be given the new one.
    `claude-wheelhouse token` prints it, to give to one.
  - **`--connect`** takes the token from `--token-file PATH`, else `WHEELHOUSE_TOKEN`,
    else asks for it on the terminal without echo. Never a flag's value, which `ps` and
    the shell's history would show.
- **Autostart and join.** The TUI looks at the state dir's lock (`daemon.lock`, held with
  `flock` by the running daemon, which writes `pid N at URL` in it):
  - **Held:** a daemon runs. The TUI joins it at the URL the file names, with the token
    from the token file, once `GET /hello` answers.
  - **Not held:** none runs, whatever the file says: the kernel lets go of a `flock` when
    its holder dies, so a lock file left by a daemon that crashed or was killed is only
    text, and never stale in a way that matters. The TUI starts one: `python -m
    claude_wheelhouse daemon` on the same interpreter and environment (so `WHEELHOUSE_DB`
    and the rest go with it), in a session of its own (`setsid`), so it outlives the TUI,
    its output appended to `daemon.log` in the state dir, on port 7428 or
    `WHEELHOUSE_PORT`. Then it waits up to 10 s for the lock to be held and `/hello` to
    answer. Two TUIs starting at once both start one; the second finds the lock held and
    exits, and both join the first.
  - **Held, but no answer** within 3 s (a daemon hung): the TUI says so, with its pid, and
    stops. It never kills a daemon it can't talk to; `--local` runs without it.
  - **It couldn't start** (the port taken by something else, say): the TUI says so, with
    the log's last line, and stops.
  - **A daemon on older code.** `/hello` answers its API version (1), a fingerprint of its
    code (a hash of its package's source files as it loaded them) and its package's path.
    The same fingerprint: join. Another, from the same package path: the code on disk has
    changed under a running daemon (a pull, say), so the TUI asks it to stop (`POST
    /shutdown`), waits for its lock to go, starts one on the new code and says so. This is
    safe because the daemon holds nothing that isn't in the database (a Relaunch under way
    is, in `sessions.relaunch`), but for an answer the database couldn't take, which it writes
    as it stops if it can. Its other clients see their streams end and reconnect. Another
    fingerprint from another path (another checkout): joined if the API version matches,
    else the TUI stops, naming both. Under `--connect` the TUI never starts or stops a
    daemon: another API version stops it, another fingerprint is only said.
- **The daemon dying under a running TUI.** The stream ends, or a request can't connect.
  The TUI keeps showing what it last had, the title bar says "daemon unreachable:
  reconnecting" in red, and it tries again every half second, backing off to 5 s. A key
  or button that would act says "not connected to the daemon: nothing was sent", and
  what's typed stays in the box: there's no outbox yet (offline answers, in the state
  table below, come later). If the local daemon has gone (its lock free), the TUI starts
  one, up to 3 times in 5 minutes; past that it stops trying and says to read
  `daemon.log`, or run `--local`. Reconnected, the stream resumes from its Last-Event-ID;
  a new daemon's ring doesn't hold it, so the client is told to refetch, and refetches
  everything; the title clears and a toast says it's back. A command whose answer is lost
  mid-request is sent again once, with the same Idempotency-Key, so it's done once; if it
  still can't get an answer, the toast says it may or may not have been done.
- **The TUI's rules, with its data coming over SSE.** The client keeps what the daemon
  last served: the sessions (`GET /sessions`), the inbox (`GET /inbox`: every item but
  their bodies, with what's processing, queued and awaiting, the drafts, and the
  tutorial's steps), and the conversation, thread or subagent's conversation shown. A
  thread of its own reads the stream and fetches what an event says moved, into a copy
  waiting beside the one shown; the TUI swaps it in only where the hub in process used to
  read the store: at the refresh tick, which a key's land holds off, and right after the
  person's own command. So nothing that arrives moves a row under a key: a burst of keys
  acts on the rows it started on and ends as the same keys typed slowly do (take_key,
  land, settle, unchanged). A command is a request that blocks until it's answered, as
  the hub's call did, and the refresh after it fetches what it changed at once, so the
  next key acts on it. From there it's the TUI's own code on the same values:
  - the 1 s dwell is the TUI's timer, and what it marks seen is a command;
  - the inbox's order is the TUI's, by `store.inbox_rank` on the rows and sets `/inbox`
    serves: time, then id, the selected item held where it was until the selection moves
    on, and only Delete, Ctrl+R (D44) and a permission answered moving it at once;
  - the Sink moves and keep-place (D46) work on what's painted, from the same rows and
    the same conversation blocks the hub makes;
  - the eyes and panes still rest while the answer box has focus (D41): animation is
    the TUI's alone.
  A conversation or thread shown for the first time is fetched while the person waits,
  as the hub read it; after that the thread keeps it up to date. The stats pane still
  reads the transcripts itself, so on the home machine it's as it was; its account usage
  comes from the daemon's (`GET /usage`, each minute). Over `--connect` from another
  machine it has no transcripts to read until phase 3's module routes.
- **The routes 2c adds,** each with the token and the rest of the guards: `GET /hello`,
  `GET /inbox`, `GET /usage`, `GET /settings` (the splitter sizes, `layout.` keys only),
  `GET /adoptable`, `GET /adoptable/{id}/running`, `GET /items/{sid}/{ref}/conversation`;
  `POST /shutdown`, `POST /settings`, `POST /adopt`, `POST /restore-all`, `POST
  /items/{sid}/{ref}/seen`, `POST /messages/{id}/unqueue`, `POST /tutorial/offer`, `POST
  /tutorial/seen`. `/sessions` grows each session's liveness (dead or not, apart from
  relaunching, ending and parking) and its model state. Errors carry their type
  (`SessionGone`, `KeyError`), so the TUI says what it said before.
- **Rollout.** 2c makes the daemon the default. `--local` stays, unchanged, until the
  daemon path has had a fair run; it runs its own hub as before, and if a daemon is
  running too it says two hubs are acting (as two TUIs did). Hosts and MCP servers never
  talk to the daemon, so 2c changes nothing for them: no schema change, no new table, and
  sessions on code from before the daemon carry on as they are.
- **As built, beyond the design:**
  - In process, the item list read the store at every repaint, so a host's write could show
    at a key's repaint; through the daemon it shows at the tick, or after the person's own
    command, whose change is fetched before anything is read again. The tests that hold
    the rules above run both ways (`@pytest.mark.both`), the daemon's way through a real
    socket with a client that fetches at each refresh after the daemon's tick.
  - The daemon follows the last two subagent conversations any client asked for, as the
    hub did for its one surface; a client that no longer shows one stops fetching it.
  - New session, from this machine (a loopback peer) only, may still open a tab. Under
    `--connect` it offers no tab and no Browse (the directories are the daemon's), and the
    daemon checks the directory.
  - The text typed but not sent is still in memory, per client: the file per target, and
    the outbox, are still to come.

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
sse-starlette and httpx2 are already installed as dependencies of `mcp`, so the server
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
| Change detection | TUI re-queries on a 1 s tick | Daemon, pushes events (proposed: polls `PRAGMA data_version` to skip unchanged ticks) |
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
`asdict`). Commands are POSTs. Errors map from the `WheelhouseError` hierarchy the suite
plan adds: `SessionGone` 404 (the one that exists today), and the planned `StillStarting`
and "already decided" 409 and `NotAllowed` 403.

```
GET  /sessions                         list, with status, mode, queue count, context size
GET  /sessions/{sid}/conversation      transcript blocks
GET  /items?sid=&finished=             inbox, ranked as today
GET  /items/{sid}/{ref}/thread
GET  /items/{sid}/{ref}/conversation   a subagent item's own transcript blocks (Hub.agent_conversation)
POST /sessions                         new session {cwd, name, ticket, brief, model?, effort?}
POST /sessions/{sid}/{restore|relaunch|park|end|interrupt|compact|shell|rename|mode}
POST /sessions/{sid}/send              {text, ref?}  immediate, or queued per mode
POST /sessions/{sid}/model             {model}   a hosted session's, applied live (.plan/model-effort.md)
POST /sessions/{sid}/effort            {effort}  the same, for effort
POST /sessions/{sid}/dispatch          send the queue;  POST /dispatch for Send all
POST /items/{sid}/{ref}/permission     {decision: allow|always|deny, message?}
POST /items/{sid}/{ref}/close
GET  /fs?path=                         directory listing for the New session picker
GET  /models                           the models and effort levels offered, with defaults
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

- **Network:** a VPN reaching the home machine is the protection, with a bearer token on
  every request (2c, above) so that being on the VPN, or another user on the home
  machine, isn't enough. The daemon speaks plain HTTP inside it; TLS and a token per
  client are deferred and can be added later without changing the API.
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
- **API:** contract tests through httpx2's ASGI transport: error mapping, idempotent
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
| 5 | Browser terminal for Shell; TLS and a token per client when wanted | Shell works from the browser |

## Open

- **Which VPN.** Any VPN that reaches the home machine works with this design; the choice
  is deferred to phase 0.
- **frp with only the token.** If frp forwards through a relay on the open internet rather than
  inside the VPN, the daemon is reachable by anyone who finds the relay's port, with the
  token in the clear over plain HTTP. TLS (or frp's own access control) has to arrive
  with that route.
- **Telling the person a host died.** Every client shows a dead host as dead. Whether the
  daemon should also post an inbox item when one dies, so it is seen in the inbox rather
  than only in the session list, is not decided.
