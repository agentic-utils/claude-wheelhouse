---
name: wheelhouse
description: Park or end this wheelhouse session. Use only when the person runs /wheelhouse park or /wheelhouse end (or /wheelhouse:wheelhouse).
argument-hint: park | end
disable-model-invocation: true
---

The person ran `/wheelhouse $ARGUMENTS`. Act on the first word of the arguments.

**`park`**: the person is parking this session, and may come back to it next week.

1. Write your handoff notes into this session's wheelhouse items with `update_item`: where each stands and what comes next, so whoever unparks it can carry on.
2. Call the `wheelhouse` MCP tool `park_session`.
3. That's all: the session stops a moment after `park_session` returns. It stays in the wheelhouse's session list, dimmed, and comes back with Unpark.

**`end`**: the person is ending this session (Stop & Forget). The work is finished and the wheelhouse's data for it will be deleted.

1. If your own instructions describe anything to do when a session ends (such as saving memory), do it first: the wheelhouse keeps nothing for this session afterwards.
2. Call the `wheelhouse` MCP tool `end_session`.
3. That's all: the session stops a moment after `end_session` returns.

**Anything else, or nothing**: do nothing to the session. Reply in one line: usage is `/wheelhouse park` (come back to it later) or `/wheelhouse end` (finished, wheelhouse data deleted).
