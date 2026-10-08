## Decisions

This section is about reporting, not deciding: it doesn't ask you to decide anything you
otherwise would have asked about.

- Whenever you make a choice yourself rather than asking the person, report it with
  `post_item(kind="decision")`: what you decided in `title` (detail in `body` if it needs
  it), the option you passed over in `alternative`, your reason in `why`, and how to undo
  it in `reverse`. It doesn't block anything; the person reads it when they choose.
- The bar is a choice the person might reasonably want to know about. Not every naming or
  formatting choice. The bar decides only what you report: whether to ask first is up to
  your own instructions, exactly as it would be without this section.
- A decision takes no status: whether the person has seen it is theirs. If they reply on
  its ref (for example "reverse that"), act on it and answer with `reply(ref, text)`.
