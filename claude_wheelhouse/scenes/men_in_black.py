"""Men in Black, the neuralyzer (#304, scene C, "Look into the little red light"): a memory wipe
with a summary attached. On a New York street at night, K and J put their sunglasses on, K raises
the neuralyzer, its little red light charges and the frame floods white; the bystanders come out
of it blank and smiling while the WIPED readout climbs, an alien ducks behind a bin, and once
compacted the readout stamps the cover story.

Each excerpt is one line or a two-line exchange, drawn at random without repeats for the length
of one compaction cycle (compactor.schedule).
"""

from __future__ import annotations

from .base import BIG_HEIGHT, BIG_WIDTH, TICKS, WIDTH, Canvas, Frame, Scene

EXCERPTS: tuple[tuple[str, ...], ...] = (
    ("K: Look into the little red light.", "J: Which red light?"),
    ("J: You can't just flash a whole conversation!", "K: Watch me."),
    ("J: Do I get to keep any of it?", "K: The summary. And the suit."),
    ("Zed: Somebody tell me why there are 400,000 tokens in my waiting room.",),
    ("Frank: Hey! I was using that context!",),
    ("J: What did we just talk about?", "K: Exactly."),
    ("K: A token is smart. A context window is dumb, panicky and dangerous.",),
    ("K: An hour ago everybody knew why we opened this file. Imagine tomorrow.",),
    ("K: See the little red button, kid? Don't ever touch it.", "J: Too late. It said \"/compact\"."),
    ("J: That's the summary? It's the size of a Noisy Cricket.", "K: Kicks like one, too."),
    ("Beatrice: The context was hanging off it. Like an Edgar suit.",),
    ("Jeebs: You compacted my head off! You know how much that stings?",
     "K: It'll grow back. They always do."),
    ("J: Where are the Worm Guys while it compacts?", "K: On their fourth coffee. Each."),
    ("Frank: Don't like the summary? You can kiss my furry little butt.",),
    ("Jensen: The best of the best of the best summaries, sir! With honours.",
     "J: Yeah, he's just real excited."),
    ("J: A whole galaxy on Orion's belt? That little marble?", "K: Your session's going somewhere smaller."),
    ("K: Read the summary, kid. Best investigative reporting on the planet.",),
    ("J: Why not tell people the context window's about to burst?", "K: They're happier not knowing, kid."),
    ("K: Summarised the Bug. \"Big, ugly, ate a farmer.\"",),
    ("Laurel: Before you compact it, can I do an autopsy?",),
    ("O: K used to smile, you know. Before his first compaction.",),
    ("Griffin: In one future the summary's perfect. In the rest, no ticket number.",),
    ("J: The summary calls him Boris the Animal.", "Boris: It's just Boris!"),
    ("J: Last agent who compacted that much ended up running a post office.",),
    ("Jeebs: I built a de-compactor. Only one head's exploded so far.",),
    ("Locker alien: All hail the summary! It is the only text we have ever known.",),
    ("Zed: You're a rumour, J. Recognisable only as déjà vu and a line of summary.",),
    ("J: Kept little Tiffany. Physics books at eight? She's up to something.",),
    ("J: (stomping) That's one less token. And another.", "Edgar: Those were my relatives!"),
    ("J: Hold up, hold up, hold up.", "K: Six tokens. Compacted."),
    ("J: The Bug swallowed K whole!", "K: (from inside) Compacting. Back in a minute."),
    ("Zed: Flash the conference room. They've seen the token bill.",),
    ("J: (singing) Here come the Men in Black. They won't let you remember.",),
    ("J: Two hundred thousand tokens? Old and busted. Three lines? New hotness.",),
    ("J: That was swamp gas off a weather balloon. Enjoy the rest of your session.",),
    ("J: Why's the neuralyzer set to three hours? We talked for two.", "K: Rounding up."),
    ("M: Dodged the neuralyzer when I was eight. I'm dodging this compaction too.",),
    ("H: Relax. I've compacted bigger sessions than this with a hangover.",),
    ("J: How do I get back to before the compaction?",
     "Jeffrey: Jump off the Chrysler Building. Time it right."),
    ("J: What did the summary leave out?", "K: Don't ask questions you don't want answers to."),
    ("J: Pass me the flashy thing.", "K: It's a neuralyzer, kid. And no."),
    ("K: (deneuralyzed) Ah. Now I remember why I compacted it.",),
    ("J: Congratulations, ma'am! It's a... summary.",),
    ("Frank: Compaction? Pal, I'm a pug. My whole face is compacted.",),
    ("J: James Darrell Edwards the Third, sir.", "Zed: J. We compact names round here."),
    ("J: Why do I have the sudden urge for chocolate milk?",
     "O: Temporal fracture. Something got compacted that shouldn't have."),
    ("Warhol: I'm painting soup cans, J. Please, just compact me.",),
    ("J: Had to flash T. He cried at the summary.",),
    ("Scrad: The summary's only got room for one head.", "Charlie: The good-looking one!"),
    ("Zed: Every so often I miss the old context. Then I read it again.",),
    ("Zed: The rookie just compacted the briefing he was about to read.",),
    ("J: The summary says I saved the day.", "K: Summaries are generous, kid. Sign it."),
    ("J: K, your half of the summary just says \"no\".",),
    ("K: When I'm done, flash me, kid. I'd like to go home with a short summary.",),
    ("J: Who decides what goes in the summary?", "K: Whoever's holding the neuralyzer."),
    ("K: Raw context's so primitive it counts as a disease in the better galaxies.",),
    ("K: Whole city saw the context overflow. Light up the Statue of Liberty.",
     "J: She's a neuralyzer?"),
    ("J: How long till the context's full?", "Zed: One galactic standard week. About an hour."),
    ("J: Tiny guy in a big head, working all the controls?", "K: That's the summary from now on."),
    ("J: Sunglasses? Indoors? At night?", "K: You'll thank me in a second."),
    ("J: Are all these tokens bad?", "K: Most are decent, just trying to make a living. Not the stack traces."),
)


# The cycle's small art (#277): J and K in their shades on the left, the alien's bin and the
# bystanders on the right, the readout under them and the caption. Every row of every stage WIDTH
# cells, so it never shifts as one stage replaces the last
def small(rows: tuple[str, str, str], readout: str, caption: str) -> str:
    """One stage's small art: three rows of street, the readout, then the caption."""
    return "\n".join([*(r.ljust(WIDTH) for r in rows), (" " + readout).ljust(WIDTH), caption.center(WIDTH)])


WALLS = {
    "preparing": small((" ■-■  ■-■    \\  /  ?   ?",
                        " /█\\  /█\\    (oo) o_o o_o",
                        " / \\  / \\    [__] -|\\ /|\\"),
                       "NEURALYZER  set: 3h 12m", "sunglasses on"),
    "compacting": small((" ■-■  ■-■ ░▒▓███▓▒░  ",
                         " /█\\  /█=[●]██████▓▒ o_o",
                         " / \\  / \\ ░▒▓███▓▒░  /|\\"),
                        "WIPED [█████·····] 1h 38m", "look here, please"),
    "completed": small((" ■-■  ■-■    (o-)  ^_^ ^_^",
                        " /█\\  /█\\    [__]  /|\\ /|\\",
                        " / \\  / \\          / \\ / \\"),
                       "COVER: nothing happened", "nothing happened here"),
    "failed": small((" ■-■  ■-■ *  (oo)/ O_O O_O",
                     " /█\\  /█=[x]  [__] \\|/ \\|/",
                     " / \\  / \\ '        / \\ / \\"),
                    "WIPED 0h 00m  ALIENS!", "it didn't take"),
    "cancelled": small((" ■-■  ■-■    \\  /  o_o o_o",
                        " /█\\  /█\\    [__]  /|\\ /|\\",
                        " / \\  / \\          / \\ / \\"),
                       "NEURALYZER  safety on", "put it away, K"),
}

# The docked art (#287): the street big and moving. Rows: the skyline, the street with J and K
# (in shades, in the compaction blue) on the left, a lamp post, the alien's bin and three
# bystanders (amber) on the right standing on FEET, the pavement, the readout and the caption.
# The neuralyzer's little light in "alert", its flash in "flash", the alien in green
CAPTIONS = {"preparing": "sunglasses on", "compacting": "look here, please",
            "completed": "nothing happened here", "failed": "it didn't take",
            "cancelled": "put it away, K"}
SKY, PAVEMENT, READOUT = 0, 9, 10
FEET = 8
SAY = 4   # the row the shouts go on
FRANK, J, K = 1, 9, 15   # Frank the Pug's first column, J's and K's middle columns
LIGHT = (7, K + 3)   # the neuralyzer's little red light, held out at K's chest
LAMP = 25   # the lamp post's column
BIN = 30   # the alien's bin's first column
FOLK = (40, 47, 54)   # the bystanders' middle columns
SKYLINE = "▂▂▅▅▅▃▃▇▇▂▂▂▅▅▆▃▃▃▆▆▂▂▄▄▄▇▇▇▂▂▃▃▅▅▂▂▂▆▆▃▃▃▇▇▂▂▄▄▄▅▅▂▂▂▃▃▆▆▂▂"
WINDOWS = (3, 8, 14, 18, 25, 26, 33, 38, 42, 47, 53, 56)   # lit windows in the skyline
LEAD, EVERY = 2 * TICKS, 6 * TICKS   # frames to the first flash while compacting, and between flashes
FLASH = 8   # frames a flash lasts: a burst, a white flood, fading
MINUTES = 192   # how far back the neuralyzer is set: 3h 12m
STILL = {"preparing": 4.0, "compacting": (LEAD + EVERY + 1) / TICKS, "completed": 0.0, "failed": 0.0,
         "cancelled": 2.0}   # compacting: the second flash bursting from the light


def flashes(state: str, f: int) -> int:
    """How many flashes have gone off f frames into the stage: one every EVERY frames while
    compacting, after a LEAD to charge."""
    return 0 if state != "compacting" or f < LEAD else (f - LEAD) // EVERY + 1


def wiped(state: str, f: int) -> int:
    """Minutes of the session wiped: a share of what's left at every flash (a long /compact never
    quite gets it all), none on a failure or when called off, all of it once compacted."""
    if state == "completed":
        return MINUTES
    return round(MINUTES * (1 - 0.7 ** flashes(state, f)))


def hm(minutes: int) -> str:
    return f"{minutes // 60}h {minutes % 60:02d}m"


def flash(c: Canvas, k: int) -> None:
    """The neuralyzer's flash, k frames in: a burst from the little red light, one frame of white
    over the whole street (no long strobe in a pinned dock), then fading out in sparkles."""
    row0, col0 = LIGHT
    for row in range(SKY, PAVEMENT):
        for col in range(BIG_WIDTH):
            d = (((col - col0) / 2) ** 2 + (row - row0) ** 2) ** 0.5
            spot = (row * 7919 + col * 104729 + k * 31) % 11
            if k == 0:
                ch = "█" if d < 1.5 else "▓" if d < 2.5 else "▒" if d < 3.5 else ""
            elif k == 1:
                ch = "█" if d < 5 else "▓" if d < 8 else "▒" if d < 11 else "░" if d < 14 else ""
            elif k == 2:
                ch = "█"
            elif k == 3:
                ch = "▓"
            elif k == 4:
                ch = "▒"
            elif k in (5, 6):
                ch = "░" if spot < 6 else ""
            else:
                ch = "·" if spot == 0 else ""
            if ch:
                c.put(row, col, ch, "f")


def street(c: Canvas, state: str, f: int) -> None:
    """The skyline with its lit windows, the label, the lamp post and the pavement."""
    c.put(SKY, 0, SKYLINE, "s")
    for col in WINDOWS:
        if (col * 13 + f // (3 * TICKS)) % 5:   # a window or two goes dark now and then
            c.put(SKY, col, "▪", "h")
    c.put(1, 2, "MEN IN BLACK", "w")
    c.put(2, LAMP - 2, "o═╗", "s")
    c.put(2, LAMP - 2, "o", "h")
    for row in range(3, PAVEMENT):
        c.put(row, LAMP, "║", "s")
    c.put(PAVEMENT, 0, "▀" * BIG_WIDTH, "w")


def alien(c: Canvas, state: str, f: int) -> None:
    """The alien behind the bin that started it all: peeking while the agents get ready, ducked
    down to its antennae through the flashes, winking once it's over, and up on show waving when
    the wipe didn't take."""
    c.put(FEET, BIN, "[__]", "s")
    if state == "failed":
        c.put(6, BIN, "\\  /", "d")
        c.put(7, BIN, "(oo)" + ("/" if f // 3 % 2 else "_"), "d")
        return
    up = (state == "preparing" and f // TICKS % 3 != 2) or state == "completed"
    eyes = "(o-)" if state == "completed" and f % (3 * TICKS) < TICKS else "(oo)"
    c.put(6 if up else 7, BIN, "\\  /", "d")
    if up:
        c.put(7, BIN, eyes, "d")


def folk(c: Canvas, state: str, f: int, blank: bool, hidden: bool) -> None:
    """The three bystanders: staring at the alien while the agents get ready, at the little red
    light while it charges, blank and smiling after a flash, hands up when it didn't take."""
    if hidden:
        return
    for i, mid in enumerate(FOLK):
        if state == "failed":
            rows = ("O_O", "\\|/", "/ \\")
        elif blank:
            rows = ("^_^", "/|\\", "/ \\")
        elif state == "preparing" and i == 0:
            rows = ("o_o", "-|\\", "/ \\")   # pointing at the alien
        else:
            rows = ("o_o", "/|\\", "/ \\")
        for r, text in enumerate(rows):
            c.put(FEET - 2 + r, mid - 1, text, "h")
        if state == "preparing" and (f // 4 + i) % 3 == 0:
            c.put(FEET - 3, mid, "?", "h")
        elif blank and (f // 3 + i) % 4 == 0:
            c.put(FEET - 3, mid, "~", "h")   # dazed


def agents(c: Canvas, state: str, f: int, aim: bool, shades: tuple[bool, bool], lit: bool = True) -> None:
    """Frank the Pug at J's feet, J, and K with the neuralyzer held out when he's using it, its
    little red light lit (lit) or blinking while it charges."""
    c.put(FEET, FRANK, "U'.'U", "h")
    for mid, on in ((J, shades[0]), (K, shades[1])):
        c.put(FEET - 2, mid - 1, "■-■" if on else " o ", "c")
        c.put(FEET, mid - 1, "/ \\", "c")
    c.put(FEET - 1, J - 1, "/█\\", "c")
    if state == "completed" and f // TICKS % 4 == 1:
        c.put(FEET - 1, J - 1, "/█/", "c")   # J straightens his tie
    if not aim:
        c.put(FEET - 1, K - 1, "/█\\", "c")
        return
    c.put(FEET - 1, K - 1, "/█=[", "c")
    c.put(FEET - 1, K + 4, "]", "c")
    row, col = LIGHT
    if state == "failed":
        c.put(row, col, "x" if f % 3 else "○", "s")
        for i, (dr, dc, ch) in enumerate(((-1, 2, "*"), (1, 2, "'"), (-1, -1, "."), (0, 3, "*"))):
            if (f + i) % 3 == 0:
                c.put(row + dr, col + dc, ch, "k")
        return
    c.put(row, col, "○" if lit is False else "●", "r")


def say(c: Canvas, mid: int, text: str, role: str = " ") -> None:
    """A shout over someone's head, kept inside the frame."""
    words = f'"{text}"'
    c.put(SAY, max(1, min(mid - len(words) // 2, BIG_WIDTH - 1 - len(words))), words, role)


def readout(c: Canvas, state: str, f: int) -> None:
    """The neuralyzer's readout: its setting, how much is wiped, and the cover story."""
    if state == "preparing":
        c.put(READOUT, 2, "NEURALYZER", "h")
        c.put(READOUT, 14, "set: " + hm(min(MINUTES, f * 8)), "c")
        if f // 3 % 2:
            c.put(READOUT, 27, "▮", "c")
    elif state == "completed":
        c.put(READOUT, 2, "COVER STORY", "h")
        c.put(READOUT, 15, '"You compacted. Nothing happened."', "c")
    elif state == "cancelled":
        c.put(READOUT, 2, "NEURALYZER", "h")
        c.put(READOUT, 14, f"safety on · {hm(MINUTES)} kept", "c")
    else:
        w = wiped(state, f)
        full = round(10 * w / MINUTES)
        c.put(READOUT, 2, "WIPED", "h")
        c.put(READOUT, 8, "[" + "█" * full + "·" * (10 - full) + "]", "r" if state == "failed" else "c")
        c.put(READOUT, 21, f"{hm(w)} of {hm(MINUTES)}", "c")
        if state == "failed" and f % 2 == 0:
            c.put(READOUT, 38, "** IT DIDN'T TAKE **", "r")


def scene(state: str, t: float, animate: bool = True) -> Frame:
    """One frame of the big art for a stage, t seconds into it (rows, roles): BIG_HEIGHT rows of
    BIG_WIDTH cells. animate False: the stage's still (STILL), as with the app's animation off."""
    t = t if animate else STILL.get(state, 0.0)
    f = int(t * TICKS)
    c = Canvas()
    beat = f // (2 * TICKS)
    street(c, state, f)
    alien(c, state, f)
    if state == "compacting":
        k = (f - LEAD) % EVERY if f >= LEAD else EVERY
        charging = k >= EVERY - TICKS or f < LEAD
        folk(c, state, f, blank=not charging and k >= FLASH, hidden=f >= LEAD and 2 <= k < FLASH - 2)
        if f >= LEAD and k < FLASH:
            flash(c, k)
        agents(c, state, f, aim=True, shades=(True, True), lit=not charging or f % 2 == 0)
        if charging:
            say(c, K, "Look here, please.")
        elif k >= FLASH:
            say(c, J, "Nothing to see here!")
    elif state == "preparing":
        folk(c, state, f, blank=False, hidden=False)
        agents(c, state, f, aim=False, shades=(f >= 2 * TICKS, f >= TICKS))
        say(c, *((K, "Sunglasses, kid."), (J, "Got 'em."), (FOLK[1], "What IS that?"))[beat % 3])
    elif state == "completed":
        folk(c, state, f, blank=True, hidden=False)
        agents(c, state, f, aim=False, shades=(True, True))
        say(c, J, "Have a nice day!")
    elif state == "failed":
        folk(c, state, f, blank=False, hidden=False)
        agents(c, state, f, aim=True, shades=(True, True))
        say(c, *((FOLK[1], "ALIENS!"), (J, "Uh... K?"), (FOLK[1], "ALIENS!"))[beat % 3], "r")
    else:
        folk(c, state, f, blank=False, hidden=False)
        agents(c, state, f, aim=t < 1, shades=(True, True))
        if t < 3:
            say(c, FOLK[1], "Was that a camera?")
    readout(c, state, f)
    c.put(BIG_HEIGHT - 1, 0, CAPTIONS.get(state, "").center(BIG_WIDTH), " ")
    return c.done()


SCENE = Scene("men_in_black", EXCERPTS, CAPTIONS, WALLS, scene)
