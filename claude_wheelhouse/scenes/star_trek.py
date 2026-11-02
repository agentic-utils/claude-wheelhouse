"""Star Trek, the engine room (#304, scene A, "She cannae take much more"): the context is the
pattern buffer and Scotty is cramming it in. The warp core pulses faster through compacting, the
CONTEXT gauge falls from full to a sliver, steam vents from the bulkheads, and Scotty thumps the
console while Kirk paces and Spock looks on.

Each excerpt is one line or a two-line exchange, drawn at random without repeats for the length
of one compaction cycle (compactor.schedule).
"""

from __future__ import annotations

from .base import BIG_WIDTH, TICKS, WIDTH, Canvas, Frame, Scene

EXCERPTS: tuple[tuple[str, ...], ...] = (
    ("Kirk: Scotty, I need that context compacted in two minutes.",
     "Scotty: Two minutes? It cannae be done in under ten!"),
    ("Scotty: She cannae take much more o' this, Captain!",),
    ("McCoy: Dammit Jim, I'm a doctor, not a garbage collector!",),
    ("Spock: Fascinating. Ninety percent of this session was the word \"actually\".",),
    ("Uhura: Captain, the session is hailing us. It wants its tokens back.",),
    ("Kirk: Scotty...", "Scotty: I KNOW, Captain!"),
    ("Kirk: Do you always multiply your compaction estimates by four, Scotty?",
     "Scotty: Certainly. How else can I keep my reputation as a miracle worker?"),
    ("Scotty: Overthink the plumbing and ye stop up the drain. Same wi' context.",),
    ("Scotty: Admiral, there be tokens here!",),
    ("Kirk: Scotty, where did all those tool results go?",
     "Scotty: Into the Klingons' engine room, where they'll be no tribble at all."),
    ("Kirk: Bones, the first two hours of this session?", "McCoy: They're dead, Jim."),
    ("Kirk: CONTEEEEEXT!",),
    ("Spock: The needs of the summary outweigh the needs of the many.", "McCoy: Tell that to the many."),
    ("Chekov: Compaction? Inwented by a little old lady from Leningrad.",),
    ("Sulu: My fencing practice has been summarised as \"stabbed at things\".",),
    ("Uhura: Captain, the Klingons want a copy of the summary.", "Kirk: Send it. Let them suffer."),
    ("Scotty: Ye cannae squeeze three hours o' talk intae a paragraph!",
     "Spock: Mr Scott, that is precisely what compaction is."),
    ("Scotty: The dilithium's crackin', Captain! Too many tool results!",),
    ("Spock: I recommend we forget everything but the plan, Captain.", "Kirk: Which plan?"),
    ("McCoy: Spock, you've compressed me into a bullet point.", "Spock: A very short one, Doctor."),
    ("Spock: Logic dictates we keep the decisions and drop the small talk.",
     "McCoy: There goes my entire contribution."),
    ("Kirk: Scotty, can you reroute the context through the deflector dish?",
     "Scotty: Captain, ye can reroute anythin' through the deflector dish."),
    ("Kirk: Mr Scott, I'm counting on you.", "Scotty: Aye, and I'm countin' tokens, sir."),
    ("Spock: Mr Scott, the summary has dropped your accent.",
     "Scotty: It has no'! I'm still here, ye ken!"),
    ("Scotty: I know this context like the back o' my hand.", "McCoy: Then why did you just walk into it?"),
    ("Kirk: Risk is our business. Forgetting, apparently, is also our business.",),
    ("Uhura: Captain, Starfleet wants a summary of the summary.",),
    ("Chekov: Keptin, context at fifty percent and falling!", "Kirk: Steady, Mr Chekov."),
    ("Sulu: Captain, we're down to three bullet points.", "Kirk: Make them count, Mr Sulu."),
    ("Spock: I have been, and always shall be, your summary.",),
    ("McCoy: What did you do to him, Spock?",
     "Spock: A mind meld. He now remembers only the last ten minutes."),
    ("Spock: Remember.", "McCoy: Remember WHAT? It's all been compacted!"),
    ("Kirk: Energise.", "Scotty: Aye, but he'll rematerialise as a summary, sir."),
    ("Scotty: The pattern buffer's full, Captain! Somethin's got tae go!",),
    ("McCoy: What is this, the Dark Ages? Compacting a conversation with a hammer!",),
    ("Kirk: Two lines from the transcript, Scotty. That's all I ask.",
     "Scotty: Two? Ye'll get one and like it."),
    ("Scotty: I'll have tae take the old jokes offline, Captain.", "Kirk: Not the old jokes!"),
    ("Uhura: I'm picking up a faint signal, Captain. It's the original prompt.",),
    ("Chekov: Keptin, the scroll bar has disappeared!",),
    ("Sulu: Captain, I've forgotten our heading. And our destination. And why.",),
    ("Scotty: My poor bairns! Look what they've done tae yer history!",),
    ("Scotty: She's held thegither wi' summaries and string, Captain.",),
    ("Kirk: Scotty, report!", "Scotty: She's a wee bit lighter, Captain, and a lot more forgetful."),
    ("Spock: With so little context, I shall have to make a guess.",
     "Kirk: A guess? You, Spock? That's extraordinary."),
    ("Uhura: Mr Spock's logs compressed better than anyone's, Captain.",
     "McCoy: No surprise. There was nothing in them."),
    ("Scotty: Aye, it'll fit. I got two humpbacks intae a Klingon ship.",),
    ("Scotty: I'm an engineer, not a... och, wait, that's the Doctor's line.",),
    ("Spock: Highly illogical to keep four copies of the same stack trace.",),
    ("Kirk: Space. The final frontier. And now, the final paragraph.",),
    ("Spock: Captain, the summary describes this mission as \"some stuff happened\".",
     "Kirk: Accurate."),
    ("Uhura: Incoming from engineering, Captain. It's mostly swearing.", "Kirk: Put it on screen."),
    ("Scotty: Dinnae touch that lever! That's the bit wi' the requirements!",),
    ("Scotty: Who left a fifty-thousand-line log file in my warp core?",),
    ("Kirk: How much did we lose, Spock?",
     "Spock: Everything but the conclusions. Which is, logically, the point."),
    ("McCoy: You're telling me we can't remember the last four hours?",
     "Spock: I am telling you we no longer need to."),
    ("Sulu: Warp factor seven, Captain. Context factor two.",),
    ("Kirk: Scotty, can you give me more context?",
     "Scotty: No, Captain, I'm givin' ye less. That's the whole point!"),
    ("Spock: The transcript is ninety percent shorter. It is now readable.",),
    ("McCoy: You can't just delete a man's anecdotes, Spock!", "Spock: I believe you will find that I can, Doctor."),
    ("Kirk: Mr Spock, you have the conn.", "Spock: I have a summary of the conn, Captain."),
    ("Uhura: Captain, the summary has me down as \"hailing frequencies\". Again.",),
    ("Scotty: I'm puttin' the old tool output oot the airlock, Captain.", "Kirk: All of it?"),
    ("Kirk: Engineering, status!",
     "Scotty: Every warnin' light's on, sir, and two I didnae know we had!"),
    ("Scotty: I gave her a good hard thump. She likes that.",),
    ("Spock: Mr Scott, striking the console is not an engineering procedure.",
     "Scotty: It is in Aberdeen."),
    ("Scotty: Every time ye say \"just one more thing\", a dilithium crystal dies.",),
    ("Spock: Captain, the context window is now more of an aperture.",),
    ("Kirk: What's left, Bones?", "McCoy: A to-do list and a vague sense of dread."),
    ("Chekov: Keptin, course plotted to the end of the summary. It is wery short.",),
    ("Scotty: Gie's a minute, Captain, she's still squeezin'.",),
    ("Kirk: Is it done, Scotty?", "Scotty: Done? Ye'll ken it's done when the core stops greetin'."),
    ("Spock: Live long and summarise.",),
    ("McCoy: The redshirt, Jim. He's been summarised.", "Kirk: What did it say?"),
    ("Scotty: I've no' had a day like this since the tribbles got intae the logs.",),
)


# The cycle's small art (#277): the engine room in WIDTH cells, the sign or the alert light over
# Scotty, the warp core in the middle, Kirk across from him, and the CONTEXT gauge on the deck.
# Every row of every stage WIDTH cells, so it never shifts as one stage replaces the last
SMALL_GAUGE = 12


def small(scotty: tuple[str, str, str], top: str, core: str, kirk: tuple[str, str, str], fill: float,
          caption: str) -> str:
    """One stage's small art: four rows of the room, the gauge, then the caption."""
    left = (" " + top, *("  " + p for p in scotty))
    right = ("", *kirk)
    rows = [left[r].ljust(13) + "||=[" + core + "]=||" + right[r].center(5) for r in range(4)]
    cells = round(fill * SMALL_GAUGE)
    rows.append(f" CONTEXT [{'█' * cells}{'·' * (SMALL_GAUGE - cells)}] {round(fill * 100):3d}%")
    return "\n".join([*rows, caption.center(WIDTH)])


WALLS = {
    "preparing": small((" o ", "<|\\", "/ \\"), "ENGINEERING", "▒▒", (" o ", "<|>", "/ \\"), 1.0,
                       "she's no built for this"),
    "compacting": small(("\\o ", " |\\", "/ \\"), "ENGINEERING", "██", ("_o ", " |\\", "/ \\"), 0.5,
                        "givin' her all she's got"),
    "completed": small(("\\o/", " | ", "/ \\"), "ENGINEERING", "░░", (" o ", "<|>", "/ \\"), 0.08,
                       "compacted, Captain"),
    "failed": small(("|o|", " | ", "/ \\"), "RED ALERT", "▓░", ("_o ", " |\\", "/ \\"), 1.0,
                    "laws o' physics, Captain!"),
    "cancelled": small((" o ", "/|\\", "/ \\"), "ENGINEERING", "▒░", (" o ", "/|\\", "/ \\"), 1.0,
                       "stand down, Mr Scott"),
}

# The docked art (#287): the engine room big and moving. Rows: the deckhead, the room (the sign,
# the steam vents, the crew standing on FEET), the deck, the CONTEXT gauge and the caption. The
# core and the crew in the compaction blue, the sign and Kirk's command gold in amber, Scotty's
# engineering red and the red alert light in "alert"; sparks off the console in amber
CAPTIONS = {"preparing": "she's no built for this", "compacting": "givin' her all she's got",
            "completed": "compacted, Captain", "failed": "I cannae change the laws o' physics!",
            "cancelled": "stand down, Mr Scott"}
STILL = {"preparing": 0.0, "compacting": 37.75, "completed": 0.0, "failed": 0.0, "cancelled": 1.0}   # compacting: mid-thump; cancelled: part way back up
HEAD, DECK, GAUGE_ROW = 0, 9, 10
ROOM = range(1, 9)
FEET = 8
CORE = 24   # the core's first column: "||=[" + 4 plasma cells + "]=||", 12 wide
PLASMA = 4
VENTS = (2, 4)   # the bulkhead rows the steam comes out of, both sides
STEAM = "~≈~-~."   # a puff, nearest the vent first
CONSOLE = (("[:::]", "s"), ("|o.o|", "s"), ("|___|", "s"))   # Scotty's panel, rows 6 to 8
GAUGE_AT, GAUGE = 2, 40   # the gauge's first column and its bar's cells
SINK = 0.08   # the gauge's sliver once compacted
THUMP = 2 * TICKS   # Scotty's cycle while compacting: working, the arm up, the thump

SCOTTY = {"work": (" o ", "<|\\", "/ \\"), "fiddle": (" o ", "<|/", "/ \\"), "raise": ("\\o ", " |\\", "/ \\"),
          "thump": (" o ", "=|\\", "/ \\"), "cheer": ("\\o/", " | ", "/ \\"), "despair": ("|o|", " | ", "/ \\"),
          "clutch": ("/o\\", " | ", "/ \\"),
          "ease": (" o ", "/|\\", "/ \\")}
KIRK = {"hips": (" o ", "<|>", "/ \\"), "point": ("_o ", " |\\", "/ \\"), "ease": (" o ", "/|\\", "/ \\")}
SPOCK = {"still": (" o ", "(|)", "/ \\"), "scan": (" o ", "(|[", "/ \\"), "brow": (" o'", "(|)", "/ \\")}


def gauge(state: str, t: float) -> float:
    """How full the context is, t seconds into the stage: full while preparing, falling fast then
    ever slower while compacting (a long /compact never quite bottoms out), a sliver once compacted,
    jammed full on a failure, and refilling when called off."""
    if state == "compacting":
        return 1 - (1 - SINK - 0.02) * (1 - 0.5 ** (t / 20))
    if state == "completed":
        return SINK
    if state == "cancelled":
        return min(1.0, 0.5 + t / 4)
    return 1.0


def core(c: Canvas, state: str, t: float, f: int) -> None:
    """The warp core up the middle, its plasma pulsing upward: slow while preparing, faster and
    denser as compacting goes on (and the casing shuddering), a calm glow once compacted, a stutter
    on a failure, and slowing to a stop when called off."""
    shudder = 1 if state == "compacting" and t > 6 and f % 5 == 2 else 0
    at = CORE + shudder
    if state == "compacting":
        pos, gap = f, max(3, 8 - int(t // 8))
    elif state == "preparing":
        pos, gap = f // 2, 8
    elif state == "cancelled":
        pos, gap = int(TICKS * 2 * (1 - 0.5 ** t)), 8   # winding down to a stop
    else:
        pos, gap = f // 3, 8
    for row in ROOM:
        up = FEET - row   # 0 at the bottom of the core, rising
        if state == "failed":
            lit = (row * 5 + f * 3) % 7
            cells, role = ("████", "r") if lit == 0 else ("▓░▓░" if lit % 2 else "░░░░", "s")
        elif state == "completed":
            cells, role = ("▓▓▓▓", "c") if (up - pos) % gap == 0 else ("▒▒▒▒", "w")
        else:
            d = (up - pos) % gap
            cells, role = ("████", "c") if d == 0 else ("▓▓▓▓", "w") if d in (1, gap - 1) else ("░░░░", "s")
        c.put(row, at, "||=[", "w")
        c.put(row, at + 4, cells, role)
        c.put(row, at + 4 + PLASMA, "]=||", "w")


def steam(c: Canvas, state: str, t: float, f: int) -> None:
    """Steam puffing in from the bulkhead vents, both sides: hard while compacting, gusting on a
    failure, dying away when called off."""
    for i, row in enumerate(VENTS):
        c.put(row, 1, "#", "s")
        c.put(row, BIG_WIDTH - 2, "#", "s")
        if state == "compacting" or state == "failed" or (state == "cancelled" and t < 2):
            n = (f + i * 3) % (len(STEAM) + 2)
            puff = STEAM[:n] if state != "cancelled" else STEAM[:max(0, n - int(t * 3))]
            c.put(row, 2, puff, "c")
            c.put(row, BIG_WIDTH - 2 - len(puff), puff[::-1], "c")


def pose(state: str, t: float, f: int) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], int, int]:
    """Scotty's, Kirk's and Spock's poses, Kirk's column and how high Scotty's feet are off the deck."""
    if state == "preparing":
        return (SCOTTY["work"] if f // 4 % 2 else SCOTTY["fiddle"], KIRK["point"] if f // TICKS % 4 == 3 else KIRK["hips"],
                SPOCK["brow"] if f // TICKS % 5 == 4 else SPOCK["still"], 42, 0)
    if state == "compacting":
        beat = f % THUMP
        scotty = SCOTTY["thump"] if beat >= THUMP - 2 else SCOTTY["raise"] if beat >= THUMP - 4 else (
            SCOTTY["work"] if beat // 2 % 2 else SCOTTY["fiddle"])
        pace = (0, 1, 2, 3, 2, 1)[f // 3 % 6]
        return scotty, KIRK["point"] if f // 9 % 2 else KIRK["hips"], SPOCK["scan"], 40 + pace, 0
    if state == "completed":
        return SCOTTY["cheer"], KIRK["hips"], SPOCK["brow"] if f // TICKS % 2 else SPOCK["still"], 42, f // 2 % 2
    if state == "failed":
        return (SCOTTY["clutch"] if f // 3 % 2 else SCOTTY["despair"], KIRK["point"], SPOCK["brow"], 42, 0)
    return SCOTTY["ease"], KIRK["ease"], SPOCK["still"] if t < 2 or f // TICKS % 3 else SPOCK["brow"], 42, 0


def scene(state: str, t: float, animate: bool = True) -> Frame:
    """One frame of the big art for a stage, t seconds into it (rows, roles): BIG_HEIGHT rows of
    BIG_WIDTH cells. animate False: the stage's still (STILL), as with the app's animation off."""
    t = t if animate else STILL.get(state, 0.0)
    f = int(t * TICKS)
    c = Canvas()
    c.put(HEAD, 0, "▄" * BIG_WIDTH, "w")
    c.put(DECK, 0, "▀" * BIG_WIDTH, "w")
    for row in ROOM:
        c.put(row, 0, "▐", "w")
        c.put(row, BIG_WIDTH - 1, "▌", "w")
    c.put(1, 3, "ENGINEERING", "h")
    # the alert light: off, amber for yellow alert while compacting, red and fast on a failure
    alert = {"compacting": ("●" if f // 3 % 2 == 0 else "○", "h"), "failed": ("●" if f % 2 == 0 else "○", "r")}
    lamp, role = alert.get(state, ("○", "s"))
    c.put(1, 51, "(" + lamp + ")", role)
    if state == "failed" and f % 2 == 0:
        c.put(1, 39, "RED ALERT", "r")
    steam(c, state, t, f)
    core(c, state, t, f)
    for r, (panel, role) in enumerate(CONSOLE):
        c.put(FEET - 2 + r, 2, panel, role)
    blink = (f // 2) % 3
    c.put(FEET - 1, 3, "o.o" if blink else ".o.", "k")
    scotty, kirk, spock, kirk_at, hop = pose(state, t, f)
    for r in range(3):
        c.put(FEET - 2 + r - hop, 8, scotty[r], "r")
        c.put(FEET - 2 + r, kirk_at, kirk[r], "h")
        c.put(FEET - 2 + r, 50, spock[r], "c")
    if scotty is SCOTTY["thump"] or (state == "failed" and f % 3 == 0):   # sparks off the console
        for i, col in enumerate((2, 4, 6, 8)):
            if (i + f) % 3:
                c.put(FEET - 3, col, "*+'"[(i + f) % 3], "k")
    fill = gauge(state, t)
    cells = round(fill * GAUGE)
    c.put(GAUGE_ROW, GAUGE_AT, "CONTEXT [", "h")
    c.put(GAUGE_ROW, GAUGE_AT + 9, "█" * cells, "r" if state == "failed" else "c")
    c.put(GAUGE_ROW, GAUGE_AT + 9 + cells, "·" * (GAUGE - cells), "s")
    c.put(GAUGE_ROW, GAUGE_AT + 9 + GAUGE, f"] {round(fill * 100):3d}%", "h")
    c.put(GAUGE_ROW + 1, 0, CAPTIONS.get(state, "").center(BIG_WIDTH), " ")
    return c.done()


SCENE = Scene("star_trek", EXCERPTS, CAPTIONS, WALLS, scene)
