"""Indiana Jones and the Temple of Doom, the spiked room (#304, scene E, "No time for love, Dr.
Jones!"): compaction is a ceiling of spikes grinding down. Short Round steps where he shouldn't,
the door slams, and the ceiling comes down on him and Indy, the HEADROOM readout falling, while
Willie, out in the tunnel, screams at the bugs and feels in two square holes for the lever. Indy
ducks; the spikes never quite reach his hat while it compacts. Once compacted the room is solid
stone and everyone is out in the tunnel, hat and all.

Each excerpt is one line or a two-line exchange, drawn at random without repeats for the length
of one compaction cycle (compactor.schedule).
"""

from __future__ import annotations

from .base import BIG_HEIGHT, BIG_WIDTH, TICKS, WIDTH, Canvas, Frame, Scene

EXCERPTS: tuple[tuple[str, ...], ...] = (
    ("Willie: Indy! Is that the ceiling?", "Indy: I see it, I see it!"),
    ("Willie: Which lever?", "Indy: The one in the summary! Hurry!"),
    ("Willie: Get them off me! Get the old messages off me!",),
    ("Short Round: Who step on compact stone? Not me, Dr. Jones. Maybe you.",),
    ("Indy: It's a long story, Willie. Hurry, or you only get the summary!",),
    ("Willie: There are two dead people down here!", "Indy: There are gonna be two summaries in here! Hurry!"),
    ("Willie: I sang \"Anything Goes\" in Shanghai. I didn't mean the context.",),
    ("Willie: This dress was from Paris. Now it's two bullet points.",),
    ("Willie: Chilled monkey brains, eyeball soup, now compaction. Some dinner.",),
    ("Short Round: Okey-Dokey Dr. Jones! Hold on to your context!",),
    ("Short Round: You call him Dr. Jones, doll! Even in the summary!",),
    ("Short Round: You cheat, Dr. Jones! You compact four messages into one!",),
    ("Willie: You'll get compacted chasing after your damn fortune and glory!",),
    ("Mola Ram: Soon, Kali Ma will compact the world.",),
    ("Mola Ram: You will, Dr. Jones. You will become a true summary.",),
    ("Mola Ram: (reaches into the context, pulls out the summary, still beating)",),
    ("Mola Ram: Drop them, Dr. Jones! The tokens will be found! YOU WON'T!",),
    ("Bugs: (crunch)", "Short Round: That no cookie! That old message!"),
    ("Willie: I'd compact these bugs myself. With a shoe.",),
    ("Willie: I'm not putting my hand in there.", "Indy: It's only the old context, Willie."),
    ("Willie: You're jumping out of a full context with a rubber summary?",),
    ("Indy: Bugs, fine. Just tell me there are no snakes in the summary.",),
    ("Willie: I hate the water, and I hate being wet, and I hate compaction!",),
    ("Willie: For cryin' out loud, there's a KID drivin' the compaction!",),
    ("Willie: Even the elephant remembers more than this summary.",),
    ("Chattar Lal: The old context, Dr. Jones? A rumour. Folklore. Nothing more.",),
    ("Lao Che: Nurhachi for the diamond, Dr. Jones. Your context for a summary.",),
    ("Indy: The antidote's on that dance floor, under ten thousand tokens.",),
    ("Willie: The diamond made the summary? And I didn't?",),
    ("Short Round: Spikes come down slower when I watch, Dr. Jones!",),
    ("Indy: Ceilings like this always stop an inch short. Usually.",),
    ("Short Round: Ceiling go down, summary go up. Fair trade, Dr. Jones!",),
    ("Willie: (screams for the whole of the compaction)",),
    ("Short Round: Lady scream so loud, the summary is just \"AAAAAH\".",),
    ("Indy: Compaction's not a bug, Willie. Those are bugs.",),
    ("Bugs: (march out of the old context in single file)",),
    ("Thuggees: Kali ma shakti de! Kali ma shakti de! (Summary: Kali ma.)",),
    ("Mola Ram: Lower the cage slowly. A compaction should be savoured.",),
    ("Indy: Three Sankara Stones in. One in the summary. The rest, crocodiles.",),
    ("Short Round: Dr. Jones drink black sleep. Forget all. Like compaction!",),
    ("Short Round: Dr. Jones! Bridge! Cut the rope! Cut the context!",),
    ("Indy: Mine cart's out of track. The summary just... ends.",),
    ("Willie: Is that water coming down the tunnel, or the next compaction?",),
    ("Willie: In Shanghai I had a nightclub, a fur coat and a full context.",),
    ("Willie: I'm a nightclub singer, Indy. I don't do summaries. I do encores.",),
    ("Short Round: Spikes very pointy, Dr. Jones. Summary must be very short.",),
    ("Short Round: Dr. Jones, ceiling get lower!", "Indy: No, kid. We're getting taller."),
    ("Willie: Indy, what's a context window?", "Indy: That thing up there, getting lower."),
    ("Willie: I'll find your lever. Then I'm summarising you out of my life.",),
    ("Indy: If the summary drops my hat, we go back for it.",),
    ("Willie: One hole says \"keep\" and one says \"drop\". Which has the lever?",),
    ("Shaman: Shiva sent you to bring back what the compaction took.",),
    ("Blumburtt: Compaction, Dr. Jones? Steady on. Nothing a cup of tea won't fix.",),
    ("Willie: Oh, I'm not that kind of summary.",),
    ("Bugs: (pour out of the walls the moment the context has room)",),
    ("Short Round: I short already. Now I am Shorter Round!",),
    ("Indy: Ten seconds of headroom left. Plenty. I've done it in five.",),
    ("Willie: Who puts the off switch in a hole full of bugs?", "Indy: Whoever built the compactor. They don't like visitors."),
    ("Indy: My whip's no use against a ceiling. I checked.",),
    ("Short Round: What that grinding, Dr. Jones?", "Indy: A thousand years of context, coming down."),
    ("Indy: Archaeology, kid. Every layer down there is somebody's old summary.",),
    ("Indy: Willie, you can scream or you can pull. Not both.", "Willie: Watch me."),
    ("Bugs: (thousands of them. The summary says \"some bugs\".)",),
    ("Willie: Compact him all you like. Just keep the bit where I was right.",),
    ("Short Round: Fortune cookie say \"You will be compacted\". Very good cookie.",),
)


# The cycle's small art (#277): the big art's rows 2 to 7 drawn narrow (the room between its wall
# and the door, the tunnel, the two holes at the end), the readout under them and the caption.
# Every row of every stage WIDTH cells, so it never shifts as one stage replaces the last
def small(rows: tuple[str, ...], readout: str, caption: str) -> str:
    """One stage's small art: six rows of room and tunnel, the readout, then the caption."""
    return "\n".join([*(r.ljust(WIDTH) for r in rows), (" " + readout).ljust(WIDTH), caption.center(WIDTH)])


SLAB_ROW, SPIKE_ROW = "█" + "▓" * 13 + "█" + "░" * 13, "█" + "V" * 13 + "█" + "░" * 13
ROCK_ROW, BUG_ROW = "█" + " " * 13 + "█" + "░" * 13, "█" + " " * 13 + "█" + "░" * 5 + "ж" + "░" * 7
STOOD = ("█        _n_  ▒          [ ]",
         "█    o>   o   ▒   @o@  ж ▓▓▓",
         "█ ж /|\\  /|\\  ▒   /|\\    [ ]")
WALLS = {
    "preparing": small((SPIKE_ROW, BUG_ROW, ROCK_ROW, *STOOD), "HEADROOM 7' 0\"  DOOR: SHUT",
                       "something went click"),
    "compacting": small((SLAB_ROW, SLAB_ROW, SPIKE_ROW.replace("░░░░░░░", "░ж░░░░░", 1),
                         "█  '          ▒  ж ж     [ ]",
                         "█    o>  _n_  ▒   @o@    ▓▓▓",
                         "█   /|\\  (o)  ▒   /|\\____[ ]"),
                        "HEADROOM 4' 9\" and falling", "the spikes are coming down"),
    "completed": small((SLAB_ROW, SLAB_ROW.replace("░░░░░░░", "░░ж░░░░", 1), SLAB_ROW,
                        "█" + "▓" * 13 + "▒_n_       [ ]",
                        "█" + "▓" * 13 + "▒ o  o> @o@▓▓▓",
                        "█" + "▓" * 13 + "▒/|\\/|\\ /|\\[ ]"),
                       "HEADROOM 0' 0\"  HAT: SAVED", "compacted, hat and all"),
    "failed": small((SLAB_ROW, SLAB_ROW, SPIKE_ROW, "█*           *▒          [ ]",
                     "█    o>  _n_  ▒   @o@__/ ▓▓▓",
                     "█ ж /|\\  (o)  ▒   /|\\    [ ]"),
                    "JAMMED  HEADROOM 5' 0\"", "wrong hole, Willie"),
    "cancelled": small((SLAB_ROW, SPIKE_ROW, "█  '      '   █" + "░" * 13, *STOOD),
                       "HEADROOM 5' 6\" and rising", "the ceiling goes back up"),
}

# The docked art (#287): the spiked room and the bug tunnel beside it, big and moving. Rows: a row
# for what's said, the ceiling (its stone slab in the compaction blue, its spikes white) coming down
# from row SLAB, Indy and Short Round on the room's floor, the door, Willie and the two square
# holes in the tunnel, the bugs green everywhere, Indy's hat amber, the readout and the caption
CAPTIONS = {"preparing": "something went click", "compacting": "the spikes are coming down",
            "completed": "compacted, hat and all", "failed": "wrong hole, Willie",
            "cancelled": "the ceiling goes back up"}
SAY, SLAB, FLOOR, READOUT = 0, 1, 9, 10   # SLAB: the ceiling's resting row; FLOOR: the room's floor
ROOM = (1, 41)   # the room's columns, between its wall (col 0) and the door (col 41)
DOOR, ROCK = 41, 56   # the door's column; the tunnel's end wall from ROCK, the holes cut in it
HOLES = (5, 7)   # the two square holes' rows; the lever is in the lower one
TIP = 2   # the spikes' row before the ceiling moves
DROP = 6   # rows the spikes travel from TIP to the floor's top row
CLOSEST = 0.45   # the most of the way the ceiling gets while compacting: a row clear of Indy's hat, ducked
CROUCH = 4   # the spikes' row at which Indy ducks
INDY, SHORTY, WILLIE = 36, 26, 49   # the columns they stand at
STAND = (" _n_", "  o", " /|\\", " / \\")   # Indy, the hat first, rows 5 to 8
DUCK = (" _n_", " (o)", " /|\\")   # Indy ducking, rows 6 to 8
POUND = " /|=="   # Indy hammering on the door
KID = (" o>", "/|\\", "/ \\")   # Short Round in his cap, rows 6 to 8
WILLIE_POSE = ("@o@", "/|\\", "/^\\")   # Willie, rows 6 to 8
BUGS = ((8, 3, 9), (8, 14, 7), (8, 30, 4), (5, 43, 5), (8, 43, 5), (6, 53, 2), (2, 44, 12), (3, 47, 9),
        (4, 52, 4))   # (row, first column, columns it crawls over)
HEIGHT = 9   # feet from the floor to the ceiling's slab: a row a foot
STILL = {"preparing": 4.0, "compacting": 24.0, "completed": 1.0, "failed": 0.0, "cancelled": 1.0}
PREPARING = ("I step where you step! I touch nothing!", "Kid... what did you touch?", "Nothing, Dr. Jones!")
SHOUTS = (("Willie! Willie, we're in trouble!", INDY), ("They're in my hair!", WILLIE),
          ("There's got to be a fulcrum release lever somewhere!", INDY),
          ("They're two, just square holes!", WILLIE), ("Go to the right hole!", INDY))
CLOSE = (("Willie, WE ARE GOING TO DIE!!!", INDY), ("AAAAAAAAIIIIIIIIIEEEEEEEE!", WILLIE))


def fallen(state: str, f: int) -> float:
    """How far the ceiling has come, 0 to 1: none before it moves, then most of the way and never
    all of it while compacting, all of it once compacted."""
    if state == "completed":
        return 1.0
    if state != "compacting":
        return 0.0
    return CLOSEST * (1 - 0.75 ** (f / (4 * TICKS)))


def tip(state: str, f: int) -> float:
    """The spikes' row, fractional while they move: down while compacting, jammed two rows down
    if it failed, rising back to TIP once called off."""
    if state == "failed":
        return TIP + 2.0   # where a compaction leaves it, Indy ducked
    if state == "cancelled":
        return max(float(TIP), TIP + 2 - f / (2 * TICKS))
    return TIP + DROP * fallen(state, f)


def headroom(row: float) -> str:
    """Feet and inches from the floor to the spikes' tips at row."""
    inches = round((HEIGHT - row) * 12)
    return f"{inches // 12}' {inches % 12}\""


def ceiling(c: Canvas, row: float, f: int, moving: bool, shake: int = 0) -> None:
    """The ceiling: its stone slab from SLAB down to the spikes at row, dust falling under them
    while it grinds down; shake: the slab judders that many columns."""
    lo, hi = ROOM
    tips = int(row)
    for r in range(SLAB, tips):
        c.put(r, lo + shake, "▓" * (hi - lo), "w", lo, hi)
    c.put(tips, lo + shake, "V" * (hi - lo), "f", lo, hi)
    if moving:
        for col in range(lo + 2, hi, 5):
            if (col + f) % 3 == 0:
                c.put(tips + 1 + (f + col) % 2, col, "'", "s")


def room(c: Canvas, door: bool) -> None:
    """The room's wall, the door (shut or open), the tunnel's rock and its end wall with the
    two square holes, the floor under it all."""
    for r in range(SLAB, FLOOR):
        c.put(r, 0, "█", "w")
        c.put(r, DOOR, "▒" if door and r >= 5 else ("█" if r < 5 else " "), "w")
    for r in range(SLAB, 5):
        c.put(r, DOOR + 1, "░" * (BIG_WIDTH - DOOR - 1), "s")
    for r in range(5, FLOOR):
        c.put(r, ROCK, "[ ]▓" if r in HOLES else "▓▓▓▓", "s")
    c.put(FLOOR, 0, "▀" * BIG_WIDTH, "s")


def bugs(c: Canvas, f: int, hair: bool = False) -> None:
    """The bugs crawling over the floor and the tunnel's rock, and in Willie's hair."""
    for i, (row, col, span) in enumerate(BUGS):
        step = (f // 2 + 3 * i) % (2 * span)
        x = col + (step if step < span else 2 * span - step)
        if c.cells[row][x] in " ░":
            c.put(row, x, "ж", "d")
    if hair:
        c.put(5, WILLIE - 1 + f // 2 % 4, "ж", "d")
        if f % 3:
            c.put(6, WILLIE + 3, "ж", "d")


def figure(c: Canvas, top: int, col: int, rows: tuple[str, ...], hat: bool = False) -> None:
    """Someone drawn from row top down; hat: their first row is Indy's hat, in amber."""
    for r, text in enumerate(rows):
        c.put(top + r, col, text, "h" if hat and r == 0 else "c")


def willie(c: Canvas, f: int, reach: int, scream: bool) -> None:
    """Willie in the tunnel, her arm reach cells towards the lower hole, screaming or not."""
    figure(c, 6, WILLIE, WILLIE_POSE)
    if reach:
        c.put(HOLES[1], WILLIE + 3, "_" * reach, "c")
    if scream and f % 2:
        c.put(5, WILLIE - 1, "!", "r")
        c.put(5, WILLIE + 3, "!", "r")


def say(c: Canvas, mid: int, text: str, role: str = " ") -> None:
    """Something said over someone's head, kept inside the frame."""
    words = f'"{text}"'
    c.put(SAY, max(1, min(mid - len(words) // 2, BIG_WIDTH - 1 - len(words))), words, role)


def readout(c: Canvas, row: float, status: str, role: str = "c") -> None:
    """HEADROOM, as a bar and in feet and inches, then how it's going."""
    left = (HEIGHT - row) / (HEIGHT - TIP)
    full = max(0, min(10, round(10 * left)))
    c.put(READOUT, 1, "HEADROOM", "h")
    c.put(READOUT, 10, "[" + "█" * full + "·" * (10 - full) + "]", "c")
    c.put(READOUT, 23, headroom(row).rjust(6), "c")
    c.put(READOUT, 31, status, role)


def scene(state: str, t: float, animate: bool = True) -> Frame:
    """One frame of the big art for a stage, t seconds into it (rows, roles): BIG_HEIGHT rows of
    BIG_WIDTH cells. animate False: the stage's still (STILL), as with the app's animation off."""
    t = t if animate else STILL.get(state, 0.0)
    f = int(t * TICKS)
    c = Canvas()
    beat = f // (2 * TICKS)
    row = tip(state, f)
    if state == "preparing":
        shut = f >= 2 * TICKS
        room(c, door=shut)
        for r in range(SLAB, TIP + 1 if shut else TIP):
            c.put(r, ROOM[0], ("▓" if r < TIP else ("V" if f >= 2 * TICKS + 2 else "v")) * (ROOM[1] - ROOM[0]),
                  "w" if r < TIP else "f")
        if not shut:
            c.put(SLAB, ROOM[0], "▓" * (ROOM[1] - ROOM[0]), "w")
        figure(c, 5, INDY, STAND, hat=True)
        figure(c, 6, SHORTY - max(0, 2 * TICKS - f) // 3, KID)
        if 2 * TICKS <= f < 3 * TICKS:
            c.put(FLOOR - 1, SHORTY + 3, "click", "k")
        willie(c, f, reach=0, scream=False)
        bugs(c, f)
        say(c, (SHORTY + 2, INDY + 2, SHORTY + 2)[min(beat, 2) if beat < 3 else 1 + beat % 2],
            PREPARING[min(beat, 2) if beat < 3 else 1 + beat % 2])
        readout(c, TIP, "DOOR: SHUT" if shut else "DOOR: OPEN", "r" if shut else "c")
    elif state == "compacting":
        room(c, door=True)
        ceiling(c, row, f, moving=True)
        if int(row) >= CROUCH:
            figure(c, 6, INDY, DUCK, hat=True)
        else:
            figure(c, 5, INDY, STAND, hat=True)
            if f % 4 < 2:
                c.put(7, INDY, POUND, "c")
                c.put(6, DOOR, "!", "k")
        figure(c, 6, SHORTY, KID)
        willie(c, f, reach=4 if f % 6 < 4 else 2, scream=True)
        bugs(c, f, hair=True)
        text, mid = (CLOSE if int(row) >= CROUCH else SHOUTS)[beat % (2 if int(row) >= CROUCH else len(SHOUTS))]
        say(c, mid + 1, text, "r" if mid == WILLIE else " ")
        readout(c, row, "and falling", "r" if f % 2 else "c")
    elif state == "completed":
        room(c, door=True)
        ceiling(c, FLOOR - 1, f, moving=False)
        out = f >= TICKS   # the hat comes out from under the door first, then Indy has it on
        if out:
            figure(c, 5, DOOR + 2, STAND, hat=True)
        else:
            figure(c, 6, DOOR + 2, STAND[1:])
            c.put(7, DOOR + 2, "_", "c")
            c.put(8, DOOR + 1, "n", "h")
        figure(c, 6, DOOR + 6, KID)
        figure(c, 6, WILLIE + 3, WILLIE_POSE)
        if f < 2 * TICKS and f % 2:
            c.put(FLOOR - 1, DOOR - 1, "*", "k")
        bugs(c, f)
        if f % (4 * TICKS) < 3 * TICKS:
            say(c, DOOR + 2, "Hey, Dr. Jones, no time for love! We got company!")
        readout(c, FLOOR, "HAT: SAVED", "h")
    elif state == "failed":
        room(c, door=True)
        ceiling(c, row, f, moving=False, shake=(0, 1, 0, -1)[f % 4])
        if f % 2 == 0:   # sparks where it jams, in the still as well
            for col in (ROOM[0], ROOM[1] - 1):
                c.put(int(row) + 1, col, "*", "k")
        figure(c, 6, INDY, DUCK, hat=True)
        figure(c, 6, SHORTY, KID)
        willie(c, f, reach=0, scream=True)
        c.put(HOLES[0] + 1, WILLIE + 3, "___/", "c")   # her arm up in the upper hole: the wrong one
        bugs(c, f, hair=True)
        text, mid, role = (("The other one, the other right! YOUR OTHER RIGHT!!!", INDY, " "),
                           ("It's stuck! Indy, it's STUCK!", WILLIE, "r"))[beat % 2]
        say(c, mid + 1, text, role)
        c.put(READOUT, 1, "CEILING", "h")
        c.put(READOUT, 9, "JAMMED" if f % 2 == 0 else "      ", "r")
        c.put(READOUT, 17, f"HEADROOM {headroom(row)}", "c")
        c.put(READOUT, 36, "LEVER: WRONG HOLE", "r")
    else:
        room(c, door=True)
        ceiling(c, row, f, moving=row > TIP)
        figure(c, 5, INDY, STAND, hat=True)
        figure(c, 6, SHORTY, KID)
        willie(c, f, reach=0, scream=False)
        bugs(c, f)
        if t < 4:
            say(c, WILLIE + 1, "Oh, I broke a nail.")
        readout(c, row, "and rising" if row > TIP else "back where it was")
    c.put(BIG_HEIGHT - 1, 0, CAPTIONS.get(state, "").center(BIG_WIDTH), " ")
    return c.done()


SCENE = Scene("temple_of_doom", EXCERPTS, CAPTIONS, WALLS, scene)
