"""Package everything Kaggle needs into dist/forge-kit.zip.

    python scripts/make_kaggle_kit.py

Upload the zip as a Kaggle Dataset (see KAGGLE.md); Kaggle unpacks it to
/kaggle/input/<dataset-name>/forge-kit/.
"""

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
INCLUDE = [
    "assistant/__init__.py",
    "assistant/prompts.py",
    "assistant/tools.py",
    "assistant/llm.py",
    "assistant/agent.py",
    "data/build_dataset.py",
    "data/out/agentic.jsonl",
    "data/out/verified.jsonl",
    "data/out/persona.jsonl",
    "data/persona_prompts.py",
    "training/formatting.py",
    "training/finetune.py",
    "training/export.py",
    "kaggle/run_all.py",
    "kaggle/abliterate.py",
    "data/refusal_prompts.py",
    "KAGGLE.md",
]


def main():
    missing = [f for f in INCLUDE if not (ROOT / f).exists()]
    if missing:
        raise SystemExit(f"missing files: {missing}")
    n_agentic = sum(1 for line in open(ROOT / "data/out/agentic.jsonl", encoding="utf-8") if line.strip())
    n_verified = sum(1 for line in open(ROOT / "data/out/verified.jsonl", encoding="utf-8") if line.strip())

    n_persona = sum(1 for line in open(ROOT / "data/out/persona.jsonl", encoding="utf-8") if line.strip())

    DIST.mkdir(exist_ok=True)
    target = DIST / "forge-kit.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in INCLUDE:
            z.write(ROOT / rel, f"forge-kit/{rel}")
        # empty package markers so `training` and `data` import cleanly
        z.writestr("forge-kit/training/__init__.py", "")
        z.writestr("forge-kit/kit_info.json", json.dumps({"agentic_conversations": n_agentic, "verified_conversations": n_verified, "persona_conversations": n_persona}, indent=2))
    print(f"wrote {target} ({target.stat().st_size / 1e6:.1f} MB, {n_agentic} agentic + {n_verified} verified + {n_persona} persona conversations)")


if __name__ == "__main__":
    main()
