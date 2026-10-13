"""Turn the tasks jarvis remembered (~/.forge/experience.jsonl) into training rows for the next fine-tune.

    python scripts/export_experience.py            writes data/out/experience.jsonl
    python scripts/export_experience.py --good     only the ones you marked with /good

data/build_dataset.py picks the file up by itself. The rows keep the user's request without the skill notes that
were shown at the time, so the next model learns to do the task without them.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from assistant import experience  # noqa: E402
from assistant.agent import VERIFIED_FOOTER  # noqa: E402
from assistant.prompts import SYSTEM_PROMPT  # noqa: E402


def clean(row):
    """Messages ready for training, or None when the turn needed a correction (a nudge) to get there."""
    msgs = row["messages"]
    if any(m["role"] == "user" and m["content"].startswith("(system note)") for m in msgs[1:]):
        return None   # the model was pushed back at least once; the first attempt is not something to teach
    out = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in msgs:
        m = dict(m)
        if m["role"] == "assistant" and m.get("content", "").endswith(VERIFIED_FOOTER):
            m["content"] = m["content"][: -len(VERIFIED_FOOTER)]
        out.append(m)
    return out if out[-1]["role"] == "assistant" and out[-1].get("content") else None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--good", action="store_true", help="only examples confirmed with /good")
    p.add_argument("--out", default=str(ROOT / "data" / "out" / "experience.jsonl"))
    a = p.parse_args()
    rows, seen, skipped = [], set(), 0
    for r in experience.load():
        if a.good and not r.get("good"):
            continue
        key = r["task"].strip().lower()
        msgs = clean(r)
        if msgs is None or key in seen:
            skipped += 1
            continue
        seen.add(key)
        rows.append({"id": hashlib.sha1(key.encode()).hexdigest()[:12], "category": "experience", "messages": msgs,
                     "tools": "LOCAL"})
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} examples to {a.out} ({skipped} skipped: duplicates or turns that needed a correction)")


if __name__ == "__main__":
    main()
