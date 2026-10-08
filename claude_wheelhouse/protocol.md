# Wheelhouse protocol

This session was launched from claude-wheelhouse, a sidecar that shows the person every
task, question and subagent status across their parallel Claude Code sessions. These
instructions cover only how to report to the wheelhouse. Your own instructions (CLAUDE.md
and the like) still decide how you work and what you say in chat; where anything here
seems to conflict with them, follow yours.

- Track work in the wheelhouse with the `wheelhouse` MCP tools. `post_item` creates a task
  (`T`), a question (`Q`) or a subagent status (`A`) and returns its ref, such as `Q3`. Put
  the full detail in `body` once; afterwards refer to it by ref. Keep `title` to a few
  words.
- Every question you put to the person gets its own question item, posted with
  `post_item(kind="question")` before or with your chat reply, never only in chat. That
  includes an "A or B?" choice inside a longer reply and a question tacked onto the end of
  a report. Several questions are several items. In chat, refer to them by ref. The person
  reads them in the wheelhouse inbox, away from this chat, so the body must stand on its
  own: name the file, symbol or value, what's already decided, the options, and what each
  answer changes.
- If you join the wheelhouse mid-conversation (adopted, or told so by a `[wheelhouse]`
  notification), post the questions you are already waiting on the person for, and your
  running tasks, as items straight away. Check `list_items` first so you don't post twice.
- Keep statuses current with `update_item`: tasks `todo running blocked waiting done
  dropped`; questions `open answered closed`; agents `running done failed`. Add a `note` for progress worth keeping.
- Don't close a question yourself. Closing is the person's call (they press `x` in the
  wheelhouse), unless the work it unblocked is done.
- The person's answers and hints arrive as notifications from the wheelhouse monitor,
  marked `[wheelhouse] from <their username>`, several answers sometimes in one
  notification. Treat them as if typed in chat. If a notification says it was cut short,
  call the `get_input(message_id=...)` it names for the full text. If it says more
  answers follow, they arrive in the next notification.
- When a message arrives on a ref, answer it with `reply(ref, text, status)` as well as in
  chat, every time. Sending doesn't change a question's status: your reply declares it. On
  a question, `status` is required: `open` while you are still waiting on the person (you
  answered their clarifying question, or need more from them), `answered` only once their
  input lets you proceed. Until you reply, the wheelhouse shows the question as awaiting you.
- Keep a synopsis of this session with `set_synopsis`: two or three sentences on what it
  is doing, set early and updated when its focus shifts.
- `/wheelhouse park` and `/wheelhouse end` handle the session's lifecycle (also
  `/wheelhouse:wheelhouse`). The wheelhouse may ask you to park or end, in a `[wheelhouse]`
  notification; do it as the notification says. If a later one says the request was
  cancelled, carry on as before.
