"""The tool-use loop: ask the model, run any tools it calls, feed results back."""

import json

from .prompts import SYSTEM_PROMPT
from .tools import NEEDS_CONFIRMATION, TOOL_SCHEMAS


class Agent:
    def __init__(self, llm, toolbox, confirm=None, max_steps=8, max_history=40):
        """``confirm(name, args) -> bool`` is asked before side-effecting tools.

        With ``confirm=None`` every tool runs without asking.
        """
        self.llm = llm
        self.toolbox = toolbox
        self.confirm = confirm
        self.max_steps = max_steps
        self.max_history = max_history
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

    def run(self, user_text, on_token=None, on_tool=None, on_result=None):
        """Handle one user message. Returns the final assistant text and stats."""
        self.messages.append({"role": "user", "content": user_text})
        self._trim_history()
        all_stats = []
        for _ in range(self.max_steps):
            message, stats = self.llm.chat(self.messages, tools=TOOL_SCHEMAS, on_token=on_token)
            all_stats.append(stats)
            self.messages.append(message)
            calls = message.get("tool_calls")
            if not calls:
                return message["content"], all_stats
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
