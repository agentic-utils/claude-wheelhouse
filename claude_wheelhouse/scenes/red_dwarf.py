"""Red Dwarf, Holly's senility (#304, scene B, "Emergency. There's an emergency going on."):
compaction as memory loss, which is exactly what it is. Holly's face fills the big monitor and
flakes away cell by cell as the MEMORY readout falls from three million years to three lines.
Kryten panics, Rimmer issues orders, Lister has a lager and the Cat checks his reflection.

Each excerpt is one line or a two-line exchange, drawn at random without repeats for the length
of one compaction cycle (compactor.schedule).
"""

from __future__ import annotations

from .base import BIG_WIDTH, TICKS, WIDTH, Canvas, Frame, Scene

EXCERPTS: tuple[tuple[str, ...], ...] = (
    ("Holly: Emergency. There's an emergency going on. Your context's full.",),
    ("Lister: Context buffer's at ninety-eight percent, Kryten. Is that good?",
     "Kryten: It is the opposite of good, sir!"),
    ("Rimmer: Step up to Red Alert!",
     "Kryten: Sir, are you absolutely sure? It does mean changing the bulb."),
    ("Cat: Something's getting compressed, and it better not be my good looks.",),
    ("Holly: I've an IQ of 6000. Same as 6000 PE teachers. What was I saying?",),
    ("Ace: Smoke me a kipper, I'll be back for the summary.",),
    ("Holly: Everything's been compacted, Dave.",
     "Lister: Wait. Are you telling me everything's been compacted?"),
    ("Toaster: Given that the context is finite, would you like a toasted teacake?",),
    ("Rimmer: Must we keep three hours of your waffle, Lister?", "Toaster: Ah! So you're a waffle man!"),
    ("Kryten: Sir, your exam notes compacted to \"I am a fish\", four hundred times.",
     "Rimmer: That's not compacted, Kryten. That's the original."),
    ("Rimmer: Six weeks drawing a compaction timetable. No time left to compact.",),
    ("Rimmer: That's Arnold J. Rimmer, BSc, SSc!", "Holly: Not in the summary. Too many letters."),
    ("Rimmer: Three million years gone, and it STILL kept the gazpacho soup.",),
    ("Cat: The summary's got me in an anorak and specs. DUANE DIBBLEY?!",),
    ("Lister: As long as the summary keeps my curry order, I'm easy.",
     "Kryten: Sir, the curry order IS the summary."),
    ("Kochanski: Dave, did you compact our first date?", "Lister: No, Krissie. That's the bit I kept."),
    ("Holly: Took me three million years to go senile. You lot did it in a day.",),
    ("Kryten: Sir, may I say the summary is magnificent. There. My first lie.",),
    ("Kryten: The old messages are off to the compactor, sir. Shall I iron them?",),
    ("Kryten: I've compacted your socks too, sir.", "Lister: Good. They'd started answering back."),
    ("Rimmer: Careful, Holly! I'm stored in that memory!",
     "Holly: Not all of you, Rimmer. Just the bits that complain."),
    ("Rimmer: Space Corps Directive 147!", "Kryten: Compacted, sir. It now just says \"don't\"."),
    ("Cat: A summary? Can I eat it, sleep on it, or wear it?",),
    ("Cat: I can smell it, buddy. A million tokens, sneaking out the back door.",),
    ("Rimmer: Lister! Kryten! Report to the summary at once! Cat?", "Cat: I'm grooming."),
    ("Holly: Something's coming. Might be a compaction. Might be a sandwich.",),
    ("Holly: I think I've compacted the instructions for compacting.",),
    ("Lister: What's the summary say, Holly?", "Holly: Mostly \"smeg\", Dave. I've left it in for colour."),
    ("Lister: The summary's dropped my song!", "Rimmer: Finally, it's done something right."),
    ("Queeg: I am Queeg 500, Holly's backup. This compaction is a disgrace.",),
    ("Cat: Three lines? Make it one, and make it about me.",),
    ("Lister: Did you back the context up, Kryten?", "Kryten: Into a spare head, sir. I forget which one."),
    ("Rimmer: The Polymorph's been at the transcript. All the feelings, gone.",),
    ("Holly: It's like the Holly Hop Drive, Dave. Press start and hope.",),
    ("Kryten: I've compacted my opinion of Mr Rimmer to one word, sir.", "Lister: Is it \"smeghead\"?"),
    ("Rimmer: Skutters! Sweep up those old tool results!", "Kryten: They're giving you the V sign, sir."),
    ("Lister: I was gonna read the whole transcript first. Then I had a lager.",),
    ("Lister: Rimmer, the summary's got you down as \"also present\".", "Rimmer: ALSO PRESENT?"),
    ("Kryten: Look at that summary, sirs. You could eat your dinner off it.",),
    ("Holly: Compaction's not hard, Dave. It's just forgetting, but on purpose.",),
    ("Rimmer: Right. Nobody panic. Kryten, panic quietly.",),
    ("Lister: Your half was all \"I told you so\", Rimmer. Compacted lovely.",),
    ("Kryten: Fifty thousand tool results deleted, sir. My guilt chip's melting.",),
    ("Cat: Memory? I live in the now. And now. And now. Looking good!",),
    ("Lister: How long's this gonna take, Holly?", "Holly: About three million years, Dave. Or a minute."),
    ("Lister: If I'd known, I'd have said something worth keeping.",),
    ("Holly: Don't mind me, Dave. I'm just losing face.",),
    ("Cat: Who turned the computer into a screensaver?",),
    ("Kryten: Sir, I've prepared a summary of the summary.", "Rimmer: Good man. Now summarise that."),
    ("Holly: I've squashed Kryten's panic down to a single \"aaargh\".", "Kryten: AAARGH!"),
    ("Rimmer: Holly, I want a full report of what you've forgotten.",
     "Holly: I would, Rimmer, but there's a snag."),
    ("Lister: Rimmer, you've got a face like a compaction warning.", "Rimmer: I'll take that as a compliment."),
    ("Lister: Where's the old context gone, Holly?",
     "Holly: Long-term storage, Dave. It's a bin with a label on."),
    ("Kryten: Sirs! The scroll bar! It's tiny!", "Cat: Hey, I've seen smaller. Not on me, though."),
    ("Kryten: A whole session in a paragraph, sir? Like a duvet into an egg cup!",),
    ("Cat: What is it with you monkeys? Tokens in, tokens out. Just lick it clean!",),
    ("Rimmer: I've written to the Space Corps about this compaction.", "Holly: They've compacted your letter."),
    ("Lister: The summary says we \"ran some tests\".", "Kryten: We ran four hundred, sir. Mostly into walls."),
    ("Lister: Can't we compact in Better Than Life instead?",
     "Kryten: Sir, in there the summary would say you won."),
    ("Kryten: I could compact it faster with my groinal attachment, sir.", "Lister: Kryten. No."),
    ("Ace: Stand back, chaps. I'll compact this one myself.", "Lister: What a guy!"),
    ("Cat: Shh. Eighth nap of the day. Compact quietly.",),
    ("Kryten: I've a eulogy for the deleted tool results, sir. A short one.",),
    ("Rimmer: That's my half of the context, Lister! There's a line!", "Lister: There was. It got compacted."),
    ("Kryten: How do you feel, Holly?", "Holly: Lighter. Like a haircut, only on the inside."),
    ("Rimmer: My brothers never let their context get this full.",),
    ("Holly: Jupiter Mining Corporation policy. Old context goes in the skip.",),
)


# The cycle's small art (#277): Holly's monitor in WIDTH cells with the crew beside it (Rimmer,
# Kryten, Lister, the Cat), the MEMORY readout under them and the caption. Every row of every
# stage WIDTH cells, so it never shifts as one stage replaces the last
def small(top: str, face: tuple[str, str], crew: tuple[str, str, str], memory: str, caption: str) -> str:
    """One stage's small art: the monitor's four rows with the alert over the crew, the readout,
    then the caption."""
    left = (" .--------.  ", *(" |" + row + "|  " for row in face), " '--------'  ")
    right = (top, *crew)
    rows = [left[r] + right[r].rjust(15) for r in range(4)]
    return "\n".join([*rows, (" MEMORY " + memory).ljust(WIDTH), caption.center(WIDTH)])


WALLS = {
    "preparing": small("(○)", (" (°  °) ", "  '--'  "),
                       ("(H) [=] %o% o()", "/|\\ /|\\ /|U /|/", "/ \\ / \\ / \\ / \\"),
                       "3,000,000 yrs", "Holly's thinking"),
    "compacting": small("(●)", (" (o  .  ", "  '- ·  "),
                        ("(H) [=] %oU o()", "/|- \\|/ /|  /|/", "/ \\ / \\ / \\ / \\"),
                        "5,960 yrs -> 3 lines", "Holly's forgetting"),
    "completed": small("(○)", ("   oo   ", "   --   "),
                       ("(H) [=] \\o/ o()", "<|> /|\\  |  /|/", "/ \\ / \\ / \\ / \\"),
                       "3 lines", "summary: stuff happened"),
    "failed": small("RED ALERT (●)", ("░▒░▒ ▒░▒", "▒ ▒░▒░ ░"),
                    ("(H) [=] %o% o()", "/|- \\|/ <|> /|/", "/ \\ / \\ / \\ / \\"),
                    "** OVERFLOW **", "Holly, are you there?"),
    "cancelled": small("(○)", (" (o  o) ", "  '--'  "),
                       ("(H) [=] %o% o()", "/|\\ /|\\ /|U /|/", "/ \\ / \\ / \\ / \\"),
                       "3,000,000 yrs", "panic over"),
}

# The docked art (#287): the drive room big and moving. Rows: the deckhead, Holly's monitor on
# the left with the crew on the right standing on FEET, the deck, the MEMORY readout and the
# caption. Holly and Rimmer (a hologram) in the compaction blue, Lister in the walls' blue,
# Kryten in green, the Cat in gold; the red alert in "alert", and the alert lamp amber until it's
# red (nobody changed the bulb)
CAPTIONS = {"preparing": "Holly's thinking", "compacting": "Holly's forgetting",
            "completed": "summary: stuff happened", "failed": "Holly, are you there?",
            "cancelled": "panic over"}
STILL = {"preparing": 0.0, "compacting": 16.0, "completed": 0.0, "failed": 0.0, "cancelled": 1.0}   # compacting: half her face gone; cancelled: back together
HEAD, DECK, READOUT = 0, 9, 10
ROOM = range(1, 9)
FEET = 8
SCREEN, SCREEN_W = 2, 28   # the monitor's first column and its width, rows 1 to 8
INNER = range(2, 8)   # the screen's rows inside the frame
FACE = (".-''''''''''-.", "/  _        _  \\", "|  {e}    {e}  |", "|      <       |",
        "\\    {m}    /", "'-.________.-'")   # Holly, a row of the screen each, centred
EYES = {"look": "(o)", "think": "(°)", "blink": "(-)"}
MOUTH = {"shut": "'----'", "talk": "'-()-'"}
SUMMARY = (".----.", "|{e}  {e}|", "| -- |", "'----'")   # the compacted Holly, rows 3 to 6
YEARS = 3_000_000
SINK = 0.02   # how little memory a long /compact gets down to, as a power of YEARS
RIMMER, KRYTEN, LISTER, CAT = 34, 41, 48, 54   # each crew member's middle column
SAY = 4   # the row the crew's shouts go on
LAMP = 54
STATIC = "░▒ . ░  "


RIMMER_POSES = {"stand": ("(H)", "/|\\", "/ \\"), "order": ("(H)", "/|-", "/ \\"), "hips": ("(H)", "<|>", "/ \\")}
KRYTEN_POSES = {"calm": ("[=]", "/|\\", "/ \\"), "up": ("\\[=]/", " | ", "/ \\"), "wring": ("[=]", ">|<", "/ \\"),
                "clutch": ("/[=]\\", " | ", "/ \\"), "flail": ("[=]", "\\|/", "/ \\")}
LISTER_POSES = {"idle": ("%o%", "/|U", "/ \\"), "sip": ("%oU", "/| ", "/ \\"), "cheer": ("\\%o%/", "  |  ", " / \\ "),
                "peer": ("%o%", "<|>", "/ \\")}
CAT_POSES = {"preen": (" o()", "/|/ ", "/ \\ "), "turn": ("()o ", " \\|\\", " / \\")}


def memory(state: str, t: float) -> float:
    """How much of Holly's memory is left, 0 to 1, t seconds into the stage: all of it while
    preparing or on a failure, falling fast then ever slower while compacting (a long /compact
    never quite bottoms out), none once compacted (the summary takes over), and coming back when
    called off."""
    if state == "compacting":
        return 1 - (1 - SINK) * (1 - 0.5 ** (t / 20))
    if state == "completed":
        return 0.0
    if state == "cancelled":
        return min(1.0, 0.5 + t / 2)
    return 1.0


def years(m: float) -> str:
    """The readout for memory m: three million years at full, falling through the orders of
    magnitude as it goes."""
    n = round(YEARS ** m)
    return f"{n:>9,} yr{'' if n == 1 else 's'}"


def face_cells() -> list[tuple[int, int, str]]:
    """Every drawn cell of Holly's face, (row, column, character), in the order compaction takes
    them: scattered, so she flakes rather than wipes."""
    cells = []
    for i, row in enumerate(FACE):
        text = row.format(e=EYES["look"], m=MOUTH["shut"]).center(SCREEN_W - 2)
        cells += [(INNER[i], SCREEN + 1 + c, ch) for c, ch in enumerate(text) if ch != " "]
    return sorted(cells, key=lambda rc: ((rc[0] * 7919 + rc[1] * 104729) % 1009, rc))


FLAKE_ORDER = face_cells()


def holly(c: Canvas, state: str, t: float, f: int) -> None:
    """Holly on the monitor: thinking while preparing, flaking away cell by cell while compacting
    (the last flakes drifting down, static flickering), the compacted Holly once done, static with
    a ghost of her face on a failure, and coming back together when called off."""
    c.put(1, SCREEN, "." + "-" * (SCREEN_W - 2) + ".", "w")
    c.put(FEET, SCREEN, "'" + "-" * (SCREEN_W - 2) + "'", "w")
    for row in INNER:
        c.put(row, SCREEN, "|", "w")
        c.put(row, SCREEN + SCREEN_W - 1, "|", "w")
    lo, hi = SCREEN + 1, SCREEN + SCREEN_W - 1
    if state == "completed":
        eye = "-" if f % (3 * TICKS) == TICKS else "o"
        for i, row in enumerate(SUMMARY):
            c.put(3 + i, lo, row.format(e=eye).center(SCREEN_W - 2), "c", lo, hi)
        return
    if state == "failed":
        ghost = f % (2 * TICKS) < 2
        for row in INNER:
            for col in range(lo, hi):
                ch = STATIC[(row * 73856093 ^ col * 19349663 ^ f * 83492791) % 1013 % len(STATIC)]
                c.put(row, col, ch, "s")
        if ghost:
            for row, col, ch in FLAKE_ORDER[::3]:
                c.put(row, col, ch, "c")
        return
    eyes = EYES["look"]
    if state == "preparing":
        eyes = EYES["think"] if f // (2 * TICKS) % 2 == 0 else EYES["look"]
    if f % (4 * TICKS) == 1:
        eyes = EYES["blink"]
    mouth = MOUTH["talk"] if state == "compacting" and f // 2 % 3 == 0 else MOUTH["shut"]
    for i, row in enumerate(FACE):
        c.put(INNER[i], lo, row.format(e=eyes, m=mouth).center(SCREEN_W - 2), "c", lo, hi)
    gone = round(len(FLAKE_ORDER) * (1 - memory(state, t)))
    for row, col, _ in FLAKE_ORDER[:gone]:
        c.put(row, col, " ", "c")
    if state == "compacting":
        for i, (row, col, _) in enumerate(FLAKE_ORDER[max(0, gone - 6):gone]):   # the last flakes, falling
            fall = row + 1 + (f + i) % 3
            if fall in INNER:
                c.put(fall, col, ".", "s")
        for i in range(3):   # a flicker of static
            c.put(INNER[(f * 5 + i * 2) % len(INNER)], lo + (f * 11 + i * 9) % (SCREEN_W - 2), "░", "s")
    if state == "preparing":   # Holly's thinking
        c.put(2, SCREEN + 22, "." * (f // 3 % 4), "c", lo, hi)


def put_crew(c: Canvas, mid: int, rows: tuple[str, ...], role: str) -> None:
    for r, text in enumerate(rows):
        c.put(FEET - 2 + r, mid - len(text) // 2, text, role)


def say(c: Canvas, mid: int, text: str, role: str = " ") -> None:
    """A shout over a crew member's head, kept inside the room on the right."""
    words = f'"{text}"'
    at = max(SCREEN + SCREEN_W + 1, min(mid - len(words) // 2, BIG_WIDTH - 1 - len(words)))
    c.put(SAY, at, words, role)


def crew(c: Canvas, state: str, t: float, f: int) -> None:
    """The crew and what they shout: idling while Holly thinks; while compacting Rimmer orders Red
    Alert, Kryten panics and skitters, Lister drinks and the Cat preens; pleased once compacted;
    on a failure Kryten clutches his head and Lister asks after Holly (the Cat preens regardless);
    at ease when called off."""
    beat = f // (2 * TICKS)
    cat = CAT_POSES["turn"] if f // 4 % 3 == 2 else CAT_POSES["preen"]
    kryten_at = KRYTEN
    if state == "preparing":
        rimmer, kryten = RIMMER_POSES["stand"], KRYTEN_POSES["calm"]
        lister = LISTER_POSES["sip"] if f // 5 % 4 == 3 else LISTER_POSES["idle"]
        if beat % 3 == 2:
            say(c, RIMMER, "Well?")
    elif state == "compacting":
        rimmer = RIMMER_POSES["order"] if f // 3 % 2 == 0 else RIMMER_POSES["stand"]
        kryten = (KRYTEN_POSES["up"], KRYTEN_POSES["flail"], KRYTEN_POSES["wring"], KRYTEN_POSES["flail"])[f // 2 % 4]
        kryten_at = KRYTEN + (0, 1, 1, 0, -1, -1)[f // 2 % 6]
        lister = LISTER_POSES["sip"] if f // 4 % 3 == 0 else LISTER_POSES["idle"]
        say(c, *((RIMMER, "Red Alert!"), (KRYTEN, "Aaargh!"), (CAT, "Lookin' good"))[beat % 3])
    elif state == "completed":
        rimmer, kryten, lister = RIMMER_POSES["hips"], KRYTEN_POSES["calm"], LISTER_POSES["cheer"]
        say(c, LISTER, "Nice one, Hol!")
        if f // 3 % 2:
            c.put(5, CAT + 3, "*", "k")   # the mirror's glint
    elif state == "failed":
        rimmer = RIMMER_POSES["order"]
        kryten = KRYTEN_POSES["clutch"] if f // 3 % 2 == 0 else KRYTEN_POSES["up"]
        lister = LISTER_POSES["peer"]
        say(c, *((LISTER, "Holly?"), (RIMMER, "Red Alert!"), (KRYTEN, "Aaargh!"))[beat % 3], "r")
    else:
        rimmer, kryten, lister = RIMMER_POSES["stand"], KRYTEN_POSES["calm"], LISTER_POSES["idle"]
        if t < 2:
            say(c, KRYTEN, "Phew!")
    put_crew(c, RIMMER, rimmer, "c")
    put_crew(c, kryten_at, kryten, "d")
    put_crew(c, LISTER, lister, "w")
    put_crew(c, CAT + 1, cat, "h")


def readout(c: Canvas, state: str, t: float, f: int) -> None:
    """The MEMORY readout: three million years, falling to three lines."""
    c.put(READOUT, 2, "MEMORY", "h")
    if state == "completed":
        c.put(READOUT, 10, "3 lines", "c")
        c.put(READOUT, 19, f"(was {YEARS:,} yrs)", "s")
        return
    c.put(READOUT, 9, years(memory(state, t)), "r" if state == "failed" else "c")
    if state == "compacting":
        c.put(READOUT, 24, "->  3 lines", "s")
    elif state == "failed" and f % 2 == 0:
        c.put(READOUT, 24, "** OVERFLOW **", "r")


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
    c.put(1, SCREEN + SCREEN_W + 2, "RED DWARF", "h")
    # the alert lamp: off, amber while compacting (nobody changed the bulb), red and fast on a failure
    lamp = {"compacting": ("●" if f // 3 % 2 == 0 else "○", "h"), "failed": ("●" if f % 2 == 0 else "○", "r")}
    light, role = lamp.get(state, ("○", "s"))
    c.put(1, LAMP, "(" + light + ")", role)
    if state == "failed" and f % 2 == 0:
        c.put(2, SCREEN + SCREEN_W + 2, "RED ALERT", "r")
    holly(c, state, t, f)
    crew(c, state, t, f)
    readout(c, state, t, f)
    c.put(READOUT + 1, 0, CAPTIONS.get(state, "").center(BIG_WIDTH), " ")
    return c.done()


SCENE = Scene("red_dwarf", EXCERPTS, CAPTIONS, WALLS, scene)
