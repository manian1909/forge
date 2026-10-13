"""`jarvis`: a terminal UI for your local model, in agent mode.

    jarvis                         interactive session in the current folder
    jarvis "fix the bug in app.py" interactive session that starts with this task
    jarvis -p "summarise notes.txt" run one task, print the answer, exit  (add --yes to allow tools to run)
    echo "list my python files" | jarvis

The model decides which tools to use for the task (run code, run PowerShell, read/write files, search the web).
By default it asks before anything that changes the machine (run_python, run_shell, write_file); reading and
searching are automatic. /auto switches to fully autonomous.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from . import experience, jail, serve, skills, speech, tasks
from .agent import VERIFIED_FOOTER, Agent
from .governor import GovernedLLM, Governor, free_mb
from .prompts import ASSISTANT_NAME
from .router import RoutedLLM, make_llm
from .session import Usage, breakdown, compact_messages, context_window, estimate_tokens, forge_home
from .speech import Speaker, normalise_rate
from .tools import TOOL_NAMES, Toolbox, keys_path, load_keys, needs_confirmation

VERSION = "0.2"
REPO = Path(__file__).resolve().parent.parent
HELP = """[bold]Commands[/bold]
  [cyan]/help[/cyan]            this list
  [cyan]/auto[/cyan]            toggle fully autonomous mode (no permission prompts)
  [cyan]/mode ask|auto[/cyan]   choose the permission mode
  [cyan]/default ask|auto[/cyan] the mode jarvis opens in from now on (saved in ~/.forge/config.json)
  [cyan]/reset[/cyan]           forget the conversation   [cyan]/clear[/cyan] clear the screen
  [cyan]/context[/cyan]         how full the model's context window is, and what fills it
  [cyan]/compact[/cyan] [focus]  summarise the conversation to free context (happens by itself near the limit)
  [cyan]/usage[/cyan]           tokens used this session, today and in total
  [cyan]/undo[/cyan]            forget the last exchange   [cyan]/retry[/cyan] ask the last request again
  [cyan]/skills[/cyan]          how-to notes (PDF, Excel, Word, slides, charts, APIs) added when a request needs them; [cyan]/skill <name>[/cyan] forces one
  [cyan]/good[/cyan] [cyan]/bad[/cyan]       keep or forget the last task as an example to copy next time; [cyan]/learn on|off[/cyan]
  [cyan]/ws <folder>[/cyan]     change the working folder the tools use
  [cyan]/model <name>[/cyan]    switch the Ollama model for this session
  [cyan]/remote[/cyan] [cyan]/local[/cyan] [cyan]/backend[/cyan]   force remote APIs, release, show router status (needs providers.json)
  [cyan]/speak[/cyan] [on|off|test]   read answers aloud (one way: it talks, it does not listen)
  [cyan]/nospeak[/cyan]         turn speak mode off   [cyan]/mute[/cyan] stop talking right now (mode stays on)
  [cyan]/voice[/cyan]           list voices; [cyan]/voice <number|name>[/cyan] choose; [cyan]/voice engine edge|windows[/cyan]; [cyan]/voice rate faster|slower|+10%[/cyan]
  [cyan]/stats[/cyan]           toggle the speed line after each answer
  [cyan]/tools[/cyan]           list what the model can do
  [cyan]/key[/cyan] [NAME value]  list or save API keys for http_request (the model only sees {{NAME}}); [cyan]/key -NAME[/cyan] deletes
  [cyan]/bg <task>[/cyan]       hand a long task to the standby worker: it pauses while your games/apps need the memory
                   and resumes from its last step, even after a restart. [cyan]/jobs[/cyan] [n] list or show, [cyan]/cancel n[/cyan]
  [cyan]/standby[/cyan]         memory sharing status; [cyan]/standby on|off[/cyan] start the worker at login;
                   [cyan]/standby heavy <app.exe>[/cyan] always pause while that app runs ([cyan]-app.exe[/cyan] removes)
  [cyan]/save <file>[/cyan]     save the conversation as markdown
  [cyan]/exit[/cyan]            quit (or Ctrl+D)
[bold]Keys[/bold]  Enter send, Alt+Enter new line, Ctrl+C stops the current task, Up/Down history"""
TOOL_INFO = {
    "run_python": "run Python code (asks first)", "run_shell": "run PowerShell commands (asks first)",
    "write_file": "create or overwrite files (asks first)", "read_file": "read a file", "list_dir": "list a folder",
    "web_search": "search the web", "fetch_url": "read a web page",
    "http_request": "call any web API for live data (GET runs freely, POST/PUT/DELETE ask first)",
}


AUTO_COMPACT = 0.85   # compact by itself once the context is this full
JAIL_TOOLS = {"run_python", "run_shell", "write_file"}   # run without asking while they are jailed to the folder


# Genshin holds ~3 GB but free RAM alone stayed above the pause line in a test, so known games are listed by name and
# anything else that keeps the GPU busy (gpu_pct, from nvidia-smi) counts as a game too.
STANDBY = {"pause_mb": 1200, "resume_mb": 4800, "settle": 30, "heavy": ["GenshinImpact.exe", "YuanShen.exe"],
           "fullscreen": True, "gpu_pct": 15}


def standby_settings():
    """Memory-sharing settings (config.json "standby"): pause below pause_mb free, resume at resume_mb (the model needs ~3.7 GB)."""
    return {**STANDBY, **load_config().get("standby", {})}


def default_folder():
    """Where jarvis works: ~/Downloads/jarvis (change it with `jarvis --folder X`, remembered)."""
    return Path(load_config().get("folder") or Path.home() / "Downloads" / "jarvis")


def config_path():
    return forge_home() / "config.json"


def load_config():
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(**values):
    cfg = load_config()
    cfg.update(values)
    config_path().write_text(json.dumps(cfg, indent=1), encoding="utf-8")


class Jarvis:
    def __init__(self, args, console=None):
        self.args = args
        self.console = console or Console(highlight=False, soft_wrap=False)
        self.jailed = False
        self.jail_root = None
        self.mode = "auto" if args.yes else "ask"
        self.always = set()
        self.show_stats = args.stats
        self._live = None
        self._status = None
        self._buf = []
        self._streamed = False
        args.model = args.model or load_config().get("model", "forge")
        self.llm = make_llm(args.url, args.model, args.temperature, args.llm_mode, args.providers,
                            log=lambda m: self.console.print(f"[dim]  [router] {m}[/dim]"))
        self.workspace = Path(args.workspace or default_folder()).resolve()
        self.setup_jail(args)
        # the agent's replies go through the governor: they pause while other programs need the memory (governor.py)
        st = standby_settings()
        worker = getattr(args, "worker", False)
        self.governor = Governor(args.url, args.model, pause_mb=st["pause_mb"], resume_mb=st["resume_mb"], heavy=st["heavy"],
                                 settle=st["settle"] if worker else 10, gpu_pct=st["gpu_pct"],
                                 fullscreen=st["fullscreen"] and worker,   # you are looking at jarvis itself when it is in front
                                 say=lambda m: self.console.print(f"[yellow]  {m}[/yellow]"))
        self.agent_llm = self.llm if getattr(args, "no_governor", False) else GovernedLLM(self.llm, self.governor, on_pause=self.on_pause)
        # history is kept until /compact (or auto-compact) shrinks it; the message cap is only a last resort
        self.agent = self.make_agent()
        self.transcript = []
        self.usage = Usage()
        self._window = None
        self._ratio = 1.0          # real prompt tokens / our estimate, refreshed after every task
        self.last_request = ""
        self.skills = skills.load_skills()
        self.loaded_skills = set()     # skills already shown in this conversation (they stay in the history)
        self.forced_skills = []        # /skill <name> adds one to the next request
        cfg = load_config()
        self.learn = cfg.get("learn", True)   # remember tasks that worked and recall them (/learn off stops it)
        self.remembered = False
        self.last_turn = None
        self.speaker = Speaker(cfg.get("voice_engine", "edge"), cfg.get("voice", ""), cfg.get("voice_rate", "+0%"))
        self.speaker.enabled = bool(getattr(args, "speak", False) or cfg.get("speak"))

    # ------------------------------------------------------------ the jail
    def setup_jail(self, args):
        """Reads work everywhere; writes and commands are confined to one folder by Windows itself (see jail.py)."""
        if getattr(args, "no_jail", False) or not jail.available():
            return
        root = default_folder().resolve() if not args.workspace else Path(args.workspace).resolve()
        try:
            if not jail.is_labelled(root):
                jail.label_folder(root)
        except (OSError, RuntimeError) as e:
            self.console.print(f"[yellow]could not set up the jarvis folder sandbox ({e}); running without it[/yellow]")
            return
        self.jailed, self.jail_root = True, root
        if not args.yes and load_config().get("mode") != "ask" and not args.ask:
            self.mode = "jail"

    def make_agent(self, background=False):
        """A background task has nobody to answer prompts (jailed tools still run) and no time limit: it must finish."""
        return Agent(self.agent_llm, self.make_toolbox(), confirm=self.auto_runs if background else self.confirm,
                     max_steps=14, max_history=200, verify_code=not self.args.no_verify,
                     max_seconds=None if background else self.args.turn_timeout or None, no_refuse=not self.args.allow_refusals)

    def on_pause(self, reason):
        self.end_text()
        self.busy(False)
        self.console.print("[dim]  (the part of the reply written so far is dropped and written again after the pause)[/dim]")

    def make_toolbox(self):
        return Toolbox(self.workspace, sandbox=self.jail_root if self.jailed else None)

    def auto_runs(self, name, args=None):
        """True when this tool call needs no question: autonomous mode, or a jailed file/command tool."""
        if self.mode == "auto" or name in self.always:
            return True
        return self.mode == "jail" and name in JAIL_TOOLS

    # ------------------------------------------------------------ rendering helpers
    def banner(self, server_note=""):
        info = Table.grid(padding=(0, 2))
        info.add_column(style="dim")
        info.add_column()
        info.add_row("model", f"[bold]{self.llm.model}[/bold]  [dim]Qwen3-8B fine-tune, runs on your laptop[/dim]")
        info.add_row("folder", str(self.workspace))
        info.add_row("mode", self._mode_label())
        if self.jailed:
            info.add_row("sandbox", f"[green]writes only in {self.jail_root}[/green] [dim]· reads anywhere · no admin rights[/dim]")
        if isinstance(self.llm, RoutedLLM):
            info.add_row("remote", f"{len(self.llm.remotes)} provider(s), routing {self.llm.mode}")
        if server_note:
            info.add_row("server", server_note)
        if self.speaker.enabled:
            info.add_row("voice", f"on  [dim]{self.speaker.current_label()}[/dim]")
        title = Text.assemble(("  J A R V I S", "bold cyan"), (f"   v{VERSION}", "dim"))
        self.console.print(Panel(Group(title, Text(""), info, Text(""), Text("Describe a task. I pick the tools. /help for commands.", style="dim")),
                                 border_style="cyan", padding=(0, 2)))

    def _mode_label(self):
        if self.mode == "jail":
            return "[green]sandboxed[/green] [dim]code, commands and files run without asking; they cannot write outside the folder[/dim]"
        return ("[green]autonomous[/green] [dim]tools run without asking[/dim]" if self.mode == "auto"
                else "[yellow]ask[/yellow] [dim]I ask before running code, shell commands or writing files[/dim]")

    def toolbar(self):
        return f" {self.mode} mode  |  {self.llm.model}  |  {self.workspace.name or self.workspace}  |  /help "

    def busy(self, on, message="thinking"):
        if self._status is not None:
            self._status.stop()
            self._status = None
        if on and self.console.is_terminal:
            self._status = self.console.status(f"[dim]{message}...[/dim]", spinner="dots")
            self._status.start()

    def end_text(self):
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._buf = []

    def on_token(self, tok):
        self.busy(False)
        self._streamed = True
        self._buf.append(tok)
        if not self.console.is_terminal:
            self.console.file.write(tok)
            return
        if self._live is None:
            self._live = Live(Markdown(""), console=self.console, refresh_per_second=8, vertical_overflow="visible")
            self._live.start()
        self._live.update(Markdown("".join(self._buf)))

    def tool_card(self, name, args):
        if name == "run_python":
            body = Syntax(self._clip(args.get("code", "")), "python", theme="ansi_dark", word_wrap=True)
        elif name == "run_shell":
            body = Syntax(self._clip(args.get("command", "")), "powershell", theme="ansi_dark", word_wrap=True)
        elif name == "write_file":
            path = str(args.get("path", ""))
            body = Syntax(self._clip(args.get("content", "")), Syntax.guess_lexer(path, args.get("content", "")) or "text",
                          theme="ansi_dark", word_wrap=True)
            name = f"write_file  {path}"
        else:
            body = Text(", ".join(f"{k}={v}" for k, v in args.items()) or "(no arguments)", style="white")
        return Panel(body, title=f"[bold cyan]{name}", title_align="left", border_style="cyan", padding=(0, 1))

    @staticmethod
    def _clip(text, lines=22):
        rows = str(text).splitlines()
        return "\n".join(rows[:lines]) + (f"\n... ({len(rows) - lines} more lines)" if len(rows) > lines else "")

    def on_tool(self, name, args):
        self.end_text()
        self.busy(False)
        self.console.print(self.tool_card(name, args))
        if not needs_confirmation(name, args) or self.auto_runs(name, args):
            self.busy(True, f"running {name}")

    def on_result(self, name, result):
        self.busy(False)
        rows = result.splitlines() or ["(no output)"]
        failed = result.startswith(("Error", "BLOCKED", "[stderr]")) or "[exit code" in result
        shown = "\n".join(f"  {r}" for r in rows[:12]) + (f"\n  ... ({len(rows) - 12} more lines)" if len(rows) > 12 else "")
        self.console.print(Text(shown, style="red" if failed else "dim"))
        self.busy(True, "thinking")

    def on_verify(self, kind, result):
        self.end_text()
        self.busy(False)
        label = {"failed": "it failed, asking the model to fix it", "untested": "it printed nothing, asking for real tests",
                 "output": "it ran, checking the output"}[kind]
        last = (result.strip().splitlines() or [""])[-1][:110]
        self.console.print(f"[magenta]  verify[/magenta] [dim]ran the code from the answer: {label}\n    {last}[/dim]")
        self.busy(True, "thinking")

    def on_retry(self, reason):
        self.end_text()
        self.busy(False)
        self.console.print(f"\n[yellow]  retrying[/yellow] [dim]{reason}[/dim]")
        self.busy(True, "thinking")

    def confirm(self, name, args):
        if self.auto_runs(name, args):
            return True
        self.busy(False)
        try:
            ans = self.console.input(f"[yellow]  Allow {name}?[/yellow] [dim]\\[y]es / \\[a]lways this session / \\[n]o[/dim]  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("a", "always"):
            self.always.add(name)
            ans = "y"
        ok = ans in ("y", "yes")
        self.busy(True, f"running {name}" if ok else "thinking")
        return ok

    # ------------------------------------------------------------------ one task
    # ------------------------------------------------------------ context and tokens
    @property
    def window(self):
        if self._window is None:
            self._window = context_window(self.args.url, self.llm.model)
        return self._window

    def context_used(self, extra_text=""):
        """Tokens the next prompt will take: our estimate, scaled by how far off it was on the last real call."""
        from .tools import TOOL_SCHEMAS
        parts = breakdown(self.agent.messages, TOOL_SCHEMAS)
        return round((sum(parts.values()) + estimate_tokens(extra_text)) * self._ratio), parts

    def _calibrate(self):
        """The last call's prompt was every message except the final reply: compare that with our estimate."""
        from .tools import TOOL_SCHEMAS
        if self.usage.last_prompt and len(self.agent.messages) > 1:
            est = sum(breakdown(self.agent.messages[:-1], TOOL_SCHEMAS).values())
            if est:
                self._ratio = min(2.0, max(0.5, self.usage.last_prompt / est))

    def compact(self, instructions="", auto=False):
        if len(self.agent.messages) <= 2:
            self.console.print("[dim]nothing to compact yet[/dim]")
            return False
        before, _ = self.context_used()
        self.busy(True, "compacting the conversation")
        try:
            messages, stats = compact_messages(self.llm, self.agent.messages, instructions)
        except KeyboardInterrupt:
            self.busy(False)
            self.console.print("[yellow]compact stopped; nothing changed[/yellow]")
            return False
        except Exception as e:  # noqa: BLE001
            self.busy(False)
            self.console.print(f"[red]compact failed:[/red] {e}")
            return False
        self.busy(False)
        if stats:
            self.usage.add(stats)
        self.agent.messages = messages
        self.loaded_skills.clear()          # the summary does not carry the skill notes
        after, _ = self.context_used()
        why = "context was nearly full, so " if auto else ""
        self.console.print(f"[magenta]  compacted[/magenta] [dim]{why}the conversation is now a summary: "
                           f"~{before:,} -> ~{after:,} tokens of {self.window:,}[/dim]")
        return True

    def show_context(self):
        used, parts = self.context_used()
        window = self.window
        cells = 40
        colors = ["cyan", "yellow", "green", "magenta", "blue"]
        bar, i = Text(), 0
        for (name, n), color in zip(parts.items(), colors):
            k = round(n * self._ratio * cells / window)
            k = min(k, cells - i)
            bar.append("■" * k, style=color)
            i += k
        bar.append("□" * (cells - i), style="dim")
        self.console.print(bar)
        t = Table.grid(padding=(0, 2))
        t.add_column()
        t.add_column(justify="right")
        t.add_column(justify="right", style="dim")
        for (name, n), color in zip(parts.items(), colors):
            n = round(n * self._ratio)
            t.add_row(f"[{color}]■[/{color}] {name}", f"{n:,}", f"{100 * n / window:.1f}%")
        t.add_row("[dim]□ free[/dim]", f"{max(0, window - used):,}", f"{100 * max(0, window - used) / window:.1f}%")
        self.console.print(t)
        exact = f"last prompt sent: {self.usage.last_prompt:,} tokens (exact)  |  " if self.usage.last_prompt else ""
        self.console.print(f"[dim]  {self.llm.model}: ~{used:,} / {window:,} tokens ({100 * used / window:.0f}%) "
                           f"in {len(self.agent.messages)} messages\n  {exact}auto-compact at {int(AUTO_COMPACT * 100)}%, "
                           f"/compact to do it now[/dim]")

    def show_usage(self):
        u = self.usage
        today, alltime, days = u.totals()
        t = Table(box=None, padding=(0, 2), show_header=True, header_style="bold")
        for col in ("", "model calls", "prompt (read)", "generated", "total"):
            t.add_column(col, justify="right" if col else "left")
        t.add_row("this session", f"{u.calls:,}", f"{u.prompt:,}", f"{u.completion:,}", f"[bold]{u.prompt + u.completion:,}[/bold]")
        t.add_row("today", f"{today['calls']:,}", f"{today['prompt']:,}", f"{today['completion']:,}", f"{today['prompt'] + today['completion']:,}")
        t.add_row(f"all time ({days} day{'s' * (days != 1)})", f"{alltime['calls']:,}", f"{alltime['prompt']:,}",
                  f"{alltime['completion']:,}", f"{alltime['prompt'] + alltime['completion']:,}")
        self.console.print(t)
        mins = (time.time() - u.started) / 60
        speed = f", {u.completion / u.seconds:.1f} tok/s generated on average" if u.seconds else ""
        self.console.print(f"[dim]  session {mins:.0f} min, biggest prompt {u.peak_prompt:,} of {self.window:,} tokens{speed}. "
                           f"Local tokens cost nothing; they are counted so you can see the work done.[/dim]")
        if len(u.by_backend) > 1 or isinstance(self.llm, RoutedLLM):
            for name, (p, c) in u.by_backend.items():
                self.console.print(f"[dim]  via {name}: {p:,} prompt + {c:,} generated[/dim]")

    def hints_for(self, text):
        """Skill notes and a remembered example for this request: (text to add, names of the skills used, example)."""
        picked = skills.match(text, self.skills, self.loaded_skills)
        for name in self.forced_skills:
            if name in self.skills and self.skills[name] not in picked and name not in self.loaded_skills:
                picked.append(self.skills[name])
        example = experience.best_match(text) if self.learn else None
        parts = [p for p in (skills.render(picked), experience.render(example) if example else "") if p]
        return "\n\n".join(parts), [s.name for s in picked], example

    def run_task(self, text):
        hints, picked, example = self.hints_for(text)
        used, _ = self.context_used(text + hints)
        if used > AUTO_COMPACT * self.window and len(self.agent.messages) > 2:
            if self.compact(auto=True):
                hints, picked, example = self.hints_for(text)
        self.last_request = text
        if picked or example:
            what = ([f"skill: {', '.join(picked)}"] if picked else []) + (["recalled a similar task that worked"] if example else [])
            self.console.print(f"[dim]  ({' · '.join(what)})[/dim]")
        mark = len(self.agent.messages)
        start = time.time()
        self.speaker.stop()
        self._streamed = False
        self.busy(True, "thinking")
        try:
            answer, stats = self.agent.run(text, on_token=self.on_token, on_tool=self.on_tool, on_result=self.on_result,
                                           on_verify=self.on_verify, on_retry=self.on_retry, hints=hints)
        except KeyboardInterrupt:
            self.end_text()
            self.busy(False)
            del self.agent.messages[mark:]  # drop the half-finished turn so the next request is well-formed
            self.speaker.stop()
            self.console.print("\n[yellow]stopped.[/yellow]")
            return None
        except Exception as e:  # noqa: BLE001
            self.end_text()
            self.busy(False)
            del self.agent.messages[mark:]
            self.console.print(f"\n[red]error:[/red] {e}")
            return None
        self.end_text()
        self.busy(False)
        for s in stats:
            self.usage.add(s, s["tokens"] / s["tokens_per_sec"] if s.get("tokens_per_sec") else 0.0)
        self._calibrate()
        if not self.console.is_terminal:
            self.console.print()
        elif not self._streamed:
            self.console.print(Markdown(answer))   # e.g. a time-limit message that never streamed
        if answer.endswith(VERIFIED_FOOTER):
            self.console.print("[green]  verified[/green] [dim]I ran the code and the output was correct[/dim]")
        if self.show_stats and stats:
            tok = sum(s["tokens"] for s in stats)
            used = ",".join(sorted({s.get("backend", "local") for s in stats}))
            self.console.print(f"[dim]  {tok} tokens, {stats[-1]['tokens_per_sec']:.1f} tok/s, {time.time() - start:.0f}s, "
                               f"{len(stats)} model call(s), via {used}[/dim]")
        self.speaker.say(answer.replace(VERIFIED_FOOTER, ""))
        self.transcript += [("you", text), (ASSISTANT_NAME.lower(), answer)]
        self.loaded_skills.update(picked)
        self.forced_skills.clear()
        turn = self.agent.messages[mark:]
        self.last_turn = (text, turn, answer)
        self.remembered = bool(self.learn and experience.worked(turn, answer) and experience.record(text, turn, answer))
        self.console.print()
        return answer

    def key_command(self, rest):
        """/key lists saved API keys, /key NAME value saves one, /key -NAME deletes it. The model only sees {{NAME}}."""
        c, keys = self.console, load_keys()
        name, _, value = rest.partition(" ")
        if name.startswith("-") and name[1:] in keys:
            del keys[name[1:]]
        elif name and value.strip():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                c.print("[red]key names are letters, digits and _ (e.g. OPENWEATHER_KEY)[/red]")
                return
            keys[name] = value.strip()
        elif name:
            c.print("[dim]use /key NAME value to save, /key -NAME to delete[/dim]")
            return
        if name:
            keys_path().write_text(json.dumps(keys, indent=1), encoding="utf-8")
            c.print(f"[dim]saved. The model can now write {{{{{name.lstrip('-')}}}}} in http_request and the real value is filled in.[/dim]"
                    if not name.startswith("-") else f"[dim]deleted {name[1:]}[/dim]")
            return
        if not keys:
            c.print("[dim]no API keys saved. Free APIs (weather, currency, crypto, Wikipedia, GitHub...) need none. "
                    "For others: /key NAME value[/dim]")
        for k, v in sorted(keys.items()):
            c.print(f"  [cyan]{k:<24}[/cyan] [dim]{v[:3]}...{v[-2:] if len(v) > 8 else ''}[/dim]")

    def undo(self):
        """Drop the last request and everything after it (tool calls, nudges, the answer)."""
        msgs = self.agent.messages
        for i in range(len(msgs) - 1, 0, -1):
            if msgs[i]["role"] == "user" and not msgs[i]["content"].startswith(("(system note)", "(summary of")):
                del msgs[i:]
                if len(self.transcript) >= 2:
                    del self.transcript[-2:]
                return True
        return False

    # ---------------------------------------------------------------- slash commands
    def command(self, line):
        """Returns False to quit."""
        cmd, _, rest = line.partition(" ")
        rest = rest.strip()
        c = self.console
        if cmd in ("/exit", "/quit"):
            return False
        if cmd == "/help":
            c.print(HELP)
        elif cmd == "/reset":
            self.agent.reset()
            self.transcript.clear()
            self.loaded_skills.clear()
            c.print("[dim](conversation cleared)[/dim]")
        elif cmd == "/skills":
            if rest == "reload":
                self.skills = skills.load_skills()
            for sk in self.skills.values():
                mark = "[green]*[/green]" if sk.name in self.loaded_skills else " "
                c.print(f" {mark} [cyan]{sk.name:<8}[/cyan] {sk.description}  [dim]{sk.path.parent.name}/[/dim]")
            c.print(f"[dim]  added to a request by itself when its words appear (* = already in this conversation). "
                    f"/skill <name> adds one to your next request. Add your own as .md files in {forge_home() / 'skills'}[/dim]")
        elif cmd == "/skill":
            if rest in self.skills:
                self.forced_skills.append(rest)
                c.print(f"[dim]the {rest} notes will go with your next request[/dim]")
            else:
                c.print(f"[red]no skill called[/red] {rest or '(none given)'}. /skills lists them.")
        elif cmd in ("/good", "/bad"):
            task = None
            if self.remembered:
                task = experience.mark_last(cmd == "/good")
            elif cmd == "/good" and self.last_turn and experience.record(*self.last_turn, good=True):
                task = self.last_turn[0]             # it did not look finished to the checks, but you say it was good
            if task is None:
                c.print("[dim]the last task was not saved as an example (only tool-using tasks that worked are)[/dim]")
            else:
                self.remembered = cmd == "/good"
                c.print(f"[dim]{'marked as a good example' if cmd == '/good' else 'forgotten, it will not be recalled'}: {task[:70]}[/dim]")
        elif cmd == "/learn":
            if rest in ("on", "off"):
                self.learn = rest == "on"
                save_config(learn=self.learn)
            n = len(experience.load())
            c.print(f"[dim]learning from past tasks is {'on' if self.learn else 'off'}; {n} example(s) saved in "
                    f"{experience.path()}. /learn on|off[/dim]")
        elif cmd in ("/key", "/keys"):
            self.key_command(rest)
        elif cmd == "/context":
            self.show_context()
        elif cmd == "/compact":
            self.compact(rest)
        elif cmd in ("/usage", "/cost"):
            self.show_usage()
        elif cmd in ("/undo", "/retry"):
            if not self.undo():
                c.print("[dim]nothing to undo[/dim]")
            elif cmd == "/retry" and self.last_request:
                self.run_task(self.last_request)
            else:
                c.print("[dim](last exchange forgotten)[/dim]")
        elif cmd == "/clear":
            c.clear()
            self.banner()
        elif cmd == "/auto":
            self.mode = "ask" if self.mode == "auto" else "auto"
            c.print(f"mode: {self._mode_label()}")
            if self.mode == "auto" and self.jailed:
                c.print("[dim]  (the sandbox still applies: nothing can write outside the folder)[/dim]")
        elif cmd == "/mode":
            if rest in ("ask", "auto") or (rest == "jail" and self.jailed):
                self.mode = rest
            elif rest == "jail":
                c.print("[dim]the sandbox is off (--no-jail or not Windows)[/dim]")
            c.print(f"mode: {self._mode_label()}")
        elif cmd == "/default":
            if rest in ("ask", "auto"):
                save_config(mode=rest)
                self.mode = rest
                c.print(f"[dim]jarvis will now always open in {rest} mode[/dim]")
            else:
                c.print(f"[dim]current default: {load_config().get('mode', 'ask')}. Use /default ask or /default auto[/dim]")
        elif cmd == "/speak":
            self.speak_command(rest)
        elif cmd == "/nospeak":
            self.speak_command("off")
        elif cmd == "/mute":
            self.speaker.stop()
            c.print("[dim]quiet (speak mode is still on; /nospeak turns it off)[/dim]")
        elif cmd == "/voice":
            self.voice_command(rest)
        elif cmd == "/stats":
            self.show_stats = not self.show_stats
            c.print(f"[dim]speed line {'on' if self.show_stats else 'off'}[/dim]")
        elif cmd == "/tools":
            for n in TOOL_NAMES:
                c.print(f"  [cyan]{n:<11}[/cyan] {TOOL_INFO.get(n, '')}")
        elif cmd == "/ws":
            target = Path(rest).expanduser() if rest else self.workspace
            if not target.is_dir():
                c.print(f"[red]not a folder:[/red] {target}")
            else:
                self.workspace = target.resolve()
                self.agent.toolbox = self.make_toolbox()
                c.print(f"[dim]tools now work in {self.workspace}[/dim]")
                if self.jailed and not self.workspace.is_relative_to(self.jail_root):
                    c.print(f"[yellow]  read-only there: writes only work inside {self.jail_root}[/yellow]")
        elif cmd == "/model":
            if rest:
                (self.llm.local.client if isinstance(self.llm, RoutedLLM) else self.llm).model = rest
                self.llm.model = rest
                self.governor.model = rest
            c.print(f"[dim]model: {self.llm.model}[/dim]")
        elif cmd == "/bg":
            self.background(rest)
        elif cmd == "/jobs":
            self.show_jobs(rest)
        elif cmd == "/cancel":
            t = tasks.find(rest) if rest else None
            c.print(f"[dim]cancelled {t['id']}[/dim]" if t and tasks.cancel(t["id"]) else "[dim]no open task with that number (/jobs lists them)[/dim]")
        elif cmd == "/standby":
            self.standby_command(rest)
        elif cmd in ("/remote", "/local", "/backend"):
            if not isinstance(self.llm, RoutedLLM):
                c.print("[dim]no remote providers configured (see REMOTE.md); start with --llm-mode auto after adding providers.json[/dim]")
            elif cmd == "/backend":
                c.print(f"[dim]{self.llm.status()}[/dim]")
            else:
                self.llm.force_remote = cmd == "/remote"
                c.print(f"[dim]{'remote APIs first' if self.llm.force_remote else 'normal routing'}[/dim]")
        elif cmd == "/save":
            path = Path(rest or "jarvis-session.md")
            path.write_text("\n\n".join(f"**{who}:** {text}" for who, text in self.transcript), encoding="utf-8")
            c.print(f"[dim]saved to {path.resolve()}[/dim]")
        else:
            c.print(f"[red]unknown command[/red] {cmd}. /help lists them.")
        return True

    # ------------------------------------------------------------------ background tasks
    def background(self, text):
        c = self.console
        if not text:
            c.print("[dim]usage: /bg <task>. It runs in the standby worker and survives games, low memory and restarts.[/dim]")
            return
        task = tasks.add(text)
        pid = tasks.start_worker(self.worker_args())
        n = len(tasks.all_tasks())
        c.print(f"[green]  queued[/green] [dim]as #{n} ({task['id']}); the worker (pid {pid}) is on it. /jobs shows progress; "
                f"the answer also lands in {default_folder() / 'results'}[/dim]")

    def worker_args(self):
        return worker_args(self.args, self.llm.model)

    def show_jobs(self, rest):
        c, rows = self.console, tasks.all_tasks()
        if rest:
            t = tasks.find(rest)
            if not t:
                c.print("[dim]no such task[/dim]")
                return
            c.print(f"[bold]{t['id']}[/bold] [dim]{t['status']}, {len(t['messages'])} messages saved, paused {t['pauses']}x "
                    f"({t['paused_s'] / 60:.0f} min)[/dim]\n{t['prompt']}")
            for line in t["log"][-12:]:
                c.print(f"[dim]  {line}[/dim]")
            if t["answer"]:
                c.print(Markdown(t["answer"]))
            return
        if not rows:
            c.print("[dim]no background tasks. /bg <task> adds one.[/dim]")
            return
        colour = {"done": "green", "running": "cyan", "queued": "yellow", "failed": "red", "cancelled": "dim"}
        for i, t in enumerate(rows, 1):
            last = t["log"][-1][9:70] if t["log"] else ""
            c.print(f" {i:>2}  [{colour.get(t['status'], 'white')}]{t['status']:<9}[/] {t['prompt'][:60]:<60} [dim]{last}[/dim]")
        pid = tasks.worker_pid()
        c.print(f"[dim]  worker {'running (pid %d)' % pid if pid else 'not running'}. /jobs <n> shows one, /cancel <n> stops one.[/dim]")

    def standby_command(self, rest):
        c, st = self.console, standby_settings()
        words = rest.split()
        if words[:1] in (["on"], ["off"]):
            f = tasks.set_autostart(words[0] == "on")
            c.print(f"[dim]the worker now starts when you log in ({f})[/dim]" if f else "[dim]the worker no longer starts at login[/dim]")
            if f:
                tasks.start_worker(self.worker_args())
            return
        if words[:1] == ["heavy"] and len(words) == 2:
            app = words[1].lower()
            heavy = [h for h in st["heavy"] if h != app.lstrip("-")]
            if not app.startswith("-"):
                heavy.append(app if app.endswith(".exe") else app + ".exe")
            save_config(standby={**load_config().get("standby", {}), "heavy": heavy})
            self.governor.heavy = {h.lower() for h in heavy}
            st["heavy"] = heavy
        free = free_mb() or 0
        reason = self.governor.busy_reason()
        c.print(f"[bold]memory sharing[/bold]  free RAM now {free / 1024:.1f} GB" + (f"  [yellow]({reason})[/yellow]" if reason else ""))
        c.print(f"[dim]  pause when free RAM < {st['pause_mb'] / 1024:.1f} GB, another program keeps the GPU over {st['gpu_pct']}% busy, "
                f"a full-screen app is in front (background tasks), or one of these runs: {', '.join(st['heavy']) or 'none set'}\n"
                f"  resume when {st['resume_mb'] / 1024:.1f} GB is free for {st['settle']}s; while paused the model is unloaded "
                f"(its ~3.7 GB RAM and the GPU go to your app)\n"
                f"  worker: {'running' if tasks.worker_pid() else 'not running'}, starts at login: "
                f"{'yes' if tasks.startup_file().exists() else 'no'}  (/standby on|off, /standby heavy game.exe)[/dim]")

    # ------------------------------------------------------------------ voice
    def speak_command(self, rest):
        c, sp = self.console, self.speaker
        if rest == "test":
            was, sp.enabled = sp.enabled, True
            sp.say("Hello, I am Forge. This is how I sound when I read an answer out loud.")
            sp.enabled = was
            return
        sp.enabled = {"on": True, "off": False}.get(rest, not sp.enabled)
        if not sp.enabled:
            sp.stop()
        elif load_config().get("voice_engine", "edge") == "edge":
            self.ensure_edge_tts()
        if sp.engine is None:
            c.print("[red]no voice engine is available on this computer[/red]")
            sp.enabled = False
        save_config(speak=sp.enabled)
        if sp.enabled:
            c.print(f"[green]speak mode on[/green] [dim]{sp.current_label()}. I read my answers aloud; code is skipped. /voice changes the voice, /speak off stops.[/dim]")
            if sp.engine_name == "edge":
                c.print("[dim]  note: edge voices send each answer's text to Microsoft's online service to make the audio. "
                        "/voice engine windows stays fully offline.[/dim]")
            elif sp.engine_name == "windows":
                c.print("[dim]  using the built-in Windows voice. For natural voices: pip install edge-tts, then /voice engine edge[/dim]")
        elif sp.engine is not None:
            c.print("[dim]speak mode off[/dim]")

    def ensure_edge_tts(self):
        """Natural voices need the edge-tts package: install it once if it is missing."""
        sp = self.speaker
        if speech.EdgeEngine.available():
            if sp.engine_name != "edge" and load_config().get("voice_engine", "edge") == "edge":
                sp.configure(engine="edge", voice="")
            return
        self.console.print("[dim]installing edge-tts for natural voices (one time)...[/dim]")
        done = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "edge-tts"], capture_output=True, text=True)
        if done.returncode == 0 and speech.EdgeEngine.available():
            sp.configure(engine="edge", voice="")
            self.console.print("[dim]edge-tts installed.[/dim]")
        else:
            self.console.print("[yellow]could not install edge-tts[/yellow] [dim](no internet?). Using the built-in Windows voice.[/dim]")

    def voice_command(self, rest):
        c, sp = self.console, self.speaker
        words = rest.split()
        if words[:1] == ["engine"]:
            name = words[1] if len(words) > 1 else ""
            if name not in ("edge", "windows"):
                c.print("[dim]use /voice engine edge  or  /voice engine windows[/dim]")
                return
            sp.configure(engine=name, voice="")
            if sp.engine_name != name:
                c.print(f"[yellow]{name} is not available here[/yellow] [dim](edge needs: pip install edge-tts)[/dim]")
            save_config(voice_engine=sp.engine_name, voice=sp.voice)
            c.print(f"[dim]voice: {sp.current_label()}[/dim]")
            return
        if words[:1] == ["rate"]:
            rate = normalise_rate(" ".join(words[1:]))
            if rate is None:
                c.print("[dim]use /voice rate faster | slower | normal | +10% | -20%[/dim]")
                return
            sp.configure(rate=rate)
            save_config(voice_rate=rate)
            c.print(f"[dim]voice: {sp.current_label()}[/dim]")
            return
        voices = sp.voices()
        if rest:
            pick = None
            if rest.isdigit() and 1 <= int(rest) <= len(voices):
                pick = voices[int(rest) - 1][1]
            else:
                pick = next((v for label, v in voices if rest.lower() in (label.lower(), v.lower())), None)
                if pick is None and sp.engine_name == "edge" and "-" in rest:
                    pick = rest                      # any edge-tts ShortName, e.g. en-US-AriaNeural
                if pick is None:
                    pick = next((v for label, v in voices if rest.lower() in label.lower()), None)
            if pick is None:
                c.print(f"[red]no voice called[/red] {rest}")
                return
            sp.configure(voice=pick)
            save_config(voice=pick, voice_engine=sp.engine_name)
            c.print(f"[dim]voice: {sp.current_label()}[/dim]")
            was, sp.enabled = sp.enabled, True
            sp.say("Hi, this is my voice.")
            sp.enabled = was
            return
        c.print(f"[bold]voices[/bold] [dim]({sp.engine_name}; current: {sp.voice or 'default'})[/dim]")
        for i, (label, v) in enumerate(voices, 1):
            mark = "[green]*[/green]" if v == sp.voice else " "
            c.print(f" {mark} {i:>2}  {label}")
        c.print("[dim]  /voice <number> picks one and plays a sample. /voice engine edge|windows, /voice rate faster|slower[/dim]")

    # ------------------------------------------------------------------ the loop
    def repl(self, first_prompt="", session=None):
        if session is None:
            session = self.make_session()
        pending = first_prompt
        while True:
            try:
                line = pending or session.prompt()
            except KeyboardInterrupt:
                self.console.print("[dim](Ctrl+C stops a running task. Use /exit or Ctrl+D to quit.)[/dim]")
                continue
            except EOFError:
                break
            pending = ""
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                if not self.command(line):
                    break
                continue
            self.run_task(line)
        self.speaker.stop()
        self.console.print("[dim]bye.[/dim]")

    def make_session(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style

        hist = forge_home()
        kb = KeyBindings()

        @kb.add("escape", "enter")
        def _newline(event):
            event.current_buffer.insert_text("\n")

        return PromptSession(
            message=HTML("<b><style fg='ansicyan'>jarvis</style></b> <style fg='ansibrightblack'>&gt;</style> "),
            history=FileHistory(str(hist / "history")), key_bindings=kb, bottom_toolbar=self.toolbar,
            style=Style.from_dict({"bottom-toolbar": "noreverse fg:ansibrightblack"}),
            enable_history_search=True, multiline=False)


def worker_args(args, model):
    """The flags a background worker needs to work like this session (same server, model, folder, sandbox)."""
    out = ["--url", args.url, "--model", model]
    if args.workspace:
        out += ["--workspace", str(Path(args.workspace).resolve())]
    return out + (["--no-jail"] if args.no_jail else [])


def run_worker(args):
    """`jarvis --worker`: the standby loop. Its output goes to ~/.forge/tasks/worker.log."""
    log = open(tasks.log_path(), "a", encoding="utf-8", buffering=1)
    console = Console(file=log, highlight=False, width=140, force_terminal=False, color_system=None)
    say = lambda m: console.print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {m}")
    ui = Jarvis(args, console)
    ui.governor.say = say
    if ui.jailed and not args.yes:
        ui.mode = "jail"            # code, commands and files run without asking, but only inside the jarvis folder

    def ensure():
        ok, problem = serve.ensure_ready(args.url, args.model, say=say, start=not args.no_start)
        if not ok:
            raise RuntimeError(problem)

    say(f"worker started (pid {os.getpid()}), mode {ui.mode}, model {args.model}")
    try:
        done = tasks.work(lambda: ui.make_agent(background=True), lambda t: ui.hints_for(t)[0], say,
                          results_dir=default_folder() / "results", once=args.until_empty, idle=ui.governor.unload, ensure=ensure)
    except KeyboardInterrupt:
        done = []
    say(f"worker stopped after {len(done)} task(s)")
    return done


def build_parser():
    p = argparse.ArgumentParser(prog="jarvis", description="Terminal agent for your local Forge model.")
    p.add_argument("prompt", nargs="*", help="a task to start with")
    p.add_argument("-p", "--print", dest="once", action="store_true", help="run the task, print the answer, exit")
    p.add_argument("--yes", action="store_true", help="autonomous: run tools without asking")
    p.add_argument("--ask", action="store_true", help="ask before running tools, even if your saved default is autonomous")
    p.add_argument("--url", default="http://127.0.0.1:11434/v1")
    p.add_argument("--model", default=None, help="Ollama model (default: your saved choice, else forge)")
    p.add_argument("--open", action="store_true", help="use the uncensored model (forge-open) and remember it as the default")
    p.add_argument("--standard", action="store_true", help="go back to the standard model (forge) and remember it")
    p.add_argument("--workspace", default=None, help="folder the tools work in (default: ~/Downloads/jarvis). "
                   "It is also the only folder jarvis can write to")
    p.add_argument("--here", action="store_true", help="work in (and be allowed to write to) the current folder instead")
    p.add_argument("--no-jail", action="store_true", help="turn the write sandbox off (old behaviour: asks before each risky tool)")
    p.add_argument("--temperature", type=float, default=0.3)
    p.add_argument("--turn-timeout", type=int, default=900, help="seconds before a single task is stopped (0 = never)")
    p.add_argument("--llm-mode", choices=["local", "auto", "remote"], default="local", help="use remote APIs too (see REMOTE.md)")
    p.add_argument("--providers", default=str(REPO / "providers.json"))
    p.add_argument("--no-verify", action="store_true", help="do not run code from answers to check it")
    p.add_argument("--allow-refusals", action="store_true", help="let the model refuse (by default a refusal is pushed back once)")
    p.add_argument("--no-start", action="store_true", help="do not start Ollama automatically")
    p.add_argument("--stats", action="store_true", help="show tokens/s after each answer")
    p.add_argument("--speak", action="store_true", help="read answers aloud (same as typing /speak)")
    p.add_argument("--bg", action="store_true", help="queue the task for the standby worker instead of running it now")
    p.add_argument("--jobs", action="store_true", help="list background tasks and exit")
    p.add_argument("--worker", action="store_true", help="run the standby worker: work through queued tasks, sharing memory with your apps")
    p.add_argument("--until-empty", action="store_true", help="with --worker: exit when the queue is empty")
    p.add_argument("--no-governor", action="store_true", help="never pause for other programs (memory sharing off)")
    p.add_argument("--check", action="store_true", help="check the setup and exit")
    p.add_argument("--version", action="version", version=f"jarvis {VERSION}")
    return p


def main(argv=None):
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    args = build_parser().parse_args(argv)
    if args.here:
        args.workspace = "."
    if args.open or args.standard:
        save_config(model="forge-open" if args.open else "forge")
    args.model = args.model or load_config().get("model", "forge")
    if load_config().get("mode") == "auto" and not args.ask:
        args.yes = True  # your saved default (/default auto)
    console = Console(highlight=False)
    if args.jobs:
        Jarvis.show_jobs(argparse.Namespace(console=console), "")
        return
    if args.bg:
        task = " ".join(args.prompt).strip() or (sys.stdin.read().strip() if not sys.stdin.isatty() else "")
        if not task:
            console.print("[red]no task given[/red]")
            sys.exit(2)
        t = tasks.add(task)
        pid = tasks.start_worker(worker_args(args, args.model))
        console.print(f"queued {t['id']}; worker pid {pid}. `jarvis --jobs` shows progress; the answer lands in "
                      f"{default_folder() / 'results'}")
        return
    if args.worker:
        run_worker(args)
        return

    with console.status("[dim]checking the model server...[/dim]") if console.is_terminal else _Null() as st:
        ok, problem = serve.ensure_ready(args.url, args.model, say=lambda m: console.print(f"[dim]{m}[/dim]"), start=not args.no_start)
    if args.check:
        console.print(f"server  {args.url}  {'[green]ready[/green]' if ok else '[red]' + problem + '[/red]'}")
        console.print(f"model   {args.model}")
        console.print(f"tools   {', '.join(TOOL_NAMES)}")
        console.print(f"folder  {Path(args.workspace or default_folder()).resolve()}")
        console.print(f"jail    {'off' if args.no_jail or not jail.available() else 'on: writes only in the folder'}")
        sys.exit(0 if ok else 1)
    if not ok:
        console.print(f"[red]{problem}[/red]")
        sys.exit(1)
    serve.warm_up(args.url, args.model)

    ui = Jarvis(args, console)
    task = " ".join(args.prompt).strip()
    piped = not sys.stdin.isatty()
    if args.once or piped:
        if piped and not task:
            task = sys.stdin.read().strip()
        if not task:
            console.print("[red]no task given[/red]")
            sys.exit(2)
        if piped and not args.yes and ui.mode != "jail":
            ui.agent.confirm = lambda name, a: False  # nothing can answer a permission prompt, so risky tools are declined
        ui.run_task(task)
        return
    ui.banner(server_note="ready" if ok else "")
    ui.repl(first_prompt=task)


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
