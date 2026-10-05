"""Chat with your model in the terminal.

    python chat.py                       # talks to llama-server on :8080
    python chat.py --auto                # run tools without asking
    python chat.py --url http://127.0.0.1:11434/v1 --model qwen3:8b   # any OpenAI-compatible server

Commands: /reset clears the conversation, /auto toggles tool confirmation, /exit quits.
"""

import argparse
import json
import sys

from assistant.agent import Agent
from assistant.llm import LLMClient
from assistant.prompts import ASSISTANT_NAME
from assistant.tools import Toolbox

DIM, CYAN, YELLOW, RESET = "\033[2m", "\033[36m", "\033[33m", "\033[0m"


def preview(args):
    text = args.get("code") or args.get("command") or json.dumps(args, ensure_ascii=False)
    lines = text.splitlines() or [""]
    shown = "\n".join(f"    {l}" for l in lines[:15])
    return shown + (f"\n    ... ({len(lines) - 15} more lines)" if len(lines) > 15 else "")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:8080/v1")
    p.add_argument("--model", default="local")
    p.add_argument("--workspace", default="workspace")
    p.add_argument("--auto", action="store_true", help="run python/shell/write tools without asking")
    p.add_argument("--temperature", type=float, default=0.6)
    p.add_argument("--stats", action="store_true", help="print speed after each reply")
    a = p.parse_args()

    if sys.platform == "win32":
        import os

        os.system("")  # enables ANSI colours in the Windows console
        sys.stdout.reconfigure(encoding="utf-8")

    llm = LLMClient(a.url, a.model, temperature=a.temperature)
    if not llm.health():
        print(f"Can't reach a model server at {a.url}. Start it with scripts/serve.ps1 first.")
        sys.exit(1)

    state = {"auto": a.auto}

    def confirm(name, args):
        if state["auto"]:
            return True
        answer = input(f"{YELLOW}  run {name}? [y/N] {RESET}").strip().lower()
        return answer in ("y", "yes")

    agent = Agent(llm, Toolbox(a.workspace), confirm=confirm)

    def on_tool(name, args):
        print(f"\n{CYAN}  > {name}{RESET}\n{DIM}{preview(args)}{RESET}")

    def on_result(name, result):
        lines = result.splitlines()
        shown = "\n".join(f"    {l}" for l in lines[:12])
        more = f"\n    ... ({len(lines) - 12} more lines)" if len(lines) > 12 else ""
        print(f"{DIM}{shown}{more}{RESET}")

    print(f"{ASSISTANT_NAME} ready. Tools {'run automatically' if a.auto else 'ask before running'}. /exit to quit.\n")
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user in ("/exit", "/quit"):
            break
        if user == "/reset":
            agent.reset()
            print("(conversation cleared)")
            continue
        if user == "/auto":
            state["auto"] = not state["auto"]
            print(f"(auto-run tools: {'on' if state['auto'] else 'off'})")
            continue

        print(f"{ASSISTANT_NAME.lower()}> ", end="", flush=True)
        try:
            _, stats = agent.run(
                user,
                on_token=lambda t: print(t, end="", flush=True),
                on_tool=on_tool,
                on_result=on_result,
            )
        except KeyboardInterrupt:
            print("\n(interrupted)")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"\n[error] {e}")
            continue
        print()
        if a.stats and stats:
            tok = sum(s["tokens"] for s in stats)
            tps = stats[-1]["tokens_per_sec"]
            print(f"{DIM}  [{tok} tokens, {tps:.1f} tok/s, first token {stats[0]['time_to_first_token']:.2f}s]{RESET}")
        print()


if __name__ == "__main__":
    main()
