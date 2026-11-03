"""Wallace and Gromit, The Wrong Trousers, the train chase (#304, scene F, "Cracking compaction,
Gromit!"): compaction is Gromit on the front of the toy train at full tilt, laying track a piece
ahead of the wheels, the summary written just in time. Wallace rides the middle carriage,
cheerfully oblivious, and Feathers McGraw sits on the rear one with the diamond. The TRACK
readout counts the pieces left in Gromit's box: it falls, but never quite to nothing while it
compacts. Once compacted the train pulls up with a piece to spare and Feathers is in a milk
bottle. Gromit never speaks: his lines are actions and eyebrows.

Each excerpt is one line or a two-line exchange, drawn at random without repeats for the length
of one compaction cycle (compactor.schedule).
"""

from __future__ import annotations

from .base import BIG_HEIGHT, BIG_WIDTH, TICKS, WIDTH, Canvas, Frame, Scene

EXCERPTS: tuple[tuple[str, ...], ...] = (
    ("Wallace: Cracking compaction, Gromit!",),
    ("Gromit: (slaps down three pieces of track, one-pawed)",),
    ("Wallace: Everything's under control, lad.", "Gromit: (a long, flat look)"),
    ("Gromit: (lays track with one paw and writes the summary with the other)",),
    ("Wallace: Not much of a talker, are you, Gromit? Good. Saves tokens.",),
    ("Gromit: (raises one eyebrow. That's the whole summary.)",),
    ("Feathers McGraw: (quietly uncouples the oldest carriage)", "Wallace: Funny. I'd have sworn this train was longer."),
    ("Feathers McGraw: (pulls a rubber glove on his head. Summary: one chicken.)",),
    ("Feathers McGraw: (checks his plan. It's been summarised to \"a house\".)",),
    ("Feathers McGraw: (rewires the trousers. Summary: Wallace took the diamond.)",),
    ("Wallace: Look at the room the compaction's freed up! I'll put a sign up.", "Feathers McGraw: (already unpacking)"),
    ("Gromit: (moves out to the kennel. It's about the size of the summary.)",),
    ("Gromit: (walks out in the rain with his bag. Summary: \"popped out\".)",),
    ("Wallace: Gromit, you've laid track across my cheese!", "Gromit: (shrugs. The summary needed the room.)"),
    ("Wallace: Did the summary keep the Wensleydale? Then it's a good summary.",),
    ("Wallace: What did we do on Tuesday, lad?", "Gromit: (lays a piece of track over the gap where Tuesday was)"),
    ("Wallace: Our trip to the moon, Gromit? Summarised in one word: \"cheese\".",),
    ("Wallace: These trousers are getting tighter, Gromit. Is that the compaction?",),
    ("Techno-Trousers: (walk off with the old context, Wallace still in them)",),
    ("Newsreader: A diamond has been compacted from the City Museum.",),
    ("Wallace: It's the bills, Gromit. Every token costs. That's why we compact.",),
    ("Wallace: Happy birthday, lad! I got you a compaction. Ex-NASA, that.",),
    ("Wallace: Ooh, we're going that fast the scenery's being summarised!",),
    ("Wallace: Ooh, a tunnel! Is that where the old messages go?", "Gromit: (nods, very slowly)"),
    ("Gromit: (pulls up the oldest track behind the train and lays it in front)",),
    ("Gromit: (lays the line in a wide curve round last week's argument)",),
    ("Wallace: I've had an idea, Gromit. What if we laid the track first?", "Gromit: (lays another piece without looking up)"),
    ("Wallace: You've missed a bit, lad.", "Gromit: (points at the summary, where the bit is, only smaller)"),
    ("Wallace: I've built a machine that compacts the context and does the toast.", "Gromit: (holds up a slice. It's a summary of toast.)"),
    ("Wallace: The summary's spelt my name with one L, Gromit.", "Gromit: (adds the other L in crayon. It runs a line over.)"),
    ("Wallace: Will the summary mention my tank top, do you think?", "Gromit: (holds up the summary. It's mostly about the tank top.)"),
    ("Wallace: Funny, Gromit. I don't remember inviting a penguin.", "Gromit: (taps the WANTED poster, then the penguin, then the poster)"),
    ("Feathers McGraw: (reads the summary. He isn't in it. He nods.)",),
    ("Wallace: It's the wrong summary, Gromit! And it's gone wrong!",),
    ("Wallace: You've got the knack of this, lad. Who taught you?", "Gromit: (holds up \"Compaction for Dogs\", well thumbed)"),
    ("Wallace: Let's see this summary, then. \"Wallace: oblivious.\" Well, charming.",),
    ("Wallace: Funny how the house gets smaller every time we go round, lad.",),
    ("Wallace: Shall I read the summary out, lad? It's quicker than living it.",),
    ("Gromit: (sniffs the summary, keeps it, and buries the rest in the garden)",),
    ("Gromit: (fetches the old context back. Wallace throws it again.)",),
    ("Gromit: (stops to check the summary for typos, then lays three to catch up)",),
    ("Wallace: I'll just pop the kettle on while you compact, lad.", "Gromit: (lays track to the kettle as well)"),
    ("Wallace: Ooh, I say, Gromit. I've gone all concise.",),
    ("Wallace: Can the summary keep the bit where I was clever?", "Gromit: (searches. Keeps searching.)"),
    ("Wallace: Cheerio, old messages! Lovely knowing you!",),
    ("Feathers McGraw: (cases the context like the museum. The loot: the summary.)",),
    ("Techno-Trousers: (take two strides and reach the end of the conversation)",),
    ("Gromit: (holds up a sign, TRACK ENDS HERE, and moves it along a bit)",),
    ("Wallace: Next stop, After Compaction! All change!",),
    ("Wallace: Fetch the big box of track, lad.", "Gromit: (holds up the big box. It's quite small.)"),
    ("Wallace: We used to keep every message, lad. In a shoebox under the stairs.",),
    ("Gromit: (lays the track in a loop. The summary goes round twice.)",),
    ("Gromit: (glances at the camera, as if to say: you try summarising Wallace)",),
    ("Feathers McGraw: (counts the tokens as they go by. Pockets one.)",),
    ("Gromit: (lays a piece marked SUMMARY, then one marked REST OF SUMMARY)",),
    ("Gromit: (gives the summary a pat. Good summary.)",),
    ("Wallace: I've left us a note for after, lad: \"Buy cheese. Watch penguin.\"", "Gromit: (underlines \"Watch penguin\" three times)"),
    ("Wallace: Let's take the scenic route next time, Gromit. The long version.",),
    ("Wallace: Steady on, lad, you're summarising faster than I can talk!",),
    ("Wallace: The compaction's taken my train of thought, Gromit!", "Gromit: (points at the one he's laying track for)"),
    ("Gromit: (lays the main line straight; Wallace's anecdotes go in a siding)",),
    ("Wallace: Did you get all that, Gromit?", "Gromit: (got about a third of it, and the third that mattered)"),
    ("Gromit: (tucks the important bit behind his ear for after the compaction)",),
)


# The cycle's small art (#277): the big art's rows 2 to 8 cut down to WIDTH columns round the
# train (from CROP), the readout under them and the caption. Cut from the big still itself, so
# the two always agree, and every row of every stage WIDTH cells
def small(state: str, readout: str) -> str:
    """One stage's small art: the train cut from its big still, the readout, then the caption."""
    rows = scene(state, 0.0, animate=False)[0][2:TRACK + 1]
    return "\n".join([*(r[CROP:CROP + WIDTH] for r in rows), (" " + readout).ljust(WIDTH), CAPTIONS[state].center(WIDTH)])


# The docked art (#287): the toy train running right along the skirting, big and moving. Rows: a
# row for what's said, the wallpaper going by, Gromit's eyebrows, the three of them on the train,
# its trucks and wheels, the track (laid as far as Gromit has got), the carpet, the readout and
# the caption. Feathers is white, Gromit's fresh piece and the diamond amber
CAPTIONS = {"preparing": "the train set comes out", "compacting": "a piece of track ahead",
            "completed": "compacted, penguin bottled", "failed": "end of the line, lad",
            "cancelled": "Gromit pulls the brake"}
SAY, PAPER, TRACK, CARPET, READOUT = 0, 1, 8, 9, 10
REAR, MIDDLE, ENGINE = 17, 23, 30   # the rear truck's, the middle carriage's and the engine's first columns
CHIMNEY, GROMIT = 34, 36   # the chimney's column; Gromit's first, on the boiler in front of it
NOSE = 41   # the engine's last column; the track has to reach past it
CROP = 17   # the small art's first column
PIECE = 3   # columns in a piece of track
BOX = 24   # pieces of track in a full box
STILL = {"preparing": 4.0, "compacting": 24.0, "completed": 3.0, "failed": 0.0, "cancelled": 1.0}
BROWS = {"up": "^ ^", "flat": "- -", "worried": "/ \\", "cross": "\\ /", "one": "^ -"}


def pieces(state: str, f: int) -> int:
    """Pieces left in Gromit's box: a full box until it moves, then falling and never to none
    while compacting, the one to spare once compacted, none if it failed."""
    if state == "preparing":
        return BOX
    if state == "compacting":
        return 1 + round((BOX - 1) * 0.5 ** (f / (60 * TICKS)))
    if state == "completed":
        return 1
    if state == "failed":
        return 0
    return min(BOX, 9 + f // TICKS)   # cancelled: the unlaid pieces go back in the box


def track(c: Canvas, scroll: int, end: int, fresh: bool) -> None:
    """The track from the left edge to column end (exclusive), its joints moving with scroll;
    fresh: the last piece just slapped down, in amber."""
    for x in range(0, min(end, BIG_WIDTH)):
        w = x + scroll
        c.put(TRACK, x, "╪" if w % PIECE == 0 else "═", "k" if fresh and x >= end - PIECE else "s")


def paper(c: Canvas, scroll: int) -> None:
    """The wallpaper going by at half the train's speed, a picture and the lodger's sign on it."""
    off = scroll // 2
    for x in range(BIG_WIDTH):
        w = x + off
        c.put(PAPER, x, "·" if w % 4 == 0 else " ", "w")
    for at, text in ((7, "[#]"), (33, "[ROOM TO LET]")):
        x = (at - off) % 72 - 6
        c.put(PAPER, x, text, "w")
    c.put(CARPET, 0, "▀" * BIG_WIDTH, "w")


def feathers(c: Canvas, row: int, col: int, diamond: bool = True) -> None:
    """Feathers McGraw, his head on row, and the diamond under his flipper."""
    c.put(row, col, "(o>", "f")
    c.put(row + 1, col, "/█\\", "f")
    if diamond:
        c.put(row + 1, col + 3, "*", "k")


def bottle(c: Canvas, col: int) -> None:
    """Feathers in a milk bottle on the rear truck: its neck, then him filling it."""
    c.put(2, col, " _ ", "f")
    c.put(3, col, "| |", "f")
    c.put(4, col - 1, "/(o>\\", "f")
    c.put(5, col - 1, "|/█\\|", "f")


def wallace(c: Canvas, wave: bool) -> None:
    """Wallace in the middle carriage: the grin, the tank top, and a hand up if he's waving."""
    c.put(3, MIDDLE, "(o o)", "c")
    c.put(4, MIDDLE, "[===]", "c")
    c.put(5, MIDDLE + 1, "/#\\", "c")
    if wave:
        c.put(3, MIDDLE + 6, "o", "c")
        c.put(4, MIDDLE + 5, "/", "c")


def gromit(c: Canvas, brows: str, arm: str, f: int = 0) -> None:
    """Gromit on the engine's boiler, his brows over him; arm: "down" (slapping a piece on the
    track ahead), "up" (the next one ready), "box" (shaking the empty box over his head) or "in"."""
    col = GROMIT
    c.put(2, col + 1, BROWS[brows], "c")
    c.put(3, col, "(o o)", "c")
    c.put(4, col, "U(▄)U", "c")
    c.put(5, col + 1, "/██", "c")
    if arm == "down":
        for i in range(3):
            c.put(5 + i, col + 4 + i, "\\", "c")
    elif arm == "up":
        c.put(4, col + 5, "═╪═", "k")
        c.put(5, col + 4, "/", "c")
    elif arm == "box":
        c.put(3, col + 5, "/", "c")
        c.put(2, col + 6 + f % 2, "[_]", "h")


def train(c: Canvas, f: int, moving: bool, off: bool = False) -> None:
    """The rear truck, the middle carriage and the engine, coupled, its wheels turning if moving;
    off: its front run off the end of the track, nose down on the carpet."""
    c.put(6, REAR, "[___]=[____]=", "w")
    c.put(7, REAR, " o o   o  o ", "s")
    c.put(4, ENGINE, "._.", "w")
    c.put(5, ENGINE, "| |", "w")
    c.put(5, CHIMNEY, "n", "w")
    spoke = "x" if moving and f % 2 else "+"
    if off:
        c.put(6, ENGINE, "|_|______", "w")
        c.put(7, ENGINE, f" ({spoke})  ({spoke})\\", "s")
        c.put(TRACK, NOSE - 1, "\\_>", "w")
    else:
        c.put(6, ENGINE, "|_|________>", "w")
        c.put(7, ENGINE, f" ({spoke})  ({spoke})({spoke})", "s")


def steam(c: Canvas, f: int) -> None:
    """Puffs off the chimney, rising and drifting back as the train goes."""
    for i in range(2):
        k = (f + 3 * i) % 6
        c.put(4 - k // 2, CHIMNEY - (k + 1) // 2, "O" if k < 3 else "o", "s")


def say(c: Canvas, text: str, mid: int, role: str = " ") -> None:
    """Something said (or, for Gromit, done) over someone's head, kept inside the frame."""
    words = text if text.startswith("(") else f'"{text}"'
    c.put(SAY, max(0, min(mid - len(words) // 2, BIG_WIDTH - len(words))), words, role)


def readout(c: Canvas, left: int, status: str, role: str = "c") -> None:
    """TRACK: the pieces left in Gromit's box, as a bar and a count, then how it's going."""
    full = round(10 * left / BOX)
    full = max(full, 1) if left else 0
    c.put(READOUT, 1, "TRACK", "h")
    c.put(READOUT, 7, "[" + "█" * full + "·" * (10 - full) + "]", "c")
    c.put(READOUT, 20, f"{left:2d} piece{'' if left == 1 else 's'} in the box", "c")
    c.put(READOUT, 43, status, role)


PREPARING = (("Right, Gromit. A quick compaction, then cheese.", MIDDLE + 2),
             ("(counts the track. Counts it again.)", GROMIT + 2),
             ("All aboard, lad!", MIDDLE + 2))
GOING = (("Ooh, I do love a day out on the train, lad!", MIDDLE + 2), ("(slap. slap. slap.)", GROMIT + 2),
         ("Ooh, we're fair flying now, lad!", MIDDLE + 2), ("(another piece. And another.)", GROMIT + 2))
LOW = (("Is there a buffet car, Gromit?", MIDDLE + 2), ("(rummages in the box)", GROMIT + 2))
LOW_AT = 3   # pieces left at which Gromit starts to worry
STATUS = {"preparing": "all aboard", "compacting": "and laying", "completed": "to spare",
          "failed": "OFF THE RAILS", "cancelled": "back in the box"}


def braked(f: int) -> int:
    """How far a called-off train has rolled, f ticks in: slowing from a column a tick to a
    stop over two seconds."""
    stop = 2 * TICKS
    return sum(stop - i for i in range(min(f, stop))) // stop


def scene(state: str, t: float, animate: bool = True) -> Frame:
    """One frame of the big art for a stage, t seconds into it (rows, roles): BIG_HEIGHT rows of
    BIG_WIDTH cells. animate False: the stage's still (STILL), as with the app's animation off."""
    t = t if animate else STILL.get(state, 0.0)
    f = int(t * TICKS)
    c = Canvas()
    beat = f // (2 * TICKS)
    left = pieces(state, f)
    role = "c"
    if state == "preparing":
        paper(c, 0)
        track(c, 0, NOSE + 1 + PIECE, fresh=False)
        train(c, f, moving=False)
        if f >= 2 * TICKS:   # Feathers waddles along the carpet and hops on the rear truck
            feathers(c, 4, REAR + 1)
        else:
            feathers(c, 6, 1 + f * REAR // (2 * TICKS))
        wallace(c, wave=beat % 2 == 1)
        gromit(c, "one" if beat % 3 == 1 else "up", "up")
        say(c, *PREPARING[beat if beat < 3 else 1 + beat % 2])
    elif state == "compacting":
        paper(c, f)
        end = PIECE * ((f + NOSE + 1) // PIECE + 1) - f   # past the nose: one to three columns of track
        slap = (f + NOSE + 1) % PIECE == 0
        track(c, f, end, fresh=slap)
        steam(c, f)
        train(c, f, moving=True)
        feathers(c, 4, REAR + 1)
        wallace(c, wave=f % 4 < 2)
        text, mid = (LOW if left <= LOW_AT else GOING)[beat % len(LOW if left <= LOW_AT else GOING)]
        gromit(c, "worried" if left <= LOW_AT else ("flat" if mid == MIDDLE + 2 else "cross"), "down" if slap else "up")
        say(c, text, mid)
        role = "k" if slap else "c"
    elif state == "completed":
        paper(c, 0)
        track(c, 0, NOSE + 1 + PIECE, fresh=False)
        train(c, f, moving=False)
        bottle(c, REAR + 1)
        wallace(c, wave=f % 4 < 2)
        gromit(c, "up" if beat % 2 == 0 else "one", "up")
        say(c, *((("Feathers McGraw, compacted to fit a pint!", MIDDLE + 2),
                  ("(holds up the piece to spare)", GROMIT + 2))[beat % 2]))
        role = "h"
    elif state == "failed":
        paper(c, 0)
        track(c, 0, NOSE - 2, fresh=False)
        train(c, f, moving=True, off=True)
        if f % 2 == 0:   # sparks where the nose ploughs the carpet, in the still as well
            c.put(TRACK - 1, NOSE + 2, "*", "k")
            c.put(CARPET, NOSE, "*", "k")
        if f >= TICKS:   # Feathers hops off the back and waddles away with the diamond
            feathers(c, 6, max(0, REAR - 2 - (f - TICKS) // 2))
        else:
            feathers(c, 4, REAR + 1)
        wallace(c, wave=True)
        gromit(c, "flat", "box", f)
        say(c, *((("Ooh! End of the line, Gromit!", MIDDLE + 2), ("(shakes the empty box)", GROMIT + 2))[beat % 2]))
        role = "r"
    else:
        rolled = braked(f)
        paper(c, rolled)
        track(c, rolled, NOSE + 1 + PIECE, fresh=False)
        train(c, f, moving=f < 2 * TICKS)
        feathers(c, 4, REAR + 1)
        wallace(c, wave=False)
        gromit(c, "one", "up" if f % TICKS < TICKS // 2 else "in")
        if t < 4:
            say(c, "Eh? Are we stopping, lad?", MIDDLE + 2)
    readout(c, left, STATUS[state], role if state != "failed" or f % 2 == 0 else "c")
    c.put(BIG_HEIGHT - 1, 0, CAPTIONS.get(state, "").center(BIG_WIDTH), " ")
    return c.done()


def small_readout(state: str) -> str:
    """The small art's readout: the pieces left at the stage's still, and how it's going."""
    left = pieces(state, int(STILL[state] * TICKS))
    return f"TRACK {left} {STATUS[state]}"


WALLS = {state: small(state, small_readout(state)) for state in CAPTIONS}

SCENE = Scene("wrong_trousers", EXCERPTS, CAPTIONS, WALLS, scene)
