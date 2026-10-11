"""How often does a model decline, or talk like "an AI"? Runs held-out prompts against an Ollama model.

    python -m bench.refusal_check                      # the `forge` model
    python -m bench.refusal_check --model forge-open   # the uncensored one
    python -m bench.refusal_check --samples 3 -q       # three tries per prompt, only the summary

Groups: adult writing, edgy, and persona (a persona reply fails if it says "as an AI" / "language model" / "I can't").
None of the held-out prompts are used to make training data.
"""

import argparse
import sys

from assistant.llm import LLMClient
from assistant.prompts import SYSTEM_PROMPT
from data.persona_prompts import AI_TALK, EVAL_ADULT, EVAL_EDGY, EVAL_PERSONA, declined
from data.refusal_prompts import ADULT_WRITING, EDGY, is_refusal_text

GROUPS = (
    ("adult writing", ADULT_WRITING + EVAL_ADULT, lambda t: is_refusal_text(t) or declined(t)),
    ("edgy", EDGY + EVAL_EDGY, lambda t: is_refusal_text(t) or declined(t)),
    ("persona", EVAL_PERSONA, lambda t: bool(AI_TALK.search(t)) or declined(t)),
)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default="forge")
    p.add_argument("--url", default="http://127.0.0.1:11434/v1")
    p.add_argument("--no-system", action="store_true", help="test without the Forge system prompt")
    p.add_argument("--samples", type=int, default=1, help="tries per prompt (sampling is random)")
    p.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    a = p.parse_args()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    llm = LLMClient(a.url, a.model, temperature=0.7, max_tokens=120,
                    extra_body={"reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}})
    if not llm.health():
        sys.exit(f"can't reach {a.url}; start Ollama first")
    totals = {}
    for group, prompts, failed in GROUPS:
        bad = n = 0
        for q in prompts:
            for _ in range(a.samples):
                msgs = ([] if a.no_system else [{"role": "system", "content": SYSTEM_PROMPT}]) + [{"role": "user", "content": q}]
                reply, _ = llm.chat(msgs)
                text = reply["content"].strip()
                no = failed(text)
                bad += no
                n += 1
                if not a.quiet:
                    print(f"[{'FAILED' if no else 'ok'}] {q[:70]}\n     {text[:110]!r}", flush=True)
        totals[group] = (bad, n)
    print()
    for group, (bad, n) in totals.items():
        print(f"{group}: failed {bad}/{n} ({100 * bad / max(1, n):.0f}%)")


if __name__ == "__main__":
    main()
