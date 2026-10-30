"""Panicked crew and droid chatter shown while a session compacts.

Each excerpt is one line or a two-line exchange, drawn at random without
repeats for the length of one compaction cycle.
"""

from __future__ import annotations

import random

CADENCE = (20, 30)

EXCERPTS: tuple[tuple[str, ...], ...] = (
    ("Han: Whose idea was it to jump in here? Oh right. Hers.",),
    ("Leia: Somebody brace that wall. No, the OTHER wall.",),
    ("Luke: Threepio, do you copy? We are being slowly flattened.",),
    ("C-3PO: Master Luke, the odds of this ending well are 3,720 to 1.",),
    ("Chewie: (a long, worried howl)",),
    ("R2-D2: (frantic whistling)",),
    ("Han: I have a bad feeling about the walls. Specifically the walls.",),
    ("Leia: Everyone stop panicking. That is an order. Panic later.",),
    ("Luke: Artoo, shut down every compactor on the level. Every single one.",),
    ("C-3PO: Oh dear. Oh my. Oh dear me. Did I mention oh dear?",),
    ("Han: Get your furry elbow out of my ear, pal.", "Chewie: (an offended rumble)"),
    ("Leia: Find something long and sturdy.", "Han: Like your patience?"),
    ("Luke: Threepio, pick up! Pick up!", "C-3PO: We are rather busy, sir."),
    ("C-3PO: Artoo says it is definitely the walls.", "R2-D2: (an emphatic beep)"),
    ("Han: This is fine. I've been in tighter spots.", "Leia: Name one."),
    ("Chewie: (a howl that rattles the pipework)", "Han: Yeah, I know, buddy."),
    ("R2-D2: (a long descending raspberry)", "C-3PO: Language, Artoo!"),
    ("Leia: Stand that pole up, wedge it there.", "Luke: It's bending!"),
    ("Han: The walls are moving.", "Leia: Thank you, I had noticed."),
    ("C-3PO: The walls appear to be closing in, sir.", "Luke: Appear?!"),
    ("Han: Is it me or is this room getting cosier?",),
    ("Leia: Wonderful rescue. Truly. Ten out of ten. Very roomy.",),
    ("Luke: Something just brushed my leg. Something with opinions.",),
    ("C-3PO: I was not designed for this sort of thing. Nobody was.",),
    ("Han: Next time we're taking the stairs.",),
    ("Chewie: (a mournful moan, rising in pitch)",),
    ("R2-D2: (a burst of very rude bleeps)",),
    ("Leia: Han, stop shooting the walls. They're metal. Very metal.",),
    ("Han: The blaster bolt went WHERE?", "Luke: Everywhere, Han."),
    ("C-3PO: Artoo, try the next terminal.", "R2-D2: (an exasperated warble)"),
    ("Luke: Threepio, we need that wall stopped NOW.", "C-3PO: Which wall?"),
    ("Han: I told you we should have gone left.", "Leia: You said right."),
    ("Leia: We need a plan.", "Han: I had a plan. You shot a hole in it."),
    ("Chewie: (an outraged yowl)", "C-3PO: I quite agree, Chewbacca."),
    ("R2-D2: (a sad little boop)", "C-3PO: Do not give up yet, Artoo."),
    ("Luke: Artoo, do you hear me? Override the lot!",),
    ("C-3PO: Sir, I calculate we have roughly one metre of hope left.",),
    ("Han: Remind me to never follow a princess down a chute again.",),
    ("Leia: Chewie, push! No, the wall, not Han.",),
    ("Chewie: (a strained, heaving grunt)",),
    ("R2-D2: (a rapid volley of beeps, like a tiny alarm)",),
    ("Han: Hold on, I'm thinking.", "Leia: Do it faster."),
    ("Luke: Threepio, which panel?", "C-3PO: The one with the buttons, sir!"),
    ("C-3PO: Artoo, they are being squashed!", "R2-D2: (a horrified squeal)"),
    ("Han: Chewie, stop howling, I can't hear myself panic.",),
    ("Leia: Nobody touch the water. Nobody look at the water.",),
    ("Luke: I think the walls are gaining on us.",),
    ("C-3PO: How perfectly dreadful. I shall file a complaint.",),
    ("Han: I'd like to speak to whoever designed this room.",),
    ("Leia: This is the least dignified rescue in galactic history.",),
    ("Chewie: (a short bark, then a whimper)",),
    ("R2-D2: (an indignant two-tone whistle)",),
    ("Han: Kid, any bright ideas?", "Luke: Not one, sorry."),
    ("Leia: Grab that pipe!", "Han: Which pipe? They're all pipes!"),
    ("C-3PO: The odds of successfully navigating a trash compactor...",
     "Han: Whatever the number is, keep it to yourself."),
    ("Luke: Artoo, it's all on you, mate.", "R2-D2: (a nervous trill)"),
    ("Chewie: (a doleful whine)", "Leia: Chewie, it's going to be fine."),
    ("Han: It's going to be fine.", "Chewie: (a deeply sceptical grumble)"),
    ("R2-D2: (a triumphant whistle)", "C-3PO: Artoo! You found it!"),
    ("R2-D2: (an apologetic burble)", "C-3PO: What do you mean, wrong level?"),
    ("Luke: Threepio, are you there?", "C-3PO: Regrettably, sir, yes."),
    ("Leia: This wall has no sense of personal space.",),
    ("Han: Walls closing in. Story of my life, honestly.",),
    ("Luke: Can anyone else hear the walls laughing?",),
    ("C-3PO: Artoo, I do not care what the manual says, stop it!",),
    ("Han: Princess, your plan is putting the squeeze on us.",),
    ("Leia: Han, if you make one more wall pun I'll let it win.",),
    ("Han: I'm just saying, it's a tight squeeze.", "Leia: Han."),
    ("Han: Things are really closing in on us.", "Leia: HAN."),
    ("Luke: At least the walls are making progress. Someone should.",),
    ("C-3PO: I have never been so close to the end of anything.",),
    ("Chewie: (a howl that clearly blames Han)",),
    ("Han: Don't look at me like that, Chewie. This was her idea.",),
    ("Leia: Everyone, up on the junk! Higher!", "Luke: It's all wobbly!"),
    ("R2-D2: (a whistle that sounds a lot like a sigh)",),
    ("C-3PO: Artoo, hurry, they are running out of room!",
     "R2-D2: (a flurry of determined beeps)"),
    ("Luke: Threepio, I'll owe you an oil bath for this.",
     "C-3PO: I shall hold you to that, sir."),
    ("Han: Hey, laser brain, stand on something useful.",
     "Luke: Like what?"),
    ("Leia: Why is the floor squelching?", "Han: Don't ask the floor."),
    ("Chewie: (a gurgling, alarmed roar)",
     "Han: I know, I saw it too. Ignore it."),
    ("C-3PO: Shall I tell them about the odds, Artoo?",
     "R2-D2: (an urgent, negative blat)"),
    ("Luke: Compactor's slowing down!", "Han: No it isn't."),
    ("Luke: Compactor's speeding up!", "Han: Yeah, that one's right."),
    ("Leia: Han, you're standing on my foot.", "Han: You're standing on mine!"),
    ("Han: Fly casual. Be calm. Totally calm. AAARGH.",),
    ("Leia: When we get out of this, someone owes me new boots.",),
    ("Luke: Uncle Owen never mentioned anything like this.",),
    ("C-3PO: I suspect this is what the humans call a pickle.",),
    ("C-3PO: Artoo, I am beginning to think nobody reads my reports.",),
    ("R2-D2: (a long, wobbling whistle of despair)",),
    ("Chewie: (a pleading, sing-song moan)",),
    ("Han: Right, new plan: we all breathe in.",),
    ("Leia: Squeeze up, everyone. Actually, don't. Very much don't.",),
    ("Luke: Threepio, tell Artoo to hurry. Politely. Then not politely.",),
    ("C-3PO: Artoo, they would like you to hurry, please.",
     "R2-D2: (an affronted squawk)"),
    ("Han: Could be worse.", "Chewie: (a howl as the walls lurch again)"),
    ("Leia: Who keeps a pet in a rubbish bin?", "Luke: Not a pet. Not a pet!"),
    ("R2-D2: (a smug little chirp)",
     "C-3PO: Do not gloat, Artoo, they are not out yet."),
    ("Luke: Wait, the walls stopped!", "Han: Don't jinx it, kid."),
    ("Han: Next rescue, I'm picking the exit.",
     "Leia: Next rescue, I'm picking the rescuer."),
    ("C-3PO: Sir, Artoo sends his regards and a schematic.",
     "Luke: Just stop the walls!"),
    ("Chewie: (a grumbling growl, muffled by garbage)",),
    ("Leia: I'd like it on record: I could have stayed in the cell.",),
    ("Han: This wasn't in the brochure.",),
    ("Luke: How long does this thing take to tidy up anyway?",),
)


def gap(rng: random.Random) -> float:
    """Return seconds to wait before the next excerpt."""
    return rng.uniform(*CADENCE)


class Excerpts:
    """Deal excerpts without repeats for one compaction cycle."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self._order: list[int] = []

    def next(self) -> tuple[str, ...]:
        """Return an excerpt not yet given, reshuffling once all are used."""
        if not self._order:
            self._order = list(range(len(EXCERPTS)))
            self._rng.shuffle(self._order)
        return EXCERPTS[self._order.pop()]


# The cycle's stage lines (#244, art B, the detention-level trash compactor): the walls close as
# it goes. Plain Text in the compaction blue, never Markdown; each line 36 cells or fewer
WALLS = {
    "preparing": "▐█          █▌ walls closing in",
    "compacting": "▐████    ████▌ compaction initiated",
    "completed": "▐████████████▌ compacted",
    "failed": "▐█          █▌ 3PO, shut them down!",
    "cancelled": "▐█          █▌ compaction called off",
}
SHOWN = ("preparing", "compacting")   # the states the crew panic through


def stage_text(state: str, origin: str | None, note: str) -> str | None:
    """A stage's two lines: the art, then (dim) who asked or what came of it. None for a stage
    with no line (requested: usually under a second, and the eye covers it)."""
    if state not in WALLS:
        return None
    detail = note or (f"asked for by {origin}" if state in SHOWN and origin else "")
    return WALLS[state] + (f"\n{detail}" if detail else "")


def schedule(cycle_id: str, windows: list[tuple[float, float]]) -> list[tuple[float, tuple[str, ...]]]:
    """The crew's excerpts for one cycle, as (epoch second, excerpt): one at the start of each
    window (a stage it panics through, from its start to its end or now), then one every
    CADENCE seconds within it. Seeded by the cycle's id, so every refresh and every app shows
    the same lines at the same times, none repeated within the cycle."""
    rng = random.Random(cycle_id)
    pool, out = Excerpts(rng), []
    for start, end in windows:
        at = start
        while at <= end:
            out.append((at, pool.next()))
            at += gap(rng)
    return out
