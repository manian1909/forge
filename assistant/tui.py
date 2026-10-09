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

from . import serve
from .agent import VERIFIED_FOOTER, Agent
from .prompts import ASSISTANT_NAME
from .router import RoutedLLM, make_llm
from .tools import NEEDS_CONFIRMATION, TOOL_NAMES, Toolbox

VERSION = "0.2"
REPO = Path(__file__).resolve().parent.parent
HELP = """[bold]Commands[/bold]
  [cyan]/help[/cyan]            this list
  [cyan]/auto[/cyan]            toggle fully autonomous mode (no permission prompts)
  [cyan]/mode ask|auto[/cyan]   choose the permission mode
  [cyan]/reset[/cyan]           forget the conversation   [cyan]/clear[/cyan] clear the screen
  [cyan]/ws <folder>[/cyan]     change the working folder the tools use
  [cyan]/model <name>[/cyan]    switch the Ollama model for this session
  [cyan]/remote[/cyan] [cyan]/local[/cyan] [cyan]/backend[/cyan]   force remote APIs, release, show router status (needs providers.json)
  [cyan]/stats[/cyan]           toggle the speed line after each answer
  [cyan]/tools[/cyan]           list what the model can do
  [cyan]/save <file>[/cyan]     save the conversation as markdown
  [cyan]/exit[/cyan]            quit (or Ctrl+D)
[bold]Keys[/bold]  Enter send, Alt+Enter new line, Ctrl+C stops the current task, Up/Down history"""
TOOL_INFO = {
    "run_python": "run Python code (asks first)", "run_shell": "run PowerShell commands (asks first)",
    "write_file": "create or overwrite files (asks first)", "read_file": "read a file", "list_dir": "list a folder",
    "web_search": "search the web", "fetch_url": "read a web page",
}


class Jarvis:
    def __init__(self, args, console=None):
        self.args = args
        self.console = console or Console(highlight=False, soft_wrap=False)
        self.mode = "auto" if args.yes else "ask"
        self.always = set()
        self.show_stats = args.stats
        self._live = None
        self._status = None
        self._buf = []
        self._streamed = False
        self.llm = make_llm(args.url, args.model, args.temperature, args.llm_mode, args.providers,
                            log=lambda m: self.console.print(f"[dim]  [router] {m}[/dim]"))
        self.workspace = Path(args.workspace).resolve()
        self.agent = Agent(self.llm, Toolbox(self.workspace), confirm=self.confirm, max_steps=14,
                           verify_code=not args.no_verify, max_seconds=args.turn_timeout or None)
        self.transcript = []

    # ------------------------------------------------------------ rendering helpers
    def banner(self, server_note=""):
        info = Table.grid(padding=(0, 2))
        info.add_column(style="dim")
        info.add_column()
        info.add_row("model", f"[bold]{self.llm.model}[/bold]  [dim]Qwen3-8B fine-tune, runs on your laptop[/dim]")
        info.add_row("folder", str(self.workspace))
        info.add_row("mode", self._mode_label())
        if isinstance(self.llm, RoutedLLM):
            info.add_row("remote", f"{len(self.llm.remotes)} provider(s), routing {self.llm.mode}")
        if server_note:
            info.add_row("server", server_note)
        title = Text.assemble(("  J A R V I S", "bold cyan"), (f"   v{VERSION}", "dim"))
        self.console.print(Panel(Group(title, Text(""), info, Text(""), Text("Describe a task. I pick the tools. /help for commands.", style="dim")),
                                 border_style="cyan", padding=(0, 2)))

    def _mode_label(self):
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
        if name not in NEEDS_CONFIRMATION or self.mode == "auto" or name in self.always:
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

    def confirm(self, name, args):
        if self.mode == "auto" or name in self.always:
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
    def run_task(self, text):
        mark = len(self.agent.messages)
        start = time.time()
        self._streamed = False
        self.busy(True, "thinking")
        try:
            answer, stats = self.agent.run(text, on_token=self.on_token, on_tool=self.on_tool, on_result=self.on_result,
                                           on_verify=self.on_verify)
        except KeyboardInterrupt:
            self.end_text()
            self.busy(False)
            del self.agent.messages[mark:]  # drop the half-finished turn so the next request is well-formed
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
        self.transcript += [("you", text), (ASSISTANT_NAME.lower(), answer)]
        self.console.print()
        return answer

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
            c.print("[dim](conversation cleared)[/dim]")
        elif cmd == "/clear":
            c.clear()
            self.banner()
        elif cmd == "/auto":
            self.mode = "ask" if self.mode == "auto" else "auto"
            c.print(f"mode: {self._mode_label()}")
        elif cmd == "/mode":
            if rest in ("ask", "auto"):
                self.mode = rest
            c.print(f"mode: {self._mode_label()}")
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
                self.agent.toolbox = Toolbox(self.workspace)
                c.print(f"[dim]tools now work in {self.workspace}[/dim]")
        elif cmd == "/model":
            if rest:
                (self.llm.local.client if isinstance(self.llm, RoutedLLM) else self.llm).model = rest
                self.llm.model = rest
            c.print(f"[dim]model: {self.llm.model}[/dim]")
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
        self.console.print("[dim]bye.[/dim]")

    def make_session(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style

        hist = Path.home() / ".forge"
        hist.mkdir(exist_ok=True)
        kb = KeyBindings()

        @kb.add("escape", "enter")
        def _newline(event):
            event.current_buffer.insert_text("\n")

        return PromptSession(
            message=HTML("<b><style fg='ansicyan'>jarvis</style></b> <style fg='ansibrightblack'>&gt;</style> "),
            history=FileHistory(str(hist / "history")), key_bindings=kb, bottom_toolbar=self.toolbar,
            style=Style.from_dict({"bottom-toolbar": "noreverse fg:ansibrightblack"}),
            enable_history_search=True, multiline=False)


def build_parser():
    p = argparse.ArgumentParser(prog="jarvis", description="Terminal agent for your local Forge model.")
    p.add_argument("prompt", nargs="*", help="a task to start with")
    p.add_argument("-p", "--print", dest="once", action="store_true", help="run the task, print the answer, exit")
    p.add_argument("--yes", action="store_true", help="autonomous: run tools without asking")
    p.add_argument("--url", default="http://127.0.0.1:11434/v1")
    p.add_argument("--model", default="forge")
    p.add_argument("--workspace", default=".", help="folder the tools work in (default: where you ran jarvis)")
    p.add_argument("--temperature", type=float, default=0.3)
    p.add_argument("--turn-timeout", type=int, default=900, help="seconds before a single task is stopped (0 = never)")
    p.add_argument("--llm-mode", choices=["local", "auto", "remote"], default="local", help="use remote APIs too (see REMOTE.md)")
    p.add_argument("--providers", default=str(REPO / "providers.json"))
    p.add_argument("--no-verify", action="store_true", help="do not run code from answers to check it")
    p.add_argument("--no-start", action="store_true", help="do not start Ollama automatically")
    p.add_argument("--stats", action="store_true", help="show tokens/s after each answer")
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
    console = Console(highlight=False)

    with console.status("[dim]checking the model server...[/dim]") if console.is_terminal else _Null() as st:
        ok, problem = serve.ensure_ready(args.url, args.model, say=lambda m: console.print(f"[dim]{m}[/dim]"), start=not args.no_start)
    if args.check:
        console.print(f"server  {args.url}  {'[green]ready[/green]' if ok else '[red]' + problem + '[/red]'}")
        console.print(f"model   {args.model}")
        console.print(f"tools   {', '.join(TOOL_NAMES)}")
        console.print(f"folder  {Path(args.workspace).resolve()}")
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
        if piped and not args.yes:
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
