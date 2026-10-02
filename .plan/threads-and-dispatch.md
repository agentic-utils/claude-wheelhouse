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
| Ctrl+X (in a compose box) | send this answer alone, now, for something urgent |

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
inline limit (1,500 characters), the monitor prints what fits and points to `get_input()`
for the rest, as it does for a single long message today.

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
- `send()` gains `draft=True` by default; Ctrl+X and the End/Park cancellation messages
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
