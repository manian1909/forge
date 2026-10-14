"""Switch jarvis's modes by voice: "auto mode", "ask mode", "reset", "speak on", "run this in the background ...".

``parse(text)`` returns ``(slash_command, what_to_say_back)`` or None. The slash command is exactly what you could type
(/mode auto, /reset, ...), and the terminal chat runs it the same way, so everything you can do with / you can say.
"""

import re

from .commands import normalise

ENTER = r"(?:switch|go|change|turn|set|put|enter|activate|enable|use|start|be in|move)(?: it| jarvis| me)?(?: on| to| into| in)?"


def _t(text):
    t = normalise(text)
    return re.sub(r"\b(the|a|my|to|mode|me|on)\b", " ", t).strip() if False else t


# (pattern on the normalised sentence, slash command or a function(match) -> command, spoken confirmation)
RULES = [
    (r"^(?:" + ENTER + r" )?(?:auto|autonomous|automatic|full auto)(?: mode)?(?: on)?$|^(?:" + ENTER + r" )?auto mode$", "/mode auto", "Auto mode. I will run things without asking."),
    (r"^(?:" + ENTER + r" )?(?:ask|ask first|ask me|ask before running|careful|confirm)(?: mode)?$|^ask mode$", "/mode ask", "Ask mode. I will ask before I run anything."),
    (r"^(?:" + ENTER + r" )?(?:sandbox|sandboxed|safe|jail|jailed)(?: mode)?$", "/mode jail", "Sandbox mode."),
    (r"^(?:what mode|which mode|current mode|mode)(?: am i in| are you in| is it)?$", "/mode", None),
    (r"^(?:reset|start over|new chat|new conversation|forget everything|clear (?:the )?(?:chat|conversation|memory)|reset (?:the )?(?:chat|conversation))$", "/reset", "Conversation cleared."),
    (r"^(?:clear|clean)(?: the)? (?:screen|terminal|console)$", "/clear", None),
    (r"^(?:compact|summari[sz]e|shrink)(?: the)?(?: chat| conversation| context)?$", "/compact", "Compacting the conversation."),
    (r"^(?:undo|undo (?:that|it|the last one))$", "/undo", "Undone."),
    (r"^(?:retry|try again|do it again|redo)(?: that)?$", "/retry", None),
    (r"^(?:show |what is |what s )?(?:the )?(?:usage|token usage|tokens used|how many tokens)$", "/usage", None),
    (r"^(?:show |what is |what s )?(?:the )?(?:context|context size|how full is the context)$", "/context", None),
    (r"^(?:show )?(?:the )?(?:stats|speed|tokens per second)$", "/stats", None),
    (r"^(?:list|show|what are)(?: your| the| all)? tools$", "/tools", None),
    (r"^(?:list|show|what are)(?: your| the| all)? skills$", "/skills", None),
    (r"^(?:help|show help|what can you do|list commands|show commands)$", "/help", None),
    (r"^(?:" + ENTER + r" )?(?:speak|speaking|talking|voice replies|read (?:answers )?aloud|talk to me)(?: mode)? ?(?:on)?$|^(?:start|begin) (?:speaking|talking)$|^(?:speak|talk) (?:to me|out loud|aloud)$", "/speak on", None),
    (r"^(?:stop|quit|end|disable|turn off|switch off)(?: the)?(?: speaking| talking| reading| voice replies| voice| speech)$|^(?:speak|speaking|voice|talking)(?: mode)? off$|^(?:don t|do not) (?:speak|talk|read)(?: aloud)?$|^no more talking$", "/nospeak", None),
    (r"^(?:list|show|what are)(?: the| my| all)?(?: background)?(?: jobs| tasks)$|^(?:jobs|background jobs|any jobs)$", "/jobs", None),
    (r"^(?:turn |switch )?(?:standby|standby worker)(?: mode)? on$|^(?:start|enable) (?:the )?standby(?: worker)?$", "/standby on", "Standby worker on."),
    (r"^(?:turn |switch )?(?:standby|standby worker)(?: mode)? off$|^(?:stop|disable) (?:the )?standby(?: worker)?$", "/standby off", "Standby worker off."),
    (r"^(?:" + ENTER + r" )?(?:the )?(?:uncensored|open|forge open|unfiltered)(?: model)?(?: mode)?$", "/model forge-open", "Using the uncensored model."),
    (r"^(?:" + ENTER + r" )?(?:the )?(?:standard|normal|default|regular|forge)(?: model)?$|^back to (?:the )?(?:standard|normal) model$", "/model forge", "Using the standard model."),
    (r"^(?:learning|learn) on$|^(?:start|turn on) learning$", "/learn on", "Learning on."),
    (r"^(?:learning|learn) off$|^(?:stop|turn off) learning$", "/learn off", "Learning off."),
    (r"^(?:mark|that was|that s|rate) (?:that |it )?(?:as )?(?:a )?good(?: example| job)?$|^good job$", "/good", "Saved as a good example."),
    (r"^(?:mark|that was|that s|rate) (?:that |it )?(?:as )?(?:a )?(?:bad|wrong)(?: example)?$|^forget that example$", "/bad", "Forgotten."),
    (r"^(?:exit|quit|close|shut down|shutdown|goodbye|good bye|bye|bye bye|go to sleep)(?: jarvis| the terminal| the chat)?$|^(?:exit|quit) jarvis$", "/exit", "Goodbye."),
]
BG = re.compile(r"^(?:(?:run|do|handle|work on) )?(?:this |that |it )?in the background[, ]*(.+)$|^(?:in the )?background[, :]+(.+)$|^(?!what|who|where|why|how|is|are|can|does|do you)(?:run |do |handle |work on )?(\w+(?: \w+){2,}?),? in the background$")
STOP = re.compile(r"^(?:stop|cancel|abort|never ?mind|stop that|stop it|cancel that|stop the task|stop working|enough)$")


def parse(text):
    """(slash command, spoken confirmation) for a mode/control phrase, or None."""
    t = normalise(text)
    if not t:
        return None
    m = BG.match(t)
    if m:
        task = next(g for g in m.groups() if g)
        return f"/bg {task.strip()}", "Okay, running that in the background."
    for pattern, command, say in RULES:
        if re.match(pattern, t):
            return command, say
    return None


def is_stop(text):
    """True for 'stop' / 'cancel that': it interrupts a running task (or, with none running, pauses the music)."""
    return bool(STOP.match(normalise(text)))
