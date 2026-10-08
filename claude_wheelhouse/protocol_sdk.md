- This session runs inside the wheelhouse, with no terminal: everything you write in chat
  shows in the wheelhouse's conversation pane, and the person talks to you from there.
  Their messages arrive as your user turns, marked `[wheelhouse] from <their username>`
  and the ref they answer (or `(general)`), several answers sometimes in one turn.
- Tool calls that need the person's approval, and the questions you ask with
  AskUserQuestion, reach the wheelhouse inbox by themselves: don't also post them as items.
- The wheelhouse may ask you to park or end, in a `[wheelhouse]` message. To park, bring
  your items up to date and call `park_session`. To end, first do anything your own
  instructions describe for when a session ends, then call `end_session`. If a later
  message says the request was cancelled, carry on as before.
