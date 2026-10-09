"""Chat with your model in the terminal.

    python chat.py                       # talks to llama-server on :8080
    python chat.py --auto                # run tools without asking
    python chat.py --url http://127.0.0.1:11434/v1 --model qwen3:8b   # any OpenAI-compatible server

Commands: /reset clears the conversation, /auto toggles tool confirmation, /remote and /local force or release
remote APIs, /backend shows router status, /exit quits.
"""

import argparse
import json
import sys

from assistant.agent import Agent
from assistant.router import RoutedLLM, make_llm
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
    p.add_argument("--temperature", type=float, default=0.3)
    p.add_argument("--mode", choices=["local", "auto", "remote"], default="local",
                   help="local: never leave this machine. auto: local first, remote APIs when it struggles. remote: APIs first")
    p.add_argument("--providers", default="providers.json", help="remote API list (see providers.example.json)")
    p.add_argument("--stats", action="store_true", help="print speed after each reply")
    p.add_argument("--no-verify", action="store_true", help="don't run the code in answers to check it before replying")
    a = p.parse_args()

    if sys.platform == "win32":
        import os

        os.system("")  # enables ANSI colours in the Windows console
        sys.stdout.reconfigure(encoding="utf-8")

    llm = make_llm(a.url, a.model, a.temperature, a.mode, a.providers,
                   log=lambda m: print(f"{DIM}  [router] {m}{RESET}"))
    if not llm.health():
        print(f"Can't reach a model server at {a.url}. Start it with scripts/serve.ps1 first.")
        sys.exit(1)

    state = {"auto": a.auto}

    def confirm(name, args):
        if state["auto"]:
            return True
        answer = input(f"{YELLOW}  run {name}? [y/N] {RESET}").strip().lower()
        return answer in ("y", "yes")

    agent = Agent(llm, Toolbox(a.workspace), confirm=confirm, verify_code=not a.no_verify)

    def on_tool(name, args):
        print(f"\n{CYAN}  > {name}{RESET}\n{DIM}{preview(args)}{RESET}")

    def on_result(name, result):
        lines = result.splitlines()
        shown = "\n".join(f"    {l}" for l in lines[:12])
        more = f"\n    ... ({len(lines) - 12} more lines)" if len(lines) > 12 else ""
        print(f"{DIM}{shown}{more}{RESET}")

    def on_verify(kind, result):
        label = {"failed": "failed, asking the model to fix it", "untested": "ran but printed nothing, asking for real tests",
                 "output": "ran OK, checking the output"}[kind]
        first = (result.strip().splitlines() or [""])[-1][:100]
        print(f"\n{CYAN}  [verify] ran the code from the answer: {label}{RESET}\n{DIM}    {first}{RESET}")

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
        if user in ("/remote", "/local", "/backend"):
            if not isinstance(llm, RoutedLLM):
                print("(no remote providers configured; run with --mode auto after adding providers.json)")
            elif user == "/backend":
                print(llm.status())
            else:
                llm.force_remote = user == "/remote"
                print(f"(next turns go {'to remote APIs first' if llm.force_remote else 'by the normal routing rules'})")
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
                on_verify=on_verify,
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
            used = ",".join(sorted({s.get("backend", "local") for s in stats}))
            print(f"{DIM}  [{tok} tokens, {tps:.1f} tok/s, first token {stats[0]['time_to_first_token']:.2f}s, via {used}]{RESET}")
        print()


if __name__ == "__main__":
    main()
