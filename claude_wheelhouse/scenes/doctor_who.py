"""Doctor Who, Flatline (#304, scene G, "It's smaller on the outside!"): compaction is the TARDIS
shrinking on a Bristol estate with the Twelfth Doctor inside it, while the Boneless leach its
dimensions into the mural on the underpass wall. Clara stands by with her handbag and Rigsy
sprays the wall. The OUTSIDE readout is the police box's height: it falls a size at a time, but
never to nothing while it compacts. Once compacted it rides in Clara's handbag, and Rigsy's
painted door has caught the Boneless; if it stalls, a Dalek glides through.

Each excerpt is one line or a two-line exchange, drawn at random without repeats for the length
of one compaction cycle (compactor.schedule).
"""

from __future__ import annotations

from .base import BIG_WIDTH, TICKS, WIDTH, Canvas, Frame, Scene

EXCERPTS: tuple[tuple[str, ...], ...] = (
    ("Doctor: Clara, the TARDIS is getting smaller.", "Clara: Or you're getting bigger."),
    ("Doctor: It's smaller on the outside!",),
    ("Clara: Doctor, I've got the TARDIS in my handbag.", "Doctor: Then mind your lipstick. It's rolled into the console room."),
    ("Doctor: These are attack eyebrows. The summary can have the rest of me.",),
    ("Doctor: I'm Scottish. I'm allowed to complain about the summary.",),
    ("Doctor: Context! I've got new context! I don't like the colour.",),
    ("Doctor: I'm not your boyfriend, Clara. I'm your hand luggage.",),
    ("Doctor: (reads Clara's card) \"So sorry. Your context has been summarised.\"",),
    ("Rigsy: I'm doing community service.", "Clara: So's the summariser. Forty hours of other people's mess."),
    ("Doctor: (walks the tiny TARDIS off the railway line on two fingers)", "Clara: Good boy, Thing."),
    ("Clara: The last Doctor would've put fish fingers and custard in the summary.",),
    ("Doctor: An exceptional summary, Clara. Goodness had nothing to do with it.",),
    ("Rigsy: Who are you, anyway?", "Clara: I'm the Doctor. He's in my bag."),
    ("Doctor: The Boneless are eating our dimensions. A compaction, only rude.",),
    ("Clara: How small does it go?", "Doctor: Ask me again in a sentence. A short one."),
    ("Rigsy: (paints a door on the wall, exactly the size of the summary)",),
    ("Clara: Doctor, can you hear me in there?", "Doctor: Only the summary of you. You sound taller."),
    ("Doctor: Every regeneration I lose a face. Every compaction I lose a Tuesday.",),
    ("Doctor: Clara, the walls are closing in!", "Clara: Wrong film, Doctor. Ours is the box getting smaller."),
    ("Rigsy: Your box is the size of a toaster now, mate.", "Doctor: Don't call her that. She's very sensitive about it."),
    ("Dalek: DALEKS DO NOT COMPACT. DALEKS ARE ALREADY COMPACT.",),
    ("Clara: You're very quiet in there.", "Doctor: I've been summarised to \"Hmm.\""),
    ("Doctor: Small talk? I'm four inches tall, Clara. All my talk is small talk.",),
    ("Boneless: (try to flatten the summary, and find it's already flat)",),
    ("Boneless: (wear the old messages like a stolen coat, two sizes too thin)",),
    ("Boneless: (reach into the third dimension, find nothing worth keeping)",),
    ("Boneless: (roll the old messages up the wall like a carpet)",),
    ("Doctor: Clara, stop swinging the bag!", "Clara: Stop being in the bag, then."),
    ("Clara: Pocket-sized Doctor. I could get used to this.", "Doctor: Don't."),
    ("Doctor: The summary calls me \"a Scottish man in a box\". Accurate. Rude.",),
    ("Clara: I'm late for school.", "Doctor: Take me in. I'll be show and tell."),
    ("Doctor: Clara, did you know a TARDIS fits in a handbag?", "Clara: Every woman knows that."),
    ("Rigsy: (photographs the TARDIS next to his trainer, for scale)",),
    ("Clara: The summary just says \"the Doctor\".", "Rigsy: Doctor who?"),
    ("Doctor: Pass me a smaller screwdriver.", "Clara: You're holding a sonic toothpick."),
    ("Doctor: A good summary has two hearts. One for what we said, and a spare.",),
    ("Clara: You'll love the summary. It cut all your speeches.", "Doctor: All of them?"),
    ("Rigsy: The mural's got everyone we talked to on it.", "Clara: That's not a mural, Rigsy. That's the old context."),
    ("Doctor: I named the Boneless. I'd name the summariser, if it kept names.",),
    ("Clara: What's the plan?", "Doctor: Run. The summary will say we strolled."),
    ("Doctor: Did the summary keep my good coat?", "Clara: It kept your bad mood. That'll keep you warm."),
    ("Doctor: How long was I in there?", "Clara: A whole compaction. You missed nothing. Literally, it's gone."),
    ("Doctor: A Time Lord never forgets, Clara. He keeps a very short summary.",),
    ("Rigsy: (sprays \"THE DOCTOR WAS HERE\", then a smaller one under it)",),
    ("Clara: The TARDIS has gone into siege mode.", "Doctor: That's not siege mode. She's sulking about the summary."),
    ("Doctor: Never be cruel, never be cowardly, and never, ever skip the summary.",),
    ("Doctor: Has anyone ever told you you're bossy, Clara?", "Clara: The summary. Twice."),
    ("Doctor: (knocks four times from inside the handbag)",),
    ("Doctor: The summary dropped the bit where I was right.", "Clara: It kept the bit where I was."),
    ("Clara: (zips the handbag shut so the Doctor can't edit the summary)",),
    ("Clara: Rigsy wants to know if he's in the summary.", "Doctor: Who's Rigsy?"),
    ("Clara: The summary's lost Gallifrey.", "Doctor: Again?"),
    ("Clara: The Boneless are copying your shape.", "Doctor: Flat, grey and furious. They've nailed it."),
    ("Doctor: Clara, I've worked out the compaction. Then it compacted that.",),
    ("Doctor: The TARDIS translates every language, Clara. Even \"the gist\".",),
    ("Doctor: Clara. Do you happen to know how to summarise this thing?",),
    ("Clara: (checks her bag) Phone, keys, purse, TARDIS.",),
    ("Clara: The bag's getting heavier, Doctor.", "Doctor: That's the summary. It's very dense."),
    ("Rigsy: Your mate's in a box in your bag.", "Clara: He's used to it. He lives in a box."),
    ("Clara: You can come out now.", "Doctor: I'd love to. The door's the size of a stamp."),
    ("Clara: (sets the TARDIS on a café table, orders the Doctor a thimble of tea)",),
)


# The cycle's small art (#277): the big art's rows 1 to 9 cut down to WIDTH columns round the
# TARDIS and Clara (from CROP), the readout under them and the caption. Cut from the big still
# itself, so the two always agree, and every row of every stage WIDTH cells
def small(state: str, readout: str) -> str:
    """One stage's small art: the TARDIS and Clara cut from its big still, the readout, then
    the caption."""
    rows = scene(state, 0.0, animate=False)[0][1:GROUND + 1]
    return "\n".join([*(r[CROP:CROP + WIDTH] for r in rows), (" " + readout).ljust(WIDTH), CAPTIONS[state].center(WIDTH)])


# The docked art (#287): the underpass on the estate. Rows: a row for what's said, then the
# Boneless's mural on the wall (left), the TARDIS standing on the pavement with the Doctor's
# scowl at its window (middle, its foot on row FOOT whatever its size), Clara and her handbag,
# Rigsy and his spray can (right), the pavement, the readout and the caption. The police box is
# blue, its lamp amber, the Boneless green, a Dalek gold with a red eye
CAPTIONS = {"preparing": "a routine compaction", "compacting": "smaller on the outside",
            "completed": "compacted, in her handbag", "failed": "stuck, and now a Dalek",
            "cancelled": "the TARDIS grows back"}
SAY, FOOT, GROUND, READOUT = 0, 8, 9, 10
MURAL = 1   # the mural's first column
TC = 26   # the TARDIS's middle column
CLARA, RIGSY = 40, 50   # their first columns
CROP = 19   # the small art's first column
FULL, FLOOR, BAG = 108, 6, 4   # inches tall: a police box, the least it shrinks to while compacting, in the handbag
HALF = 30.0   # seconds a compaction takes to halve the height left above FLOOR
STUCK = 30   # inches tall when it stalled, and where a called-off one starts growing back from
GROW = 3   # seconds a called-off one takes to grow back to FULL
STILL = {"preparing": 2.0, "compacting": 40.0, "completed": 1.0, "failed": 10.5, "cancelled": 1.0}

# the police box a size at a time (rows top to bottom, its foot on FOOT): the tallest it can be
# drawn at each height, its lamp on the top row, and where the Doctor's scowl goes (row, column)
SIZES = (
    (72, ("    _[]_    ",
          " /========\\ ",
          "|POLICE BOX|",
          "|[##]||[##]|",
          "|[##]||[##]|",
          "|    ||    |",
          "|    ||    |",
          "|____||____|"), ((3, 1, "[\\/]"), (4, 1, "[oo]"))),
    (40, ("  _[]_  ",
          " /====\\ ",
          "|POLICE|",
          "|[]||[]|",
          "|  ||  |",
          "|__||__|"), ((3, 1, "\\/"),)),
    (16, (" _[]_ ",
          "/====\\",
          "|[][]|",
          "|_||_|"), ((2, 1, "\\/"),)),
    (0, ("_[]_",
         "|##|"), ()),
)
MURAL_ROWS = ("+------------+",
              "| o   o   o  |",
              "|/|\\ /|\\ /|\\ |",
              "|/ \\ / \\ / \\ |",
              "+------------+")
DALEK = ("  (==)=o",
         " [____]=",
         " |oooo| ",
         "/oooooo\\")
DOOR = (".--.", "|  |", "| o|", "|__|")


def height(state: str, f: int) -> int:
    """The police box's height in inches, f ticks into a stage: full until it moves, then
    falling and never below FLOOR while compacting, BAG once compacted, STUCK if it stalled,
    and growing back from STUCK if called off."""
    if state == "preparing":
        return FULL
    if state == "compacting":
        return round(FLOOR + (FULL - FLOOR) * 0.5 ** (f / (HALF * TICKS)))
    if state == "completed":
        return BAG
    if state == "failed":
        return STUCK
    return min(FULL, STUCK + (FULL - STUCK) * f // (GROW * TICKS))


def size(inches: int) -> tuple[tuple[str, ...], tuple[tuple[int, int, str], ...]]:
    """The police box drawn at a height: its rows and the Doctor's scowl."""
    return next((rows, face) for least, rows, face in SIZES if inches >= least)


def tardis(c: Canvas, inches: int, lamp: str, wave: int | None = None) -> int:
    """The police box at its height, its foot on FOOT and centred on TC, its lamp in role lamp,
    the Doctor scowling out of its window; wave: the Doctor's hand out of the door, waving (the
    tick) once the box is too small for the rest of him. Its first column."""
    rows, face = size(inches)
    top, left = FOOT - len(rows) + 1, TC - len(rows[0]) // 2
    for i, row in enumerate(rows):
        c.put(top + i, left, row, "w")
    c.put(top, left + rows[0].index("[]"), "[]", lamp)
    if "POLICE" in "".join(rows):
        row = next(i for i, r in enumerate(rows) if "POLICE" in r)
        c.put(top + row, left + 1, rows[row][1:-1], "f")
    for row, col, text in face:
        c.put(top + row, left + col, text, "c")
    if wave is not None:
        c.put(FOOT, left + len(rows[0]), "_W" if wave % 2 else "_w", "c")
    return left


def mural(c: Canvas, f: int, boneless: bool) -> None:
    """The mural on the underpass wall, its people flattened into it, and the Boneless rippling
    through it if they're still about."""
    for i, row in enumerate(MURAL_ROWS):
        c.put(3 + i, MURAL, row, "s")
    if boneless:
        for i in range(3):
            c.put(4 + i, MURAL + 1 + (f + 2 * i) % 12, "~", "d")


def drain(c: Canvas, f: int, left: int) -> None:
    """The TARDIS's dimensions leaching across to the mural: dots streaming left."""
    for x in range(MURAL + len(MURAL_ROWS[0]) + 1, left - 1):
        if (x + f) % 3 == 0:
            c.put(6, x, "·", "d")


def clara(c: Canvas, bag: str = "[__]", swing: bool = False, tardis_in: bool = False) -> None:
    """Clara, her handbag on her arm (swinging a column out if swing), the TARDIS peeking out of
    it if tardis_in."""
    c.put(5, CLARA, "{o}", "c")
    c.put(6, CLARA, "/|\\", "c")
    c.put(7, CLARA, "/_\\", "c")
    c.put(8, CLARA, "/ \\", "c")
    at = CLARA + 3 + (1 if swing else 0)
    c.put(7, at, bag, "h")
    if tardis_in:
        c.put(5, at + 1, "·", "k")
        c.put(6, at + 1, "█", "w")


def rigsy(c: Canvas, f: int, spraying: bool) -> None:
    """Rigsy in his hoodie, his can held out and, if spraying, a puff of paint off it."""
    c.put(5, RIGSY, "/o\\", "c")
    c.put(6, RIGSY, "/|\\=", "c")
    c.put(6, RIGSY + 3, "=", "k")
    c.put(7, RIGSY + 1, "|", "c")
    c.put(8, RIGSY, "/ \\", "c")
    if spraying:
        c.put(5 + f % 2, RIGSY + 5 + f % 3, "°", "k")


def dalek(c: Canvas, x: int, f: int) -> None:
    """A Dalek gliding right, its first column x, its eye flashing."""
    for i, row in enumerate(DALEK):
        c.put(5 + i, x, row, "h")
    c.put(5, x + 7, "o", "r" if f % 2 == 0 else "f")


def say(c: Canvas, text: str, mid: int, role: str = " ") -> None:
    """Something said (or done) over someone's head, kept inside the frame."""
    words = text if text.startswith("(") else f'"{text}"'
    c.put(SAY, max(0, min(mid - len(words) // 2, BIG_WIDTH - len(words))), words, role)


def readout(c: Canvas, inches: int, status: str, role: str = "c") -> None:
    """OUTSIDE: the police box's height, as a bar and in feet and inches, then how it's going."""
    full = max(round(10 * inches / FULL), 1)
    c.put(READOUT, 1, "OUTSIDE", "h")
    c.put(READOUT, 9, "[" + "█" * full + "·" * (10 - full) + "]", "c")
    c.put(READOUT, 22, f"{inches // 12} ft {inches % 12:2d} in", "c")
    c.put(READOUT, 35, status, role)


TALK = CLARA + 1   # over Clara's head
PREPARING = (("Routine compaction. Nothing changes size.", TC), ("You always say that.", TALK),
             ("(shakes the spray can)", RIGSY + 1))
TALL = (("Doctor? Your door's gone a bit small.", TALK), ("Not now, Clara. I'm being summarised!", TC),
        ("Is that meant to happen?", RIGSY + 1), ("Nothing he does is meant to happen.", TALK))
SHORT = (("Clara! Pick me up before someone posts me!", TC), ("Right. Handbag.", TALK),
         ("(a hand waves out of the door)", TC), ("Mind the door, Clara. It's my only one.", TC))
FAILED = (("COM-PACT! COM-PACT!", None), ("Oh, that's all we need.", TALK), ("Don't let it near the summary, Clara!", TC))
STATUS = {"preparing": "bigger inside", "compacting": "and shrinking", "completed": "in a handbag",
          "failed": "STUCK. DALEK!", "cancelled": "growing back"}


def scene(state: str, t: float, animate: bool = True) -> Frame:
    """One frame of the big art for a stage, t seconds into it (rows, roles): BIG_HEIGHT rows of
    BIG_WIDTH cells. animate False: the stage's still (STILL), as with the app's animation off."""
    t = t if animate else STILL.get(state, 0.0)
    f = int(t * TICKS)
    c = Canvas()
    beat = f // (2 * TICKS)
    inches = height(state, f)
    lamp = "k" if f % 4 < 2 else "w"
    role = "c"
    c.put(GROUND, 0, "▀" * BIG_WIDTH, "s")
    if state == "preparing":
        mural(c, f, boneless=True)
        tardis(c, inches, "k")
        clara(c)
        rigsy(c, f, spraying=beat % 3 == 2)
        say(c, *PREPARING[beat % len(PREPARING)])
    elif state == "compacting":
        mural(c, f, boneless=True)
        left = tardis(c, inches, lamp, wave=f if inches < SIZES[1][0] else None)
        drain(c, f, left)
        clara(c)
        rigsy(c, f, spraying=True)
        lines = TALL if inches >= SIZES[1][0] else SHORT
        say(c, *lines[beat % len(lines)])
        role = lamp
    elif state == "completed":
        mural(c, f, boneless=False)
        for i, row in enumerate(DOOR):   # Rigsy's painted door, the Boneless caught behind it
            c.put(5 + i, RIGSY + 5, row, "k")
        clara(c, swing=beat % 2 == 1, tardis_in=True)
        rigsy(c, f, spraying=False)
        say(c, *((("Compacted. Fits in my handbag.", TALK), ("Clara, I can hear you smiling.", CLARA + 5))[beat % 2]))
        role = "h"
    elif state == "failed":
        mural(c, f, boneless=True)
        tardis(c, inches, "r" if f % 2 == 0 else "w", wave=f)
        clara(c)
        rigsy(c, f, spraying=False)
        x = (f // 2) % (BIG_WIDTH + 16)   # gliding in from the left edge, out at the right, and round again
        dalek(c, x, f)
        text, mid = FAILED[beat % len(FAILED)]
        if mid is None and x > BIG_WIDTH - len(DALEK[0]):   # the chant only while the Dalek is all in frame
            text, mid = FAILED[1]
        say(c, text, x + 4 if mid is None else mid, "r" if mid is None else " ")
        role = "r" if f % 2 == 0 else "c"
    else:
        mural(c, f, boneless=True)
        tardis(c, inches, "k")
        clara(c)
        rigsy(c, f, spraying=False)
        if t < 4:
            say(c, *((("Called off? Good. I was getting a crick.", TC), ("Back to full size. And full volume.", TALK))[beat % 2]))
    readout(c, inches, STATUS[state], role)
    c.put(READOUT + 1, 0, CAPTIONS[state].center(BIG_WIDTH), " ")
    return c.done()


def small_readout(state: str) -> str:
    """The small art's readout: the police box's height at the stage's still, and how it's going."""
    inches = height(state, int(STILL[state] * TICKS))
    return f"OUTSIDE {inches // 12}'{inches % 12}\" {STATUS[state]}"


WALLS = {state: small(state, small_readout(state)) for state in CAPTIONS}

SCENE = Scene("doctor_who", EXCERPTS, CAPTIONS, WALLS, scene)
