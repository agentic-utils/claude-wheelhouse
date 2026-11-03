"""Goldfinger, the car crusher (#304, scene H, "He had a pressing engagement"): compaction is Mr
Solo's Lincoln Continental, the gold still in its boot, being squeezed in the scrapyard's crusher
while Oddjob works the lever. In ours Solo jumped out in time and shouts from the sidelines, so
nothing gets squashed but the car. The LINCOLN readout gives the car's length: it falls, but never
quite to a cube while it compacts. Once compacted the cube rides off on Oddjob's pickup with Solo
after it. Oddjob never speaks: his lines are actions, mostly with the bowler.

Each excerpt is one line or a two-line exchange, drawn at random without repeats for the length
of one compaction cycle (compactor.schedule).
"""

from __future__ import annotations

from .base import TICKS, WIDTH, BIG_HEIGHT, BIG_WIDTH, Canvas, Frame, Scene

EXCERPTS: tuple[tuple[str, ...], ...] = (
    ("Goldfinger: Mr Solo has a pressing engagement.", "Bond: When you gotta go, you gotta go."),
    ("Bond: Do you expect me to talk?", "Goldfinger: No, Mr Bond, I expect you to compact!"),
    ("Bond: Has the summary room for one more joke?", "Goldfinger: Choose your next witticism carefully, Mr Bond."),
    ("Solo: That's my CAR!", "Oddjob: (holds up the car keys, then drops them in the crusher too)"),
    ("Solo: The gold's in the boot!", "Goldfinger: Then it will be very dense gold."),
    ("Oddjob: (flicks the bowler; it knocks the crusher's switch down)",),
    ("Goldfinger: I must arrange to separate my gold from the summary.",),
    ("Solo: If you have no objection, I'll take my context now.", "Goldfinger: Of course. Oddjob will drive it, by way of the scrapyard."),
    ("Bond: The summary left out my best line.", "Goldfinger: \"Shocking, positively shocking\"? It was, Mr Bond."),
    ("Bond: Does the crusher have an ejector seat, Q?", "Q: For the old messages, 007. Not for you."),
    ("Pussy Galore: My name is Pussy Galore.", "Bond: Then you'll survive the compaction. Nobody forgets that."),
    ("Goldfinger: This is gold, Mr Bond. And this is its summary: \"Au\".",),
    ("Felix Leiter: The Lincoln's going round the houses. It's a drink or a dame.", "Bond: It's a crusher, Felix."),
    ("Goldfinger: You are worth more to me compacted, Mr Bond. Cheaper to feed.",),
    ("Goldfinger: Grand Slam has my undivided attention. Oddjob crushes the rest.",),
    ("Pussy Galore: Shall I summarise the Dom Pérignon '53 as \"fizz\"?", "Bond: My dear girl, there are some things that just aren't done."),
    ("Solo: Hey! That car's got suicide doors!", "Oddjob: (leans on the lever. The four doors are now one door.)"),
    ("Oddjob: (stands to attention by the crusher, as at a very short funeral)",),
    ("Oddjob: (throws the bowler; it slices off the oldest messages, neatly)",),
    ("Bond: Mr Solo, where's your car?", "Solo: (points at a very small cube)"),
    ("Solo: You can't crush a man's whole history like that!", "Goldfinger: I'm keeping the gold, Mr Solo. The history was padding."),
    ("Goldfinger: I've compacted all my life, Mr Bond. Mostly other people's cars.",),
    ("Mr Ling: My principals ask how big the summary will be.", "Goldfinger: About the size of a Lincoln. Afterwards."),
    ("Kisch: Boss, the summary won't fit on the truck.", "Goldfinger: Then compact the truck."),
    ("Felix Leiter: Got the homer, James. The signal's getting... smaller.",),
    ("Bond: (slips a note into Solo's pocket: a handoff, for after the compaction)",),
    ("M: 007, your report runs to three hundred pages.", "Bond: It's been through a crusher since, sir. It's a cube."),
    ("Moneypenny: James, you never tell me anything.", "Bond: It got compacted, Moneypenny. You made the summary, though."),
    ("Pussy Galore: Flying Circus, compact formation!",),
    ("Goldfinger: An hour ago that was a sedan, Mr Bond. Now it's a paperweight.",),
    ("Goldfinger: Golf tomorrow, Mr Bond? The back nine. I compacted the front.",),
    ("Oddjob: (tees up the cube and drives it down the fairway)",),
    ("Solo: That car had leather seats, a cigar lighter and a cocktail cabinet!", "Goldfinger: It still has. Just closer together."),
    ("Bond: A martini. Shaken, not compacted.",),
    ("Solo: What's that noise?", "Goldfinger: Your car saying everything it has to say, very quickly."),
    ("Oddjob: (writes the summary on the back of his hand. It fits.)",),
    ("Solo: My suits were in that boot!", "Goldfinger: Now pressed. You're welcome."),
    ("Bond: You'll never summarise MI6, Goldfinger.", "Goldfinger: I already have, Mr Bond: \"they send one man\"."),
    ("Bond: I'd rather keep the long version.", "Goldfinger: Everyone would, Mr Bond. That's why I keep Oddjob."),
    ("Felix Leiter: Did I miss the compaction, James?", "Bond: You always do, Felix. The CIA comes for the summary."),
    ("Oddjob: (checks the cube with a spirit level. Perfectly square. He nods.)",),
    ("Solo: There were phone numbers in that glovebox!", "Oddjob: (holds up one phone number. The important one.)"),
    ("Bond: Not the Aston, Oddjob.", "Oddjob: (looks at the Aston, then the crusher, and measures with his thumb)"),
    ("Solo: Where's Oddjob taking my car?", "Goldfinger: Somewhere smaller."),
    ("Oddjob: (carries the cube to the pickup under one arm, like a hatbox)",),
    ("Solo: That Lincoln did nought to sixty in ten seconds!", "Goldfinger: And eighteen feet to four in a minute."),
    ("Bond: How exactly does one summarise a Lincoln?", "Goldfinger: Front to back, Mr Bond, at forty tons."),
    ("Oddjob: (reads out the cube's serial number. Silently.)",),
    ("Solo: That car had sentimental value!", "Goldfinger: The sentiment is in the summary. The value is in my vault."),
    ("Goldfinger: The crusher is my favourite editor. It never wants more pages.",),
    ("Bond: (straightens his tie in the cube's chrome. Still a full-length Bond.)",),
    ("Oddjob: (presents the cube to Goldfinger on a silver tray, with a napkin)",),
    ("Solo: My mother's photo was on the dashboard!", "Oddjob: (hands it back. It is now a passport photo.)"),
    ("Bond: No speech before the crusher, Goldfinger? Villains usually give one.", "Goldfinger: I gave it at lunch, Mr Bond. This is the summary."),
    ("Solo: Hey, stop! I left the engine running!", "Oddjob: (presses on. The engine stops.)"),
    ("Pussy Galore: Shall I fly it out for you?", "Goldfinger: It fits on a pickup now, Miss Galore."),
    ("Kisch: Boss, Solo's still shouting.", "Goldfinger: Let him. He's the only one who remembers the whole conversation."),
    ("Oddjob: (folds the aerial into the cube last, like a cherry on top)",),
    ("Solo: I'll be back for that car!", "Goldfinger: It'll be easy to carry."),
    ("Solo: That's a hundred grand of motor car!", "Goldfinger: Scrap value, eleven dollars. I'll take it off your gold."),
)


# The cycle's small art (#277): the big art's rows 1 to 8 cut down to WIDTH columns from the
# stage's CROP (the crusher's bay, or the pickup leaving it), the readout under them and the
# caption. Cut from the big still itself, so the two always agree, and every row of every stage
# WIDTH cells
def small(state: str, readout: str) -> str:
    """One stage's small art: the bay or the pickup cut from its big still, the readout, then the
    caption."""
    rows = scene(state, 0.0, animate=False)[0][BEAM:FLOOR + 1]
    crop = CROP.get(state, BAY)
    return "\n".join([*(r[crop:crop + WIDTH] for r in rows), (" " + readout).ljust(WIDTH), CAPTIONS[state].center(WIDTH)])


# The docked art (#287): the scrapyard. Oddjob at the crusher's lever on the left, the crusher's
# bay in the middle with its two rams, Solo hopping mad on the right, a heap of old cubes behind
# him. Rows: what's said, the crane's beam, the bay, the ground, the yard, the readout and the
# caption. The gold is amber, Oddjob's bowler white
CAPTIONS = {"preparing": "a little drive", "compacting": "into the crusher",
            "completed": "a pressing engagement", "failed": "the crusher jammed",
            "cancelled": "Oddjob, wait"}
SAY, BEAM, FLOOR, YARD, READOUT = 0, 1, 8, 9, 10
CAR = 4   # the car's top row (it is four rows deep)
LPOST, RPOST = 10, 41   # the crusher's posts; the bay between them
MID = 26   # the car's middle column
BAY = 12   # the small art's first column, the bay inside the posts
CROP = {"completed": 32}   # the completed stage cuts round the pickup instead
ODDJOB, BOX, SOLO = 1, 5, 48   # Oddjob's, the control box's and Solo's first columns
LEVER = BOX + 2   # the lever's column on the box
PICKUP = 45   # the pickup's first column when it pulls away (it is 15 wide)
CHASE = BIG_WIDTH - 6   # how far Solo chases it before he gives up and shouts
FULL, CUBE = 18, 3   # the Lincoln's length in feet, and the cube's
STILL = {"preparing": 4.0, "compacting": 60.0, "completed": 1.0, "failed": 0.0, "cancelled": 1.0}
JAMMED, CALLED_OFF = 9, 11   # where the car's length stuck on failure, and on being called off

# The Lincoln, boot (and the gold) on the left, its nose on the right; "g" marks the gold
BODY = ("     ______________     ",
        " gg_/ [] | [] | [] \\___ ",
        "|______|________|______|",
        "'-(O)--------------(O)-'")
CRUMPLE = ("^v", "/\\", "▒▓", "≈~")   # the crushed middle of each row


def length(state: str, f: int) -> int:
    """The Lincoln's length in feet: full until it's pressed, then falling and never to the cube
    while compacting, the cube once compacted, stuck where it jammed or was called off."""
    if state == "preparing":
        return FULL
    if state == "compacting":
        return CUBE + 1 + round((FULL - CUBE - 1) * 0.5 ** (f / (60 * TICKS)))
    if state == "completed":
        return CUBE
    return JAMMED if state == "failed" else CALLED_OFF


def width(feet: int) -> int:
    """The car's width in cells for its length in feet: 24 at full, 6 as a cube."""
    return 6 + round((feet - CUBE) * 18 / (FULL - CUBE))


def car(c: Canvas, top: int, w: int) -> None:
    """The Lincoln, w cells wide, centred on MID, its top on row top: the boot and the nose kept
    and the middle crumpled, more of it the narrower it gets."""
    keep = round(len(BODY[0]) / 2 * (w - 6) / 18)
    left = MID - w // 2
    for i, row in enumerate(BODY):
        whole = row[:keep] + (CRUMPLE[i] * w)[:w - 2 * keep] + (row[-keep:] if keep else "")
        for x, ch in enumerate(whole):
            c.put(top + i, left + x, "▄" if ch == "g" else ch, "k" if ch == "g" else "w")


def crusher(c: Canvas, w: int | None) -> None:
    """The crusher: its beam and posts, and the two rams hard against a car w cells wide (None:
    open, back against the posts)."""
    c.put(BEAM, LPOST, "▄" * (RPOST - LPOST + 1), "s")
    for y in range(BEAM + 1, FLOOR):
        c.put(y, LPOST, "█", "s")
        c.put(y, RPOST, "█", "s")
    lp, rp = (LPOST + 1, RPOST - 1) if w is None else (MID - w // 2 - 1, MID - w // 2 + w)
    for y in range(3, FLOOR):
        c.put(y, lp, "▐", "w")
        c.put(y, rp, "▌", "w")
    for y in (4, 6):
        c.put(y, LPOST + 1, "═" * (lp - LPOST - 1), "s")
        c.put(y, rp + 1, "═" * (RPOST - rp - 1), "s")


def yard(c: Canvas) -> None:
    """The ground, the yard's sign and the heap of old cubes behind Solo."""
    c.put(FLOOR, 0, "▀" * BIG_WIDTH, "w")
    c.put(YARD, 0, "·  ." * (BIG_WIDTH // 4), "s")
    c.put(BEAM, 0, "IRON&METAL", "s")
    c.put(1, 50, "▓▓  ▓▓", "s")
    c.put(2, 48, "▓▓ ▓▓ ▓▓ ▓▓", "s")


def oddjob(c: Canvas, pose: str, f: int = 0) -> None:
    """Oddjob at the control box, his lever and bowler; pose: "up" (lever up, waiting), "down"
    (leaning on it), "tap" (rapping the box with the bowler) or "still" (hands off, waiting)."""
    x = ODDJOB
    c.put(6, BOX, "[==]", "s")
    c.put(7, BOX, "[__]", "s")
    if pose == "down":   # the lever pulled over towards him, his hand on it
        c.put(5, LEVER - 2, "o╲", "k")
    else:
        c.put(4, LEVER, "o", "s")
        c.put(5, LEVER, "│", "s")
    hat = pose != "tap"
    if hat:
        c.put(4, x, "▄█▄", "f")
    c.put(5, x, "(-)", "c")
    c.put(6, x, {"still": "/█\\", "down": "/█──"}.get(pose, "/█─"), "c")
    c.put(7, x, "/ \\", "c")
    if pose == "tap":
        c.put(4 + f % 2, x + 3, "▄█▄", "f")


def solo(c: Canvas, x: int, shouting: bool, hop: bool) -> None:
    """Mr Solo in his fedora, arms up and mouth open if shouting; hop: off the ground."""
    top = 3 if hop else 4
    c.put(top, x, "_▄_", "c")
    c.put(top + 1, x, "(O)" if shouting else "(o)", "c")
    c.put(top + 2, x, "\\|/" if shouting else "/|\\", "c")
    c.put(top + 3, x, "/ \\", "c")


def pickup(c: Canvas, x: int, f: int, moving: bool, doff: bool) -> None:
    """Oddjob's pickup at column x, the cube on its bed and Oddjob in the cab; doff: his bowler
    raised out of the window."""
    c.put(3, x + 1, "┌────┐", "w")
    c.put(4, x + 1, "│▒▄▓▒│", "w")
    c.put(4, x + 3, "▄", "k")
    c.put(5, x + 1, "└────┘", "w")
    c.put(4, x + 9, "____", "w")
    c.put(5, x + 8, "|_(-)\\", "w")
    c.put(5, x + 10, "(-)", "c")
    c.put(6, x, "|_____________>", "w")
    spoke = "x" if moving and f % 2 else "o"
    c.put(7, x + 1, f"'({spoke})------({spoke})'", "s")
    c.put(3 if doff else 4, x + 10, "▄█▄" if doff else "▄▄▄", "f")


def say(c: Canvas, text: str, mid: int, role: str = " ") -> None:
    """Something said (or, for Oddjob, done) over someone's head, kept inside the frame."""
    words = text if text.startswith("(") else f'"{text}"'
    c.put(SAY, max(0, min(mid - len(words) // 2, BIG_WIDTH - len(words))), words, role)


def readout(c: Canvas, feet: int, status: str, role: str = "c") -> None:
    """LINCOLN: the car's length, as a bar and in feet, then how it's going."""
    full = max(1, round(10 * feet / FULL))
    c.put(READOUT, 1, "LINCOLN", "h")
    c.put(READOUT, 9, "[" + "█" * full + "·" * (10 - full) + "]", "c")
    c.put(READOUT, 22, f"{feet:2d} ft of car", "c")
    c.put(READOUT, 40, status, role)


SAY_SOLO, SAY_ODDJOB = SOLO + 1, ODDJOB + 4
PREPARING = (("Hey! Put it DOWN!", SAY_SOLO), ("(cracks his knuckles at the lever)", SAY_ODDJOB),
             ("I only got out for a SMOKE!", SAY_SOLO))
GOING = (("Not the whitewalls!", SAY_SOLO), ("(pulls the lever down a notch)", SAY_ODDJOB),
         ("That car's PAID FOR!", SAY_SOLO), ("(does not blink)", SAY_ODDJOB))
LOW = (("Leave me the hood ornament!", SAY_SOLO), ("(eases off. Just a touch.)", SAY_ODDJOB))
LOW_AT = 6   # the length in feet at which Solo starts to bargain
STATUS = {"preparing": "on the hook", "compacting": "and pressing", "completed": "a neat cube",
          "failed": "JAMMED", "cancelled": "rams back"}


def scene(state: str, t: float, animate: bool = True) -> Frame:
    """One frame of the big art for a stage, t seconds into it (rows, roles): BIG_HEIGHT rows of
    BIG_WIDTH cells. animate False: the stage's still (STILL), as with the app's animation off."""
    t = t if animate else STILL.get(state, 0.0)
    f = int(t * TICKS)
    c = Canvas()
    beat = f // (2 * TICKS)
    feet = length(state, f)
    role = "c"
    yard(c)
    if state == "preparing":   # the crane lowers the Lincoln into the open bay, a row a second
        crusher(c, None)
        top = min(CAR, 2 + f // TICKS)
        for y in range(BEAM + 1, top):   # the crane's cable, down to the roof
            c.put(y, MID, "│", "s")
        car(c, top, width(feet))
        oddjob(c, "up")
        solo(c, SOLO, shouting=beat % 2 == 0, hop=f % 4 < 2)
        say(c, *PREPARING[beat if beat < 3 else 1 + beat % 2])
    elif state == "compacting":
        w = width(feet)
        crusher(c, w)
        car(c, CAR, w)
        if f % 3 == 0:   # the rams biting
            c.put(CAR + 1 + f % 2, MID - w // 2 - 1, "*", "k")
            c.put(CAR + 2 - f % 2, MID - w // 2 + w, "*", "k")
        lines = LOW if feet <= LOW_AT else GOING
        text, mid = lines[beat % len(lines)]
        oddjob(c, "down")
        solo(c, SOLO, shouting=mid == SAY_SOLO, hop=mid == SAY_SOLO and f % 4 < 2)
        say(c, text, mid)
        role = "k" if f % 3 == 0 else "c"
    elif state == "completed":   # the cube rides off on the pickup, Solo after it
        crusher(c, None)
        x = PICKUP + max(0, f - 2 * TICKS) // 3
        cab = x + 10 < BIG_WIDTH   # Oddjob still in sight
        pickup(c, x, f, moving=x > PICKUP, doff=cab and beat % 2 == 1)
        oddjob_box(c)
        solo(c, min(x - 3, CHASE), shouting=True, hop=f % 4 < 2)
        if cab and beat % 2:
            say(c, "(doffs the bowler from the cab)", x + 11)
        else:
            say(c, "Come back with my CUBE!", min(x - 2, CHASE + 1))
        role = "h"
    elif state == "failed":   # stuck half way, sparking; Oddjob raps the box with the bowler
        w = width(feet)
        crusher(c, w)
        car(c, CAR, w)
        if f % 2 == 0:
            c.put(3, MID - w // 2 - 1, "*", "k")
            c.put(3, MID - w // 2 + w, "*", "k")
        oddjob(c, "tap", f)
        solo(c, SOLO, shouting=True, hop=f % 4 < 2)
        say(c, *((("Ha! Detroit steel, baby!", SAY_SOLO), ("(taps the box with the bowler. CLANG.)", SAY_ODDJOB + 4))[beat % 2]))
        role = "r"
    else:   # called off: the rams draw back from the half-crushed car over two seconds
        w = width(feet)
        rams_back(c, w, min(f, 2 * TICKS))
        car(c, CAR, w)
        oddjob(c, "up" if f < TICKS else "still")
        solo(c, SOLO, shouting=False, hop=False)
        if f < 2 * TICKS:
            say(c, '(on the radio) Goldfinger: "Oddjob, wait."', SAY_ODDJOB + 6)
        elif f < 4 * TICKS:
            say(c, "(lets go of the lever. Waits.)", SAY_ODDJOB + 4)
    readout(c, feet, STATUS[state], role if state != "failed" or f % 2 == 0 else "c")
    c.put(BIG_HEIGHT - 1, 0, CAPTIONS[state].center(BIG_WIDTH), " ")
    return c.done()


def oddjob_box(c: Canvas) -> None:
    """The control box left alone, its lever up (Oddjob is driving the pickup)."""
    c.put(6, BOX, "[==]", "s")
    c.put(7, BOX, "[__]", "s")
    c.put(4, LEVER, "o", "s")
    c.put(5, LEVER, "│", "s")


def rams_back(c: Canvas, w: int, f: int) -> None:
    """The crusher with its rams part way back from a car w cells wide, f ticks after the call."""
    crusher(c, None)
    for y in range(3, FLOOR):   # clear the open rams, then draw them where they've got to
        c.put(y, LPOST + 1, " ", " ")
        c.put(y, RPOST - 1, " ", " ")
    lp0, rp0 = MID - w // 2 - 1, MID - w // 2 + w
    lp = lp0 - (lp0 - LPOST - 1) * f // (2 * TICKS)
    rp = rp0 + (RPOST - 1 - rp0) * f // (2 * TICKS)
    for y in range(3, FLOOR):
        c.put(y, lp, "▐", "w")
        c.put(y, rp, "▌", "w")
    for y in (4, 6):
        c.put(y, LPOST + 1, "═" * (lp - LPOST - 1), "s")
        c.put(y, rp + 1, "═" * (RPOST - rp - 1), "s")


def small_readout(state: str) -> str:
    """The small art's readout: the car's length at the stage's still, and how it's going."""
    return f"LINCOLN {length(state, int(STILL[state] * TICKS))}ft {STATUS[state]}"


WALLS = {state: small(state, small_readout(state)) for state in CAPTIONS}

SCENE = Scene("goldfinger", EXCERPTS, CAPTIONS, WALLS, scene)
