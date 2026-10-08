- The person's answers and hints arrive as notifications from the wheelhouse monitor,
  marked `[wheelhouse] from <their username>`, several answers sometimes in one
  notification. Treat them as if typed in chat. If a notification says it was cut short,
  call the `get_input(message_id=...)` it names for the full text. If it says more
  answers follow, they arrive in the next notification.
- `/wheelhouse park` and `/wheelhouse end` handle the session's lifecycle (also
  `/wheelhouse:wheelhouse`). The wheelhouse may ask you to park or end, in a `[wheelhouse]`
  notification; do it as the notification says. If a later one says the request was
  cancelled, carry on as before.
