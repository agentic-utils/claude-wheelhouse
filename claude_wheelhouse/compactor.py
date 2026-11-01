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


# The cycle's art (#244, art B, the detention-level trash compactor; rows since #277): the walls
# thicken inward as it goes, the crew standing, then panicking, and gone by the end; failed and
# cancelled leave the walls open. Every row of every stage WIDTH cells, so it never shifts as one
# stage replaces the last in place. Plain Text in the compaction blue, never Markdown
WIDTH = 28
STAND = (" o    o    o ", "/|\\  /|\\  /|\\", "/ \\  / \\  / \\")
PANIC = ("\\o/\\o/\\o/", " |  |  | ", "/ \\/ \\/ \\")


def frame(wall: str, crew: tuple[str, ...], caption: str) -> str:
    """One stage's art: three rows of crew (or none) between the walls, the sludge, the caption."""
    inner = WIDTH - 2 * len(wall) - 2
    rows = ["▐" + wall + (crew[r] if r < len(crew) else "").center(inner) + wall[::-1] + "▌" for r in range(3)]
    rows.append("▐" + wall + "~" * inner + wall[::-1] + "▌")
    return "\n".join([*rows, caption.center(WIDTH)])


WALLS = {
    "preparing": frame("█▓", STAND, "walls closing in"),
    "compacting": frame("████▓", PANIC, "compaction initiated"),
    "completed": frame("█████████▓", (), "compacted"),
    "failed": frame("█▓", PANIC, "3PO, shut them down!"),
    "cancelled": frame("█▓", STAND, "compaction called off"),
}
SHOWN = ("preparing", "compacting")   # the states the crew panic through


def stage_text(state: str, origin: str | None, note: str, stamp: str = "") -> str | None:
    """A stage's art, then (dim) a line of who asked or what came of it, and when (stamp). None
    for a stage with no art (requested: usually under a second, and the eye covers it)."""
    if state not in WALLS:
        return None
    return WALLS[state] + "\n" + detail(state, origin, note, stamp)


def detail(state: str, origin: str | None, note: str, stamp: str) -> str:
    """The dim line under a stage's art: what came of it, else who asked, and when."""
    said = note or (f"asked for by {origin}" if state in SHOWN + ("requested",) and origin else "")
    return " · ".join(filter(None, (said, stamp)))


ENDS = ("completed", "failed", "cancelled")


def dock_text(state: str, since: float, origin: str | None, note: str, stamp: str) -> str:
    """The running cycle's "dock" entry (#287): its stage (requested drawn as preparing), when
    that stage began (epoch, the animation's time zero) and its dim line, one a line (docked)."""
    return "\n".join((state if state in WALLS else "preparing", repr(since), detail(state, origin, note, stamp)))


def docked(text: str) -> tuple[str, float, str]:
    """A dock entry's stage, start and dim line (dock_text)."""
    state, since, line = (text.split("\n", 2) + ["", ""])[:3]
    try:
        return state, float(since), line
    except ValueError:
        return state, 0.0, line


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


# The docked art (#287): the compactor big and moving, pinned under the conversation while a cycle
# runs, and its last frame left in the conversation when it ends. BIG_WIDTH by BIG_HEIGHT cells in
# every frame of every stage (a test pins it); a pane narrower than BIG_WIDTH gets WALLS instead.
# A frame is its rows and, cell for cell, the role each cell is drawn in (ROLES), so the app colours
# it without parsing the art: walls and crew in the compaction blue, sparks and the hatch light in
# amber, the dianoga in green. Everything moves on TICKS frames a second of the stage's own time, so
# any rate draws the same scene, and a still (animation off) is the frame at STILL[state]
BIG_WIDTH, BIG_HEIGHT = 60, 12   # 11 rows of art and the caption
TICKS = 6
ROLES = {"w": "wall", "s": "seam", "c": "crew", "g": "sludge", "k": "spark", "h": "hatch", "d": "dianoga", " ": "text"}
CAPTIONS = {"preparing": "walls closing in", "compacting": "compaction initiated", "completed": "compacted",
            "failed": "3PO, shut them down!", "cancelled": "compaction called off"}
STILL = {"preparing": 0.0, "compacting": 40.0, "completed": 0.0, "failed": 0.0, "cancelled": 1.5}   # cancelled: part way back out
RIM, FLOOR, CHAMBER, SLUDGE = 0, 10, range(1, 8), (8, 9)   # the rows: hatch rim, floor, air and crew, sludge
FEET = 7   # the row the crew stand on
OPEN, SHUT = 3, 29   # wall thickness, each side, open and met (58 inner cells between the frame)
# the crew, distinguishable: Han's vest and blaster, Leia's buns, Luke plain (and the one the
# dianoga wants), Chewie tall and furry. Each pose top row first, every row the same width
CREW = {
    "han": {"stand": (" o ", "/H>", "/ \\"), "panic": ("\\o/", " H ", "/ \\"), "cheer": "\\o/"},
    "leia": {"stand": ("@o@", "/|\\", "/^\\"), "panic": ("@o@", "\\|/", "/^\\"), "cheer": "@o@"},
    "luke": {"stand": (" o ", "/|\\", "/ \\"), "panic": ("\\o/", " | ", "/ \\"), "grabbed": ("\\o/", " |)", " (("),
             "cheer": "\\o/"},
    "chewie": {"stand": ("{o}", "/#\\", "|#|", "/ \\"), "panic": ("{O}", "\\#/", "|#|", "/ \\"), "cheer": "{#}"},
}
ORDER = ("han", "leia", "luke", "chewie")
GARBAGE = ((0.08, "[]"), (0.27, "%"), (0.45, "o="), (0.62, "&"), (0.8, "<>"), (0.95, "#"))
GRAB, GRAB_FOR = 15, 2.0   # the tentacle takes Luke every GRAB seconds, for GRAB_FOR
EYE, EYE_FOR = 7, 2.0   # the dianoga's eye surfaces every EYE seconds, for EYE_FOR


def walls(state: str, t: float) -> int:
    """Each wall's thickness at t seconds into the stage: still while preparing, grinding in
    (fast, then ever slower: a long /compact never quite shuts them) while compacting, met once
    compacted, stopped where the droids caught them on a failure, back out when called off."""
    if state == "compacting":
        return OPEN + 1 + round(13 * (1 - 0.5 ** (t / 30)))
    if state == "completed":
        return SHUT
    if state == "failed":
        return 10
    if state == "cancelled":
        return max(OPEN, 10 - int(t * 2))
    return OPEN


class Canvas:
    """A frame being drawn: rows of cells and the role of each."""

    def __init__(self) -> None:
        self.cells = [[" "] * BIG_WIDTH for _ in range(BIG_HEIGHT)]
        self.roles = [[" "] * BIG_WIDTH for _ in range(BIG_HEIGHT)]

    def put(self, row: int, col: int, text: str, role: str, lo: int = 0, hi: int = BIG_WIDTH) -> None:
        """text from col on row, in role, clipped to the columns lo to hi."""
        for i, ch in enumerate(text):
            if lo <= col + i < hi and 0 <= row < BIG_HEIGHT and ch != "\0":
                self.cells[row][col + i], self.roles[row][col + i] = ch, role

    def done(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return tuple("".join(r) for r in self.cells), tuple("".join(r) for r in self.roles)


def wall_cell(row: int, depth: int) -> tuple[str, str]:
    """A wall's cell depth cells back from its face: the face, then plate with seams, rivets
    and the odd greeble, all fixed to the face, so they ride in with it as it grinds."""
    if depth == 0:
        return "▓", "w"
    if row in (1, 5, 9) and depth % 4 == 2:
        return "o", "s"   # rivets
    if depth % 6 == 5:
        return "▒", "s"   # plate seams
    if (row * 7 + depth * 3) % 23 == 0:
        return "=", "s"   # greebles
    return "█", "w"


def scene(state: str, t: float, animate: bool = True) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """One frame of the big art for a stage, t seconds into it (rows, roles): BIG_HEIGHT rows of
    BIG_WIDTH cells. animate False: the stage's still (STILL), as with the app's animation off."""
    t = t if animate else STILL.get(state, 0.0)
    f = int(t * TICKS)
    c = Canvas()
    moving = state == "compacting"
    w = walls(state, t)
    # the rim, with the hatch light: slow blink waiting, fast while grinding, steady at the end
    c.put(RIM, 0, "▄" * BIG_WIDTH, "w")
    lit = {"preparing": f // TICKS % 2 == 0, "compacting": f % 2 == 0, "cancelled": False}.get(state, True)
    c.put(RIM, BIG_WIDTH // 2 - 2, "[" + ("●" if lit else "○") + "]", "h")
    c.put(FLOOR, 0, "▀" * BIG_WIDTH, "w")
    for row in (*CHAMBER, *SLUDGE):
        c.put(row, 0, "▐", "w")
        c.put(row, BIG_WIDTH - 1, "▌", "w")
        jolt = 1 if moving and (f + row) % 4 == 0 else 0   # the grind: a row lurching a cell ahead
        depth = min(SHUT, w + jolt)
        for d in range(depth):
            ch, role = wall_cell(row, d)
            c.put(row, depth - d, ch, role)
            c.put(row, BIG_WIDTH - 1 - depth + d, ch, role)
    lo, hi = w + 1, BIG_WIDTH - 1 - w   # the room left between the faces
    room = hi - lo
    # the sludge, rippling, and what floats in it bobbing
    for row in SLUDGE:
        c.put(row, lo, "".join("~" if (i + f // 2 + row) % 3 else "-" for i in range(room)), "g", lo, hi)
    for i, (at, junk) in enumerate(GARBAGE):
        if room > len(junk) + 2:
            c.put(SLUDGE[(f // 3 + i) % 2], lo + 1 + int(at * (room - len(junk) - 2)), junk, "g", lo, hi)
    alive = state in ("preparing", "compacting")
    if alive and t % EYE < EYE_FOR and room > 6:   # the dianoga's eye, blinking once as it looks round
        x = lo + 2 + int(t // EYE * 11) % max(1, room - 6)
        c.put(SLUDGE[0], x, "(-)" if f % 5 == 0 else "(@)", "d", lo, hi)
    grabbed = moving and GRAB / 2 <= t % GRAB < GRAB / 2 + GRAB_FOR
    if state == "completed":   # squeezed out of the top: on the rim, cheering
        for i, who in enumerate(ORDER):
            pose = CREW[who]["cheer"] if f % 4 < 2 else "_" + CREW[who]["cheer"][1] + "_"
            c.put(RIM, (6, 16, 40, 50)[i], pose, "c")
    elif room >= 4:
        wide = sum(len(CREW[w_]["stand"][0]) for w_ in ORDER)
        gap = max(0, (room - wide) // (len(ORDER) + 1))
        x = lo + gap
        for i, who in enumerate(ORDER):
            poses = CREW[who]
            cheering = state == "failed"   # the droids stopped the walls: arms up, jumping for joy
            pose = (poses["grabbed"] if who == "luke" and grabbed else
                    poses["panic"] if moving and (f // 2 + i) % 3 or cheering else poses["stand"])
            dodge = ((f // 3 + i * 2) % 3 - 1) if moving else 0
            col = min(max(lo, x + dodge), hi - len(pose[0]))
            top = FEET - len(pose) + 1 + (1 if who == "luke" and grabbed else 0) - (1 if cheering and (f // 2 + i) % 2 else 0)
            for r, line in enumerate(pose):
                c.put(top + r, col, line, "c", lo, hi)
            if who == "luke" and grabbed:   # the tentacle, wriggling up out of the sludge round him
                for r in range(top, SLUDGE[0] + 1):
                    c.put(r, col + len(pose[0]), ")" if (r + f) % 2 else "(", "d", lo, hi)
                c.put(SLUDGE[0], col - 1, "@", "d", lo, hi)
            x += len(poses["stand"][0]) + gap
    if moving:   # sparks off both faces, a few rows a frame
        for row in CHAMBER:
            spark = (f * 7 + row * 13) % 11
            if spark < 3:
                c.put(row, lo + (spark % 2), "*+'"[spark], "k", lo, hi)
            if (spark + 5) % 11 < 3:
                c.put(row, hi - 1 - (spark % 2), ".*,"[(spark + 5) % 11], "k", lo, hi)
    c.put(BIG_HEIGHT - 1, 0, CAPTIONS.get(state, "").center(BIG_WIDTH), " ")
    return c.done()


def still(state: str) -> str:
    """A stage's big still frame as plain text (animation off, or its last frame left in the
    conversation)."""
    return "\n".join(scene(state, 0.0, animate=False)[0])


def state_of(art: str) -> str | None:
    """Which stage a stage entry's art (WALLS) shows, from its rows: None for any other text."""
    for state, rows in WALLS.items():
        if art.startswith(rows):
            return state
    return None
