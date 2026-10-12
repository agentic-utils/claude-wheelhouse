# Keeping the item list in step with the session (design)

Issue #16. The agreed design, ready to build.

## Requirement

Doug, verbatim:

> I have seen a small number of cases where the Wheelhouse gets out of step with the
> session itself, leaving tasks sitting in the list when actually everything is finished.
> Two suggestions: (1) define a control message from the session to Wheelhouse listing a
> definitive set of running tasks/open questions - just the ids and status, no need for
> titles or descriptions; (2) leverage that message in two situations, (2.1) each time the
> session has finished a task; (2.2) requested every 55 minutes by Wheelhouse, as the cache
> warm instruction.

Constraint carried from the token-burn work: the fix must cost as little as possible per
call and per day. Doug, on the design: "keep it efficient: wheelhouse should only ask
about items that are still logged in a transient state (running, processing, etc) and
that have had no updates from the session for at least 15 minutes."

## Findings

### Stale items in the live database

Read-only, on 2026-10-12 at about 08:15Z, against the database and the sessions'
transcripts. Every `post_item` and `update_item` call in the transcripts was replayed
against the stored statuses: the store never lost or misapplied one, and no call failed
on a wrong ref. Three stale items turned up in the last four days:

1. **T81, Rare caper, a task open about 34 h after it finished.** At 22:05Z on 10 Oct
   the session set T81 `waiting` with a note beginning "Done.", and asked Q45. At 22:22Z
   Doug answered Q45; the session replied, posted T82 for the follow-on build, and never
   came back to T81. A compaction followed at 22:32Z: it came after the miss, and meant
   the session could no longer remember it.
2. **Q72, LG plugin fix, a question awaiting the session about 9 h.** Doug answered at
   13:41Z on 11 Oct, in the same notification as a general message. The session acted on
   both, then posted Q73 and Q74, but made no `reply` or `update_item` on Q72.
3. **A59, Rare caper, a finished subagent shown `running` about 5 h.** A compaction
   appended old records to the transcript again, and `subagents.py` read a replayed
   `SendMessage` as a resume. This is the wheelhouse's own bug, fixed separately (below).

In both of the session's own misses, it moved on to a new item (posting T82; posting Q73)
without closing the one it was leaving.

### The keep-warm today

- Only SDK-hosted sessions have one (`host.py`, `keepalive_due`). It fires
  `KEEPALIVE_AFTER` (55 min) after the last turn ended, while the session is quiet, its
  context is over `KEEPALIVE_TOKENS` (150k), no Compact is under way, the last turn didn't
  compact, it has been quiet less than `KEEPALIVE_FOR` (8 h), and it waits on the person
  (`waits_on_person`). Its text is `KEEPALIVE_TEXT`.
- A tab session can be sent text: `Store.notice` writes a `notice` message, the plugin
  monitor (`monitor.py`) prints it as `[wheelhouse] ...`, and Claude Code delivers each
  printed line as a notification. Lines are cut at 500 characters (`LINE_LIMIT` 480).
- Tab sessions write 1-hour caches too (49 of 62 recent transcripts carry
  `ephemeral_1h_input_tokens`), so a 55-minute warm pays off for them.
- `items.updated_at` is written by the person's actions as well as the session's
  (Delete, answering, a decision seen), so it can't measure the session's silence.

## Design

### The principle: the wheelhouse asks, the session corrects

The wheelhouse holds the list; what goes wrong is that the session stops looking at it.
So the wheelhouse names the items it doubts, and the session corrects any that are wrong
with its ordinary `update_item` and `reply` calls. Nothing closes by omission, there is no
new tool, and the UI needs nothing new. A session that doesn't recognise a ref leaves it
alone, which is the safe failure. Refs are durable, so this is rare.

### Which items are asked about

An item is **in doubt** when both hold:

- **It is in a transient state:** a task `running`, `blocked` or `waiting`, or a question
  awaiting the session's reply (`open` and `AWAITING`: the person's word is the latest).
  Not `todo` (nothing has started to finish), not a question still waiting on the person,
  and never agent, decision or permission items, nor anything settled or closed.
- **The session has been silent on it for at least 15 minutes:** the latest session
  update is `DOUBT_AFTER` (15 min) ago or more. A session update is anything the session
  does to that item: `post_item`, `update_item` (status, title, body or note) and `reply`
  on it. A new column `items.session_at` (through `ADDED_COLUMNS`) records it, written by
  those three calls only. The person's actions, the wheelhouse's own writes and the
  session's work on other items don't count. Rows that predate the column use
  `created_at`.

Both stale cases qualify: T81 had been `waiting` 17 minutes when T82 was posted, and Q72
was awaiting the session's reply.

### The ask

Refs and statuses only, as Doug specified. `reply owed` marks a question awaiting the
session's reply:

```
[wheelhouse] Check: T1 running, T3 waiting, Q2 reply owed. Correct any that's finished with update_item or reply, then reply ok. Leave alone any you don't recognise.
```

The session answers with `update_item(ref, status=...)` for each item that's wrong (a task
to `done` or `dropped`, a question to `answered`), several in one request, then `ok`.
Closing a question stays the person's call. If the person changes an item in between,
last write wins, as with any `update_item` today.

### When the wheelhouse asks

**On a change of work (2.1).** No extra request: the wheelhouse appends the in-doubt items
to the result of the calls that mark a change of work, `update_item` and `reply` when they
set a task to `done` or `dropped` or a question to `answered`, and `post_item` of a task or
question. Nothing is appended when no item is in doubt. Both stale cases happened at a
`post_item` (T82, Q73) and would have been caught there:

```
T82. Still open: T81 waiting, Q72 reply owed
```

The protocol gains one sentence: "When a reply lists what is still open, correct any of
it that is finished, and reply on any marked reply owed."

**Every 55 minutes, as the cache warm (2.2), for SDK and tab sessions alike.**

- `keepalive_due` keeps its conditions, with "waits on the person" widened to "waits on
  the person or has an item in doubt".
- The keepalive text is the check line when any item is in doubt, else today's
  `KEEPALIVE_TEXT`.
- SDK sessions: `host.py` sends it as a turn, as today.
- Tab sessions: the hub sends it as a notice, `KEEPALIVE_AFTER` after the last turn ended
  (the stats snapshot's `last_at`), repeating every 55 minutes under the same conditions,
  never to a parked, dead or mid-turn session. The hub keeps each tab session's quiet-since
  time as the host does for SDK ones.

### After a compaction

Once per compaction, the session is given its open items: every task not settled and
every open question, as `list_items` returns them (refs, statuses and titles, since this
is the moment it has forgotten them), and no agent, decision or permission items.

- SDK sessions: the host sees the compaction end and sends the list with the next turn's
  prompt, or as a notice if the session is quiet.
- Tab sessions: the plugin gains `hooks/hooks.json`, a `SessionStart` hook with matcher
  `compact` running `"${WHEELHOUSE_PYTHON}" -m claude_wheelhouse open-items` (as the
  monitor is run, since `claude-wheelhouse` may not be on the session's path), which prints the list for
  `WHEELHOUSE_SESSION_ID` (nothing when there are none). Claude Code adds a
  `SessionStart` hook's output to the context.

### The keepalive limit, configurable

`KEEPALIVE_FOR` (8 h) becomes a setting, read as `usage-mode` is in `features.py`:

- Command line: `--keepalive-hours N` (`features.KEEPALIVE_FLAG`), on the app and the
  daemon, as `--usage-mode` is.
- Environment and `.env`: `WHEELHOUSE_KEEPALIVE_HOURS`, or `KEEPALIVE_HOURS=N`
  (`features.KEEPALIVE_KEY`, upper case only, as every key is) in the install directory's
  `.env` (the checkout) or `~/.config/claude-wheelhouse/.env`.
- `features.keepalive_hours()`, through `features.given()`: the flag, else the variable,
  else the install directory's `.env`, else the user's, else 8. A positive number, fractions allowed; 0 turns the keepalive off. Any
  other value is said once on stderr and taken as 8.
- The hub reads it for tab sessions, and passes it to each SDK host it starts as
  `--keepalive-hours`. `connect.py` passes it on when it restarts the daemon, beside
  `--usage-mode`.

### Token cost

A request's dominant cost is reading the cached context: at 200k on Opus 5.5 about $0.08,
against about $1.60 for a cold rewrite with a 1-hour cache write.

| Piece | Cost |
|---|---|
| Protocol sentence | About 25 tokens in the cached prefix |
| In-doubt items on a change of work | About 3 tokens an item, only when an item is in doubt; no extra request |
| Check line | About 40 tokens plus 3 an item; replaces the keepalive's 35 |
| A check that finds nothing wrong | One request, as today's keepalive |
| A check that corrects something | Two requests: about $0.16 at 200k |
| Compaction reminder | About 10 tokens an item, once per compaction |

Tab sessions are new spend: up to `keepalive-hours` worth of 55-minute requests per quiet
spell for a session over 150k, at about $0.08 each against the rewrite they avoid.

## What changes where

- `store.py`: `items.session_at`, written by the session's `post_item`, `update_item` and
  `reply`; `Store.in_doubt(sid, now)`, the in-doubt refs as `T1 running`;
  `Store.open_items(sid)` for the compaction list. Bump `PROTOCOL_VERSION` to 11 with its
  comment line.
- `mcp_server.py`: `update_item`, `reply` and `post_item` append `Still open: ...` as
  above. Their docstrings say so.
- `host.py`: the keepalive text from the in-doubt items, `keepalive_due` widened, the
  limit from `--keepalive-hours`, and the compaction list.
- `hub.py`: the tab sessions' keepalive, timed from the stats snapshot, sent by
  `Store.notice`.
- `features.py`, `cli.py`, `connect.py`: the `keepalive-hours` setting and flag.
- `cli.py` and `plugin/hooks/hooks.json`: `claude-wheelhouse open-items` and its hook.
- `protocol.md`: the sentence on still-open lists and one on the check. `protocol_sdk.md`
  and `protocol_tab.md`: the keepalive wording. README: the check, the compaction list and
  `keepalive-hours`. AGENTS.md: `features.py`'s line.
- `.plan/sdk-sessions.md`: the keepalive paragraph, as built.
- Separately, its own issue: `subagents.py` must not read a replayed `SendMessage` (a
  record whose uuid it has already seen, or one older than the agent's end) as a resume.

## Tests

Parameterised, one case per line with its description, using the `clock` fixture for time:

- `test_store`: `in_doubt` for each status and kind (task `running`, `blocked`, `waiting`
  listed; `todo`, `done`, `dropped` not; question listed only when awaiting the session;
  agent, decision and permission never), and for silence (14:59 not listed, 15:00 listed,
  a note from the session resets it, a person's Delete or answer doesn't, an old row
  falls back to `created_at`).
- `test_mcp_server`: the replies of `update_item`, `reply` and `post_item` by call and
  what is in doubt (nothing in doubt, closing one of three, a note only, a question
  answered, a task posted beside a `waiting` one quiet 17 minutes, a question marked
  `reply owed`).
- `test_host`: `keepalive_due` for an item in doubt only, waiting on the person only,
  both, neither, parked, compacted last, past the limit at 8 h and at a configured 2 h,
  off at 0; the text lists the items and falls back when none; the compaction list is
  sent once.
- `test_features`: `keepalive_hours()` from the flag, the variable, the upper-case
  key alone, the install directory's `.env` before the user's, the flag before both, the default, a
  fraction, 0, and a bad value said once and taken as 8.
- `test_cli`: `--keepalive-hours` reaches the hub and is passed on when the daemon
  restarts; `open-items` prints the list, and nothing when none.
- `test_hub` (marked `both`): a tab session's keepalive is sent at 55 minutes under the
  conditions, repeats every 55 minutes up to the limit, and is never sent to a parked,
  dead or mid-turn session.
