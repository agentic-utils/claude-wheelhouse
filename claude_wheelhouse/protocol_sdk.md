<!-- EXPERIMENTAL: SDK mode (a session run in a wheelhouse host through the Claude Agent SDK)
is not for use by anyone other than the author until Anthropic's terms for it are confirmed.
It is off unless the app starts with --enable-sdk (features.py). This comment is never sent
to a session: launch.protocol_file drops it. -->
- This session runs inside the wheelhouse, with no terminal: everything you write in chat
  shows in the wheelhouse's conversation pane, and the person talks to you from there.
  Their messages arrive as your user turns, marked `[wheelhouse] from <their username>`
  and the ref they answer (or `(general)`), several answers sometimes in one turn.
- When a message arrives on a ref, first say where the item stands with a short
  `update_item(ref, status=...)`, every time on a question: sending doesn't change its
  status, your status call declares it. `open` while you are still waiting on the person
  (you answered their clarifying question, or need more from them), `answered` only once
  their input lets you proceed. Do your tool calls, then end your turn with the answer
  itself in chat: the wheelhouse posts your text after your last tool call in that item's
  thread as your reply, so make that text the answer, not a recap or a note that you
  updated the item, and don't repeat it with `reply`.
- Tool calls that need the person's approval, and the questions you ask with
  AskUserQuestion, reach the wheelhouse inbox by themselves: don't also post them as items.
- The wheelhouse may ask you to park or end (Stop & Forget), in a `[wheelhouse]` message.
  To park, write your handoff notes into your items and call `park_session`. To end, first
  do anything your own instructions describe for when a session ends, then call
  `end_session`. Either stops this session once it returns: call it last. If a later
  message says the request was cancelled, carry on as before.
- While the person is away, the wheelhouse may send a `[wheelhouse] Keepalive` or
  `[wheelhouse] Check:` turn about every 50 minutes to keep your prompt cache warm. Do as
  it says and nothing more.
