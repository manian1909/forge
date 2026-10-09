"""The tool-use loop: ask the model, run any tools it calls, feed results back."""

import json
import re

from .prompts import SYSTEM_PROMPT
from .tools import NEEDS_CONFIRMATION, TOOL_SCHEMAS


# "Let me run it:" with no tool call behind it means the model stopped one step early
ANNOUNCE_RE = re.compile(r"\b(let me|let's|i'll|i will|i'm going to|i am going to|now i'll|next,? i'll)\b(?! know)", re.I)
NUDGE_EMPTY = "(system note) Your reply was empty. Answer the user's request now."
NUDGE_ACT = "(system note) You described an action but did not perform it. Call the tool now, then report the real result."


class Agent:
    def __init__(self, llm, toolbox, confirm=None, max_steps=8, max_history=40, nudge=True):
        """``confirm(name, args) -> bool`` is asked before side-effecting tools.

        With ``confirm=None`` every tool runs without asking.
        """
        self.llm = llm
        self.toolbox = toolbox
        self.confirm = confirm
        self.max_steps = max_steps
        self.max_history = max_history
        self.nudge = nudge  # re-prompt when the model goes quiet or only announces an action
        self.reset()

    def reset(self):
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    def _trim_history(self):
        # keep the system prompt and the most recent turns; a shorter prompt is
        # faster to process on a small GPU
        if len(self.messages) > self.max_history:
            tail = self.messages[-(self.max_history - 1):]
            while tail and tail[0]["role"] == "tool":
                tail = tail[1:]
            self.messages = [self.messages[0]] + tail

    def _nudge_for(self, text, used_tools, nudges):
        if not self.nudge or nudges >= 2:
            return None
        if not text.strip():
            return NUDGE_EMPTY
        tail = text.strip()[-250:]
        if used_tools and (tail.endswith(":") or (ANNOUNCE_RE.search(tail) and not tail.endswith("?"))):
            return NUDGE_ACT
        return None

    def run(self, user_text, on_token=None, on_tool=None, on_result=None):
        """Handle one user message. Returns the final assistant text and stats."""
        self.messages.append({"role": "user", "content": user_text})
        self._trim_history()
        all_stats = []
        used_tools, nudges = False, 0
        for _ in range(self.max_steps):
            message, stats = self.llm.chat(self.messages, tools=TOOL_SCHEMAS, on_token=on_token)
            all_stats.append(stats)
            self.messages.append(message)
            calls = message.get("tool_calls")
            if not calls:
                note = self._nudge_for(message["content"], used_tools, nudges)
                if note is None:
                    return message["content"], all_stats
                nudges += 1
                self.messages.append({"role": "user", "content": note})
                continue
            used_tools = True
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                except json.JSONDecodeError as e:
                    args, result = None, f"Error: arguments were not valid JSON ({e})"
                if args is not None:
                    if on_tool:
                        on_tool(name, args)
                    if self.confirm and name in NEEDS_CONFIRMATION and not self.confirm(name, args):
                        result = "The user declined to run this tool call."
                    else:
                        result = self.toolbox.call(name, args)
                if on_result:
                    on_result(name, result)
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        return "(stopped: too many tool steps in a row)", all_stats
