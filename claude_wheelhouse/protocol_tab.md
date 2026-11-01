- When a message arrives on a ref, answer it with `reply(ref, text, status)` as well as in
  chat, every time. Sending doesn't change a question's status: your reply declares it. On
  a question, `status` is required: `open` while you are still waiting on the person (you
  answered their clarifying question, or need more from them), `answered` only once their
  input lets you proceed. Until you reply, the wheelhouse shows the question as awaiting you.
- The person's answers and hints arrive as notifications from the wheelhouse monitor,
  marked `[wheelhouse] from <their username>`, several answers sometimes in one
  notification. Treat them as if typed in chat. If a notification says it was cut short,
  call the `get_input(message_id=...)` it names for the full text. If it says more
  answers follow, they arrive in the next notification.
- `/wheelhouse park` and `/wheelhouse end` handle the session's lifecycle (also
  `/wheelhouse:wheelhouse`). The wheelhouse may ask you to park or end (Stop & Forget), in a
  `[wheelhouse]` notification; do it as the notification says. If a later one says the
  request was cancelled, carry on as before. While it is parking, compacting or ending this
  session, `end_session` and `park_session` are refused but for the one it asked for.
  `park_session` and `end_session` stop this
  session a moment after they return: call them last.
- While the person is away, the wheelhouse may send a `[wheelhouse] Keepalive` or
  `[wheelhouse] Check:` notification about every 50 minutes to keep your prompt cache warm.
  Do as it says and nothing more.
