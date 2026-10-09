# Question threads and batched dispatch (design)

Issue #11. Two changes to how the person and a session talk about an item: a real
conversation per question, and answers that go out together instead of one at a time.

## Why

Answering a question today sends one message straight away, and the session wakes and
acts on it. Answers often bear on each other: a session that reads the answer to Q3 alone
may start work before it sees Q4's, which changes the plan. One composite message per
session lets it read everything first, and costs one turn instead of several.

The thread under an item already holds both sides (the person's messages and the
session's notes), but nothing asks the session to answer there, so the thread shows half
the conversation and the rest lives only in chat.

## Part 1: a conversation per question

**A `reply` tool.** `reply(ref, text)` adds the session's answer to an item's thread. It is
separate from `update_item(note=...)`: a note records progress, a reply is part of the
conversation. The store marks which is which, so the thread view can show replies as
conversation and notes as quieter progress lines.

**Follow-ups reopen the question.** `reply(ref, text, asks=false)` takes an optional `asks`
flag. When the session's reply asks the person something back, it sets `asks=true` and the
question moves back to `open`, so it returns to the top of the inbox. The conversation stays
on one ref rather than spreading across new questions.

**Protocol line.** One line in protocol.md: "When a message arrives on a ref, answer it with
`reply` as well as in chat." It says how to report, not what to do, in keeping with the
rest of the protocol.

**Thread view.** Enter on an item opens it full screen: the item's title, status and
session at the top, its body, then the thread oldest first, and a compose box at the
bottom. Esc goes back to the inbox. The view refreshes on the app's existing tick, so a
reply appears while it's open. The side pane on the inbox stays as a preview.

**Delivery.** Unchanged in shape: `[wheelhouse] from doug on Q1: ...`, where `doug` is the
OS user (#10).

## Part 2: batched dispatch

**Queue, then send.** Finishing an answer queues it rather than sending it. Queued answers
sit in an outbox until the person sends them:

| Key | Does |
|---|---|
| Ctrl+S (in a compose box) | queue this answer |
| `s` | send the selected session's queued answers as one message |
| `S` | send every session's queued answers, one message per session |
| Ctrl+Enter (in a compose box; terminals that send it as a line feed give ctrl+j, also bound) | send this answer alone, now, for something urgent |

The tabs move from `i` and `s` to `1` (Inbox) and `2` (Sessions), with `i` kept for the
inbox, so `s` and `S` are free to send.

`S` doesn't ask for confirmation. It's the action the outbox exists for, and a prompt every
time would train people to press Enter without reading. The notification says what went
where instead.

There is no automatic send after a quiet spell. It would send a half-finished set of
answers while the person checks something, which is exactly the surprise this design is
meant to remove.

**Seeing what's queued.** Each session in the left list shows a badge with its count,
such as `✉ 3`, and the footer shows the total with the key to send. A queued answer shows
in its thread marked "queued", where it can be edited (back into the compose box) or
dropped. A question with a queued answer shows the status "queued" in the item list; the
stored status stays `open` until the answer is sent, when it becomes `answered` as today.

**Composite message.** The monitor prints one notification per send, on one line as now
(newlines flattened), with a block per item in thread order:

```
[wheelhouse] from doug, 3 answers: on Q3: use SQLite ‖ on Q4: yes, keep the flag ‖ (general): ship it tonight
```

A send is one transaction, and the monitor's claim takes every sent message for the
session at once, so a session never sees part of a batch. If the line would pass the
line limit, the monitor prints what fits and points to `get_input()` for the rest, as it
does for a single long message today.

To check during the build: how Claude Code's monitor turns stdout into notifications. If
two lines printed together already arrive as one notification, the monitor could print a
line per item instead; one line is the safe choice until that's confirmed.

## Store changes

- `messages.draft INTEGER NOT NULL DEFAULT 0`. A queued answer is a row with `draft = 1`.
  Sending sets `draft = 0` on the session's drafts in one transaction. The default keeps
  every existing row as sent, so the column is added on start like the others, with no
  backfill.
- `messages.kind TEXT` for the session's messages: `note` from `update_item`, `reply` from
  `reply`. Existing rows read as notes.
- `claim`, `pending` and `get_input` skip drafts. The pending-messages index gains
  `AND draft = 0`.
- `send()` gains `draft=True` by default; Ctrl+Enter and the End/Park cancellation messages
  pass `draft=False`. Marking a question answered moves from writing the message to
  sending it.
- Half-typed text in a compose box is not stored. It lives in the app until queued, and is
  lost if the app quits; a queued answer survives a restart.

## Edge cases

- **The session closes a question that has a queued answer.** The answer stays queued and
  its thread shows the question is now closed, so the person can drop it. If sent anyway,
  it is delivered like any other message.
- **The session is parked or dead.** Sending is allowed. The messages wait in the store
  and the monitor delivers them when the session is restored, as unsent messages do today.
  The notification after sending says so.
- **The session is ended.** Its drafts go with it (the session row cascades). The End
  confirmation names the count: "End Rare caper? 3 queued answers will be discarded."
- **The app quits with answers queued.** They stay queued for next time.
- **A reply arrives while the person is typing in the thread view.** It appears in the
  thread above the compose box; the typed text is untouched.

## As built

Where the build differs from the design above:

- **Send mode per session, and the keys (#38).** Each session has a send mode, Queued or
  Immediate (`sessions.send_mode`; NULL means `store.DEFAULT_MODE`, `"queued"`). Ctrl+Enter
  (and `ctrl+j`, as Windows Terminal sends it) submits: queued, or sent at once in
  Immediate mode. Ctrl+T switches the session in context. Ctrl+S sends that session's
  queue. Send all is a button only: Windows Terminal sends Ctrl+Shift+S and Ctrl+Alt+S as
  plain Ctrl+S. A bar above the footer, on the inbox and in the thread view, always
  shows Send all (n), disabled while every queue is empty; its buttons take no focus. The
  mode button and Send (n) were in it too, until #59 moved them under the session list,
  for the current session, the one highlighted there, which is also the one the keys act
  on (D20 in `wheelhouse-sessions.md`); the thread view's bar keeps them, for its session.
  The session in context is the thread's, or the inbox's current session, which follows
  the filter or the selected item's session. The `s`, `S` and `i`
  keys are gone, and keys are shown in upper case, the usual convention.
- **Store calls.** `send()` still sends at once (Immediate mode and the End/Park
  cancellations use it). Queuing is a separate `queue()`, and `dispatch(sid)` sends a session's drafts in one
  transaction. `unqueue()` takes a draft back.
- **Editing or dropping a queued answer.** Ctrl+R in a compose box takes the item's latest
  queued answer back into the box, to edit and queue again, or to clear and so drop.
- **One notification per poll.** The monitor prints everything sent since its last poll
  as one line (a single message reads as before). Two sends within one poll, or sends
  that waited for a dead session, share a line. A batch is split only when it won't fit:
  the line stays within 480 characters, blocks that don't fit are left for the next
  poll, and the line says how many follow. Claude Code cuts a monitor notification at 500
  characters (appending "...(truncated)"), so anything past that never reaches the
  session. Short messages go whole; long ones share what's left, each showing at least 40
  characters.
- **Cut-short messages** lead with their pointer, `[cut short, full text:
  get_input(message_id=N)]`, so it survives any cut, which returns that message in
  full whether or not it has been delivered. The monitor confirms a message as it prints
  it, so a pointer to "what's undelivered" could never find a general message's rest.
- **The session declares a question's status (replaces `asks`).** Sending no longer
  marks a question answered: the person's message may itself be a clarifying question.
  `reply(ref, text, status)` requires `status` on a question, `open` (still waiting on
  the person) or `answered` (their input lets the session proceed); on a task or agent it
  is optional. While the person's message is the latest on an item, with no reply since,
  the item list shows it dimmed as `⏳ <status>` (derived, `Store.awaiting()`). The old
  `items.reopened_after` column is no longer used; it stays because MCP servers still
  running older code write it. `PROTOCOL_VERSION` is 3, so those sessions show "needs
  relaunch".
- **Sessions on older code.** An MCP server or monitor started before drafts existed
  delivers a draft at once, and it keeps its old code until the session restarts. Each
  session row now carries `code_version`, stamped with `PROTOCOL_VERSION` when the
  session registers, by the MCP server's heartbeat and when the monitor starts. A running
  session with an older or missing stamp shows "needs relaunch". Only one stamped before
  drafts (below `DRAFTS_VERSION`, 2, or unstamped) has Ctrl+Enter send to it at once
  (with a warning) instead of queuing: later versions all hold drafts. Bump
  `PROTOCOL_VERSION` whenever older running code would mishandle the store.
- **Unsent text and refreshes.** Typed text is kept per row and swapped only when the
  person moves the highlight. The once-a-second refresh changes table cells in place
  when the rows are the same, and when it does rebuild a table it posts no
  `RowHighlighted` (a rebuild's `clear()` puts the cursor on row 0, and the first new row
  used to announce that as a selection, swapping the answer box out and back every second
  and losing its cursor and selection).
