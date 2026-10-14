"""Understand short everyday commands without asking the language model.

"next song", "vol to 40%", "reduce the volume a bit", "play <song>" are simple, and an 8B model on this laptop needs
seconds (and sometimes guesses a PowerShell command that does not exist). So these are matched here, run at once by
``run()``, and anything that is not one of them (``parse`` returns None) goes to the model as before.

The same code serves typed requests and spoken ones. For speech the text has already been through the recogniser, so
the matching is tolerant: "vol" for "volume", number words, filler words, "please".
"""

import re

from . import media

UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
         "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
         "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
        "ninety": 90}
LEVELS = {"max": 100, "maximum": 100, "full": 100, "half": 50, "min": 10, "minimum": 10, "low": 20, "medium": 50}
STEP, SMALL, BIG = 10, 5, 25          # default change, "a little", "a lot"

NUM = r"(\d{1,3}|[a-z]+?(?: [a-z]+?)??)"
ADDRESS = re.compile(r"^\s*(?:(?:hey|ok|okay|hi|hello)\s+)?jarvis\b|\bjarvis\s*$", re.I)   # the name, only when it addresses us
FILLER = re.compile(r"\b(please|hey|ok|okay|can you|could you|would you|will you|i want you to|i want to|"
                    r"i would like to|just|now|for me|kindly|hello)\b")
SERVICES = {"youtube music": "youtube_music", "utube music": "youtube_music", "u tube music": "youtube_music", "yt music": "youtube_music", "you tube music": "youtube_music",
            "youtube": "youtube", "you tube": "youtube", "utube": "youtube", "u tube": "youtube"}


def to_number(text):
    """'60' -> 60, 'sixty' -> 60, 'sixty five' -> 65, 'a hundred' -> 100; None if it is not a number."""
    t = text.strip().lower().replace("-", " ")
    if t.isdigit():
        return int(t)
    t = re.sub(r"^(a|one) hundred$", "hundred", t)
    if t == "hundred":
        return 100
    if t in LEVELS:
        return LEVELS[t]
    parts = t.split()
    if len(parts) == 1:
        return UNITS.get(parts[0]) if parts[0] in UNITS else TENS.get(parts[0])
    if len(parts) == 2 and parts[0] in TENS and parts[1] in UNITS and UNITS[parts[1]] < 10:
        return TENS[parts[0]] + UNITS[parts[1]]
    return None


def normalise(text):
    """Lower-case, no punctuation, no filler words, '60%' -> '60 percent', 'vol' -> 'volume'."""
    t = text.lower().strip()
    t = re.sub(r"(\d)\s*%", r"\1 percent", t)
    t = t.replace("per cent", "percent")
    t = re.sub(r"[^\w\s]", " ", t)
    t = FILLER.sub(" ", ADDRESS.sub(" ", t))
    t = re.sub(r"\b(vol|volum|volumne|wallum|valium|vall?um)\b", "volume", t)
    if not re.match(r"(play|put on|listen)\b", t):
        t = re.sub(r"\b(sound|audio)\b", "volume", t)       # "play sound of rain" is a song, not a volume request
    t = re.sub(r"\b(sung|songs|soon|son)\b(?= *$)", "song", t) if re.search(r"\b(next|previous|skip|last)\b", t) else t
    return re.sub(r"\s+", " ", t).strip()


SONG = r"(?:song|track|one|music|tune)"
PATTERNS = [
    ("unmute", rf"^(?:un ?mute|turn (?:the )?(?:volume |sound )?on)(?: volume)?$"),
    ("mute", rf"^(?:mute|silence)(?: the)?(?: volume| music| sound)?$"),
    ("next", rf"^(?:play |go to |skip to |switch to |change to |put on )?(?:the )?next(?: {SONG})?(?: please)?$"),
    ("next", rf"^(?:skip|change)(?: this| the| that)?(?: {SONG})?$"),
    ("prev", rf"^(?:play |go to |go back to |switch to |put on )?(?:the )?(?:previous|prev|last|earlier|old)(?: {SONG})?$"),
    ("prev", r"^go back$"),
    ("restart", rf"^(?:play |start )?(?:this |the |that |it )?(?:{SONG} )?(?:again|from the start|from the beginning|from start)$"),
    ("pause", rf"^(?:pause|stop|hold|hold on)(?: the| this| that)?(?: {SONG}| playing| playback| it)?$"),
    ("resume", rf"^(?:resume|continue|unpause|play|start|go on|carry on)(?: the| this| that)?(?: {SONG}| playing| playback| it)?$"),
]


def _vol_delta(rest):
    if re.search(r"\b(a lot|much|way|heavily)\b", rest):
        return BIG
    if re.search(r"\b(a bit|little|slightly|tad|touch|notch|abit)\b", rest):
        return SMALL
    m = re.search(rf"\bby {NUM}(?: percent)?\b", rest)
    n = to_number(m.group(1)) if m else None
    return n if n else STEP


def parse(text):
    """Return a command dict, or None when this is not a simple command (hand it to the model).

    {"action": "next"} · {"action": "volume_set", "value": 60} · {"action": "volume_up", "value": 10}
    {"action": "play", "query": "...", "service": "youtube_music"} · {"action": "open_app", "name": "outlook"}
    """
    t = normalise(text)
    if not t:
        return None
    for action, pattern in PATTERNS:
        if re.match(pattern, t):
            return {"action": action}

    is_play = bool(re.match(r"(play|put on|start playing|listen to)\s+\S", t))
    if not is_play and re.search(r"\bvolume\b|\b(louder|quieter|softer)\b", t) or re.match(r"^(?:turn|bring|take|pump|crank) (?:it |that )?(?:up|down)\b", t):
        # an exact level: "volume 60", "set volume to sixty percent", "volume to max", "60 percent volume"
        m = (re.search(rf"\bvolume (?:to |at |on |level )?(?:to )?{NUM}(?: percent)?$", t)
             or re.search(rf"\b(?:set|make|put|change)(?: the)? volume {NUM}(?: percent)?$", t)
             or re.search(rf"^{NUM} percent volume$", t))
        if m:
            n = to_number(m.group(1))
            if n is not None and 0 <= n <= 100:
                return {"action": "volume_set", "value": n}
        down = re.search(r"\b(down|lower|lesser|reduce|decrease|drop|less|quieter|softer|quiet|minus|low)\b", t)
        up = re.search(r"\b(up|raise|increase|higher|louder|more|boost|plus|pump|crank|bump)\b", t)
        if up and not down:
            return {"action": "volume_up", "value": _vol_delta(t)}
        if down and not up:
            return {"action": "volume_down", "value": _vol_delta(t)}
        return None

    m = re.match(r"^(?:play|put on|start playing|listen to)\s+(.+)$", t)
    if m:
        query, service = m.group(1), "youtube_music"
        for name, svc in sorted(SERVICES.items(), key=lambda kv: -len(kv[0])):
            tail = re.search(rf"\s+(?:on|in|using|from|via)\s+(?:the\s+)?{name}$", query)
            if tail:
                query, service = query[:tail.start()], svc
                break
        query = re.sub(r"^(?:the |me |a |some )?(?:song|track|music|tune)s? ", "", query).strip()
        query = re.sub(r"\s+(?:song|music)$", "", query).strip() if len(query.split()) > 2 else query
        if query:
            return {"action": "play", "query": query, "service": service}

    m = re.match(r"^(?:open|launch|start|show|go to|browse|take me to)\s+(.+)$", t)
    if m:
        rest = m.group(1)
        spec = _folder_spec(rest)
        if spec:
            return {"action": "open_folder", "path": spec}
        if re.fullmatch(r"(?:the )?(?:files? |windows )?explorer|this pc|my computer", rest):
            return {"action": "open_app", "name": "file explorer"}
        m = re.match(r"^(?:open|launch|start|run)\s+(?:the\s+)?([a-z0-9 ]{2,30})$", t)
        if m and not re.search(r"\b(file|folder|script|program|code|website|site|page|link|document)\b", m.group(1)):
            return {"action": "open_app", "name": m.group(1).strip()}
    return None


FOLDER_NAMES = r"documents?|docs|downloads?|desktop|pictures?|photos|images|music|songs|videos?|movies|recycle bin|home|jarvis"
DRIVE = r"(?:[a-z] drive|drive [a-z]|[a-z]:)"


def _folder_spec(rest):
    """The folder named in 'open ...' (documents, d drive, 'reports folder', 'explorer ... then the downloads folder')."""
    last = re.split(r"\b(?:in that|in it|and then|and|then)\s+(?:open|show|go to)\s+|\bin\s+(?=(?:the |my )?(?:" + FOLDER_NAMES + r"))", rest)[-1]
    last = re.sub(r"^(?:the |my |a )+", "", last.strip())
    if re.fullmatch(rf"(?:{FOLDER_NAMES}|{DRIVE})(?: folder| directory)?", last):
        return re.sub(r" (?:folder|directory)$", "", last)
    m = re.fullmatch(r"([a-z0-9 _.-]{2,40}) (?:folder|directory)", last)        # "project reports folder"
    if m and not re.search(r"\b(file|explorer)\b", m.group(1)):
        return m.group(1).strip()
    m = re.fullmatch(r"(?:folder|directory) (?:called |named )?([a-z0-9 _.-]{2,40})", last)
    return m.group(1).strip() if m else None


def run(cmd, toolbox=None):
    """Do a parsed command; returns a short sentence to show or say."""
    action = cmd["action"]
    if action == "play":
        from . import desktop
        return desktop.play_music(cmd["query"], cmd.get("service", "youtube_music"))
    if action == "open_app":
        from . import desktop
        return desktop.open_app(cmd["name"])
    if action == "open_folder":
        from . import desktop
        return desktop.open_folder(cmd["path"])
    return media.do(action, cmd.get("value"))
