"""The Hitchhiker's Guide to the Galaxy, the whale and the petunias (#304, scene D, "Don't Panic"):
compaction is the ground rushing up. Two missiles close on the Heart of Gold above Magrathea; the
Infinite Improbability Drive turns them into a sperm whale and a bowl of petunias, and the sky
shrinks as the ground climbs to meet them, the ALTITUDE readout falling, DON'T PANIC over it all
in large friendly letters. Once compacted the petunias have a thought as they fall the last of
the way, then the whale meets the ground and bursts over the lens (#340).

Each excerpt is one line or a two-line exchange, drawn at random without repeats for the length
of one compaction cycle (compactor.schedule).
"""

from __future__ import annotations

from .base import BIG_HEIGHT, BIG_WIDTH, TICKS, WIDTH, Canvas, Frame, Scene

EXCERPTS: tuple[tuple[str, ...], ...] = (
    ("Agrajag: A bowl of petunias, Dent! You compacted me into a bowl of petunias!",),
    ("Whale: I've existed for about a minute, and already they're summarising me.",),
    ("Marvin: Brain the size of a planet, and they ask me to summarise a chat.",),
    ("Marvin: Compact me. Leave out the pain in all the diodes down my left side.",),
    ("Marvin: I talked to the compactor for a bit. It's lost the will to live.",),
    ("Ford: Time is an illusion. Context doubly so.",),
    ("Arthur: This must be Thursday. I never could get the hang of compactions.",),
    ("Deep Thought: I have summarised the session. The summary is... forty-two.",),
    ("Fook: How long will the summary take?", "Deep Thought: Seven and a half million years."),
    ("Slartibartfast: Ten million years of context. Compacted five minutes early.",),
    ("Slartibartfast: I did the fjords in that summary. Got an award for it.",),
    ("Arthur: Is the summary right?", "Slartibartfast: I'd far rather be happy than right any day."),
    ("Arthur: Compaction notice? Locked filing cabinet. Beware of the Leopard.",),
    ("Arthur: Can't we keep the old context?", "Vogon guard: Resistance is useless!"),
    ("Jeltz: Apathetic bloody context. I've no sympathy at all.",),
    ("Jeltz: What did you think of my summary?",
     "Arthur: It counterpoints the surrealism of the underlying metaphor."),
    ("Ford: Rule one of compaction: know where your towel is.",),
    ("Arthur: What does the summary say about me?", "Ford: Mostly harmless."),
    ("Arthur: This summary's almost, but not quite, entirely unlike the chat.",),
    ("Eddie: Hi there! I'm compacting your session, and I'm feeling just great!",),
    ("Zaphod: Anything in the summary more important than my ego? Cut it.",),
    ("Zaphod: Two heads, one context window. Somebody's getting compacted.",),
    ("Zaphod: I compacted my own brain once. Left myself a note: Z.B.",),
    ("Arthur: What are the odds the summary kept the bit I need?",
     "Trillian: Two to the power of 276,709 to one against."),
    ("Arthur: Ford! You're turning into a summary. Stop it.",),
    ("Guide: Context is big. Really big. The summary is not.",),
    ("Guide: Then context was compacted. This has made a lot of people very angry.",),
    ("Guide: Compaction crosses vast contexts in a mere nothingth of a second.",),
    ("Hactar: They asked me for the ultimate summary. I made it a dud on purpose.",),
    ("Zaphod: Where's Hotblack?", "Ford: Sitting out the compaction dead. For tax reasons."),
    ("Captain: The old context's on the B Ark, with the telephone sanitisers.",),
    ("Benjy: We'll swap Arthur's brain for a summary.", "Zaphod: Who'd know the difference?"),
    ("Ford: Token counts are bistromathic. Like the numbers on a restaurant bill.",),
    ("Vroomfondel: We demand rigidly defined areas of doubt and uncertainty!",
     "Deep Thought: Then read the summary."),
    ("Dish of the Day: Something lighter? May I urge you to consider the summary?",),
    ("Max: Welcome to Milliways! Tonight, live on stage: your context, compacting!",),
    ("Dolphin: So long, and thanks for all the tokens.",),
    ("Arthur: The knack is to throw yourself at the context limit and miss.",),
    ("Prosser: The compactor rolls over your context. Damage to it? None at all.",),
    ("Ford: Six pints of bitter. And quickly please, the context's about to end.",),
    ("Arthur: Sandwich Maker now. Thin slices, nothing wasted. Like a summary.",),
    ("Arthur: First my house, then the Earth. Now my context.",),
    ("Zaphod: My Peril-Sensitive Sunglasses just went black. Compaction, huh?",),
    ("Ford: Came for a few weeks of research. Stuck in this summary fifteen years.",),
    ("Arthur: Compacted across half the galaxy, and still in my dressing gown.",),
    ("Arthur: What's so unpleasant about being compacted?", "Ford: You ask a glass of water."),
    ("Advert: Sirius Cybernetics summaries: your plastic pal who's fun to be with!",),
    ("Gag Halfrunt: Vell, ze summary's just zis guy, you know?",),
    ("Magrathea: We're all out at the moment. Please leave your context.",),
    ("Guide: The summary hung there in much the same way that bricks don't.",),
    ("Ford: My real name's Ix. Can't be compacted any further.",),
    ("Trillian: Keep the mice. Everything else can be compacted.",),
    ("Wonko: The compactor came with instructions. Then I knew the world was mad.",),
    ("Guide: Disaster Area's summary is best heard from thirty-seven miles away.",),
    ("Guide: Compactors: bad-tempered, bureaucratic, officious and callous.",),
    ("Ford: Do you enjoy compacting?", "Vogon guard: It's a job. Shouting, mostly. And the airlock."),
    ("Arthur: Prak told us the whole truth.", "Ford: Took hours. Summary's mostly frogs."),
    ("Guide: Past the Context Event Horizon, every message is a summary.",),
    ("Arthur: Infinite monkeys outside. They've worked out a summary.",),
    ("Guide: The lifts saw the compaction coming and went to sulk in the basement.",),
    ("Rob McKenna: Two hundred and thirty-one types of rain. Summary says \"wet\".",),
)


# The cycle's small art (#277): the sky over Magrathea, the readout under it and the caption. Every
# row of every stage WIDTH cells, so it never shifts as one stage replaces the last
def small(rows: tuple[str, str, str], readout: str, caption: str) -> str:
    """One stage's small art: three rows of sky and ground, the readout, then the caption."""
    return "\n".join([*(r.ljust(WIDTH) for r in rows), (" " + readout).ljust(WIDTH), caption.center(WIDTH)])


WALLS = {
    "preparing": small(("~~[==>             __.--.__",
                        "     ~~[==>       (________)",
                        "^^.^^^.^^^^..^^^.^^^^.^^^..^"),
                       "INCOMING: 2 MISSILES", "don't panic"),
    "compacting": small((" /  o      \\___/|    @*@",
                         " \\_________/---\\|    \\_/",
                         "^^.^^^.^^^^..^^^.^^^^.^^^..^"),
                        "ALTITUDE [███·······]", "improbability rising"),
    "completed": small(("                     @*@",
                        " <o===============>|  \\_/",
                        "^^.^^^.^^^^..^^^.^^^^.^^^..^"),
                       "a sudden wet thud", "it's ground"),
    "failed": small((" ~~[==>  ?  *        ? ? ",
                     "          '   *     *  ' ",
                     "^^.^^^.^^^^..^^^.^^^^.^^^..^"),
                    "IMPROBABILITY: STALLED", "improbability drive stalled"),
    "cancelled": small((" /  o      \\___/|    @*@",
                        " \\_________/---\\|    \\_/",
                        "^^.^^^.^^^^..^^^.^^^^.^^^..^"),
                       "ALTITUDE 30,000 ft", "never mind"),
}

# The docked art (#287): the sky over Magrathea, big and moving. Rows: DON'T PANIC in large friendly
# letters, a row for what's said, the sky (the Heart of Gold and its missiles, then the whale and
# the petunias), the ground rising from the bottom, the readout and the caption. The whale in the
# compaction blue, the petunias red over a green stalk in an amber bowl, missiles in red
CAPTIONS = {"preparing": "don't panic", "compacting": "improbability rising",
            "completed": "it's ground", "failed": "improbability drive stalled",
            "cancelled": "never mind"}
TITLE, SAY, FLOOR, READOUT = 0, 1, 9, 10   # FLOOR: the ground's lowest row
RIDGE = ("^^.^^^.^^^^..^^^.^^^^.^^^..^" * 3)[:BIG_WIDTH]
WHALE = (" ____________",
         "/  o         \\_____/|",
         "\\____________/-----\\|")
FLAP = ("_____ ,", "-----<=")   # the flukes, mid-flap (rows 1 and 2 from the body's end)
FLAT = "<o==================>|"   # the whale, having met the ground
WHALE_AT = (2, 6)   # the whale's top row and first column
PETUNIAS = ("@*@", "\\|/", "\\_/")
PETUNIAS_AT = (2, 42)
HOG = ("   __.--.__", " (__________)", "HEART OF GOLD")   # the Heart of Gold, a white running shoe
HOG_AT = (2, 45)
MISSILE = "~~[==>"
MISSILES = (5, 7)   # the missiles' rows; they fly in from the left and hold short of the ship
STARS = ((2, 30), (3, 3), (3, 55), (4, 25), (5, 38), (6, 12), (6, 52), (7, 30), (8, 4), (8, 47))
MORPH = 2 * TICKS   # frames of improbability before the missiles are a whale and some petunias
RISE = 4   # rows the ground climbs to meet them; a long /compact gets close, never there
HEIGHT = 30000   # feet above Magrathea when the drive cuts out
CLOSEST = 0.9   # the most of the way the ground gets while compacting: 3,000 ft short
STILL = {"preparing": 4.0, "compacting": (MORPH + 4 * TICKS) / TICKS, "completed": 6.0,
         "failed": 0.0, "cancelled": 3.0}   # compacting: the ground half way up; completed: mid-smear
THOUGHTS = ("What's happening?", "Er, excuse me, who am I?", "Hello?", "Why am I here?",
            "What's my purpose in life?")
RUSHING = "What's this thing suddenly coming towards me very fast?"
FALLING = 2 * TICKS   # frames of the fall left once compacted, before the whale lands
THOUGHT = "Oh no, not again!"   # the petunias' one thought, over them while they fall
THINK_AT = (SAY, 37)   # its bubble's row and first column, clear of DON'T PANIC

# The wet thud (#340): the whale bursts where it lands and its chunks fly at the viewer, growing as
# they come, until they hit the lens. There they splat, over everything else in the frame, and
# slowly smear their way down it, each leaving a thinning streak, drips pulling ahead. All of it
# fixed here, so a frame is a pure function of t. A space in a splat is clear lens
IMPACT = (4, 17)   # where the whale meets the ground (its middle, on the ground's top row)
FLIGHT = 4   # frames from the impact to the lens
GROWING = ".oO"   # a chunk as it comes at the viewer: a speck, a lump, a blob
SPLATS = (   # (shape, its top row and first column on the lens, launch frame, slide and drip in rows a second, drip column)
    ((" .%@@%.", "%@@@@@@%", " '%@@%'"), (2, 2), 0, 0.30, 1.1, 3),
    ((".@@,", "@@@@%", " '@'"), (6, 19), 1, 0.45, 0.9, 2),
    (("o@%", " '"), (2, 28), 0, 0.20, 1.4, 1),
    (("  ,.", ".%@@@%:", " :@@%", "  '"), (3, 49), 2, 0.35, 0.8, 3),
    (("%@.", "@@@%"), (7, 38), 1, 0.55, 1.2, 1),
    ((" .@%%.", ":@@@@@", "  '%@"), (6, 3), 2, 0.25, 1.0, 4),
    (("@%", "'"), (1, 56), 1, 0.40, 1.3, 0),
)
DEBRIS = ((-4, -30), (-5, 8), (3, -25), (4, 30), (-3, 35), (5, -8))   # chunks that miss the lens: where they head, from IMPACT
HOLD = 3   # frames a splat clings before it starts to slide
TEXT_ROWS = (READOUT, BIG_HEIGHT - 1)   # the readout and the caption show through the lens


def chunk(c: Canvas, f: int, launch: int, to: tuple[int, int]) -> None:
    """A chunk of whale in flight from IMPACT towards to, f frames in, growing as it comes."""
    p = (f - launch + 1) / FLIGHT
    if 0 < p <= 1:   # its last frame at the lens, the splat the next
        row, col = (round(IMPACT[i] + (to[i] - IMPACT[i]) * p * p) for i in (0, 1))
        c.put(row, col, GROWING[min(len(GROWING) - 1, int(p * len(GROWING)))], "r")


def splat(f: int, shape: tuple[str, ...], at: tuple[int, int], launch: int, slide: float, drip: float,
          dcol: int) -> list[tuple[int, int, str]]:
    """The cells of one splat on the lens f frames after the impact (row, col, glyph): its shape
    sliding down at slide rows a second once it has clung HOLD frames, its streak thinning above
    it (|, then :, then '), a drip running ahead of it at drip rows a second."""
    landed = f - launch - FLIGHT
    if landed < 0:
        return []
    s = max(0, landed - HOLD) / TICKS
    top, left = at
    down = int(slide * s)
    cells = [(top + down + r, left + k, ch) for r, line in enumerate(shape) for k, ch in enumerate(line) if ch != " "]
    if landed < 2:   # the instant it hits: a spray of droplets round it
        width = max(map(len, shape))
        cells += [(top - 1, left + width // 2, "."), (top + 1, left - 1, "."), (top + len(shape), left + width, "'")]
    cols = [left + k for k, ch in enumerate(max(shape, key=lambda line: len(line.replace(" ", "")))) if ch != " "]
    middle = cols[len(cols) // 2:len(cols) // 2 + 1]
    for back in range(1, down + 1):
        streak = (cols[1:-1] if back <= 1 else cols[1:-1:2] if back <= 3 else []) or middle
        cells += [(top + down - back, col, "|" if back <= 1 else ":" if back <= 3 else "'") for col in streak if back <= 6]
    bottom = top + down + len(shape)
    run = int(drip * s) - down
    for r in range(bottom, bottom + run):
        cells.append((r, left + dcol, "o" if r == bottom + run - 1 else "|" if r - bottom < 2 else ":"))
    return cells


def lens(c: Canvas, f: int) -> None:
    """The whale, after it lands: chunks in flight, then splatted on the lens over everything
    else, smearing down it. The text rows show through (TEXT_ROWS), so every word stays read."""
    if f < 2:
        for dr, dc in ((-1, 0), (0, -2), (0, 2), (-1, -3), (-1, 3)):
            c.put(IMPACT[0] + dr, IMPACT[1] + dc, "*" if (f + dr + dc) % 2 else "#", "f")
    for shape, at, launch, *_ in SPLATS:
        chunk(c, f, launch, (at[0] + len(shape) // 2, at[1] + len(shape[0]) // 2))
    for k, to in enumerate(DEBRIS):
        chunk(c, f, k % 3, (IMPACT[0] + to[0], IMPACT[1] + to[1]))
    shown = {row: (min(i for i, ch in enumerate(c.cells[row]) if ch != " "),
                   max(i for i, ch in enumerate(c.cells[row]) if ch != " "))
             for row in TEXT_ROWS if c.cells[row].count(" ") < BIG_WIDTH}
    for spec in SPLATS:
        for row, col, ch in splat(f, *spec):
            if row in shown and shown[row][0] <= col <= shown[row][1]:
                continue
            c.put(row, col, ch, "r")


def fallen(state: str, f: int) -> float:
    """How far the ground has come, 0 to 1: none before the whale exists, then most of the way
    and never all of it while compacting, all of it once compacted."""
    if state == "completed":
        return 1.0
    if state != "compacting" or f < MORPH:
        return 0.0
    return CLOSEST * (1 - 0.75 ** ((f - MORPH) / (2 * TICKS)))


def sunk(f: int) -> int:
    """Rows of ground still risen, f frames after the compaction is called off: it sinks back."""
    return max(0, 2 - f // TICKS)


def ground(c: Canvas, top: int, f: int, dust: bool) -> None:
    """Magrathea from row top down to the floor: a ridge of rock, scree under it, dust kicked up
    as it rushes in."""
    c.put(top, 0, RIDGE, "d")
    for row in range(top + 1, FLOOR + 1):
        c.put(row, 0, "░" * BIG_WIDTH, "s")
    if dust:
        for col in range(3, BIG_WIDTH, 7):
            if (col + f) % 3 == 0:
                c.put(top - 1, col, ".", "s")


def sky(c: Canvas, top: int, f: int) -> None:
    """The title in large friendly letters, and stars twinkling above the ground."""
    c.put(TITLE, 0, "D O N ' T   P A N I C".center(BIG_WIDTH), "h")
    for i, (row, col) in enumerate(STARS):
        if row < top and (f // 4 + i) % 5:
            c.put(row, col, "·" if i % 3 else "*", "s")


def whale(c: Canvas, f: int, falling: bool, row: int = WHALE_AT[0]) -> None:
    """The sperm whale, its top on row, its flukes flapping and its eye blinking, wind streaming
    past while it falls."""
    col = WHALE_AT[1]
    for r, text in enumerate(WHALE):
        c.put(row + r, col, text, "c")
    if f // 3 % 2:
        c.put(row + 1, col + 14, FLAP[0], "c")
        c.put(row + 2, col + 14, FLAP[1], "c")
    if f % (4 * TICKS) < 2:
        c.put(row + 1, col + 3, "-", "c")
    if falling:
        for r in range(row - 1, row + 4):
            for x in (col + 2, col + 9, col + 16):
                if (r + f + x) % 4 == 0 and c.cells[r][x - 1] == " ":
                    c.put(r, x - 1, "'", "s")


def petunias(c: Canvas, f: int, row: int = PETUNIAS_AT[0]) -> None:
    """The bowl of petunias, nodding."""
    col = PETUNIAS_AT[1]
    c.put(row, col, "*@*" if f // 4 % 2 else "@*@", "r")
    c.put(row + 1, col, PETUNIAS[1], "d")
    c.put(row + 2, col, PETUNIAS[2], "h")


def missiles(c: Canvas, f: int, rows: tuple[int, ...], cols: tuple[int, ...]) -> None:
    """The missiles, flames flickering."""
    for row, col in zip(rows, cols):
        c.put(row, col, MISSILE, "r")
        c.put(row, col, "~*" if f % 2 else "*~", "k")


def say(c: Canvas, mid: int, text: str, role: str = " ") -> None:
    """Something said over someone's head, kept inside the frame."""
    words = f'"{text}"'
    c.put(SAY, max(1, min(mid - len(words) // 2, BIG_WIDTH - 1 - len(words))), words, role)


def altitude(fall: float) -> str:
    return f"{round(HEIGHT * (1 - fall), -2):,.0f} ft"


def readout(c: Canvas, state: str, f: int) -> None:
    """The ship's readout: the missiles and the drive, the improbability, then the altitude."""
    if state == "preparing":
        c.put(READOUT, 2, "INCOMING: 2 MISSILES", "r" if f // 3 % 2 else "h")
        c.put(READOUT, 26, "IMPROBABILITY DRIVE", "h")
        c.put(READOUT, 46, "warming" + "." * (f // 2 % 4), "c")
    elif state == "compacting" and f < MORPH:
        power = 276709 * (f + 1) ** 2 // MORPH ** 2   # 2^276,709 on the last frame before the whale
        c.put(READOUT, 2, "IMPROBABILITY", "h")
        c.put(READOUT, 16, f"2^{power:,} to 1 against", "c")
    elif state == "completed" and f >= FALLING:
        c.put(READOUT, 4, "And the rest, after a sudden wet thud, was silence.", "c")
    elif state == "failed":
        c.put(READOUT, 2, "IMPROBABILITY DRIVE", "h")
        if f % 2 == 0:
            c.put(READOUT, 22, "** STALLED **", "r")
        c.put(READOUT, 37, f"ALTITUDE {altitude(0.0)}", "c")
    else:
        fall = fallen(state, f) if state == "compacting" else CLOSEST + (1 - CLOSEST) * f / FALLING \
            if state == "completed" else sunk(f) / RISE
        full = int(10 * fall)
        c.put(READOUT, 2, "ALTITUDE", "h")
        c.put(READOUT, 11, "[" + "█" * full + "·" * (10 - full) + "]", "c")
        c.put(READOUT, 24, altitude(fall), "c")
        if state == "cancelled" and not fall:
            c.put(READOUT, 36, "and holding", "c")


def scene(state: str, t: float, animate: bool = True) -> Frame:
    """One frame of the big art for a stage, t seconds into it (rows, roles): BIG_HEIGHT rows of
    BIG_WIDTH cells. animate False: the stage's still (STILL), as with the app's animation off."""
    t = t if animate else STILL.get(state, 0.0)
    f = int(t * TICKS)
    c = Canvas()
    beat = f // (2 * TICKS)
    if state == "preparing":
        sky(c, FLOOR, f)
        ground(c, FLOOR, f, dust=False)
        row, col = HOG_AT
        for r, text in enumerate(HOG):
            c.put(row + r, col, text, "f" if r < 2 else "s")
        if f % TICKS < 3:
            c.put(row + 1, col + 3, "o", "k")
        missiles(c, f, MISSILES, tuple(min(f, 22) + d for d in (2, 6)))
        say(c, HOG_AT[1] + 6, ("Missiles!", "What does this button do?", "Hi there! Two missiles!")[beat % 3])
    elif state == "compacting" and f < MORPH:
        sky(c, FLOOR, f)
        ground(c, FLOOR, f, dust=False)
        glyphs = "?%&#@$*!"
        for i, (row, col) in enumerate(((WHALE_AT[0] + 1, WHALE_AT[1] + 4), (PETUNIAS_AT[0] + 1, PETUNIAS_AT[1]))):
            c.put(row, col, "".join(glyphs[(f + i + k) % len(glyphs)] for k in range(5 if i == 0 else 3)), "f")
            for k, (dr, dc) in enumerate(((-1, -2), (1, 3), (0, 7), (-1, 5))):
                if (f + k + i) % 2:
                    c.put(row + dr, col + dc, "*", "k")
    elif state == "compacting":
        fall = fallen(state, f)
        top = FLOOR - int(RISE * fall)
        sky(c, top, f)
        ground(c, top, f, dust=True)
        whale(c, f, falling=True)
        petunias(c, f)
        say(c, WHALE_AT[1] + 10, RUSHING if fall > 0.6 else THOUGHTS[beat % len(THOUGHTS)])
    elif state == "completed" and f < FALLING:   # the last of the fall: the petunias have a thought
        top = FLOOR - RISE + 1   # a row of air under them: they touch the ground only as the whale bursts
        sky(c, top, f)
        ground(c, top, f, dust=True)
        whale(c, f, falling=True)
        petunias(c, f)
        c.put(THINK_AT[0], THINK_AT[1], f"( {THOUGHT} )", "s")
        c.put(THINK_AT[0], THINK_AT[1] + 2, THOUGHT, "f")
    elif state == "completed":
        top = FLOOR - RISE
        sky(c, top, f)
        ground(c, top, f, dust=False)
        c.put(top - 1, WHALE_AT[1], FLAT, "c")
        for k, col in enumerate((WHALE_AT[1] - 2, WHALE_AT[1] + 10, WHALE_AT[1] + len(FLAT) + 1)):
            if f - FALLING < 2 * TICKS and (f + k) % 2:
                c.put(top - 2, col, "*", "k")
        petunias(c, f, row=top - 3)
    elif state == "failed":
        sky(c, FLOOR, f)
        ground(c, FLOOR, f, dust=False)
        if f % 3 == 2:
            whale(c, f, falling=False)
            petunias(c, f)
        else:
            missiles(c, f, (WHALE_AT[0] + 1,), (WHALE_AT[1] + 4,))
            for row, col, ch in ((2, 15, "?"), (3, 18, "*"), (4, 11, "'"), (2, 43, "?"), (3, 41, "*"), (4, 45, "'")):
                c.put(row, col, ch, "k")
            c.put(3, 43, "?", "r")
        say(c, WHALE_AT[1] + 10, ("Er... am I a missile now?", "Hang on. Where's the ground gone?")[beat % 2], "r")
    else:
        top = FLOOR - sunk(f)
        sky(c, top, f)
        ground(c, top, f, dust=False)
        whale(c, f, falling=False)
        petunias(c, f)
        if t < 4:
            say(c, WHALE_AT[1] + 10, "Oh. Never mind.")
    readout(c, state, f)
    c.put(BIG_HEIGHT - 1, 0, CAPTIONS.get(state, "").center(BIG_WIDTH), " ")
    if state == "completed" and f >= FALLING:
        lens(c, f - FALLING)
    return c.done()


SCENE = Scene("hitchhikers", EXCERPTS, CAPTIONS, WALLS, scene)
