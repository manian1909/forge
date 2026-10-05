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
    "training/formatting.py",
    "training/finetune.py",
    "training/export.py",
    "kaggle/run_all.py",
    "KAGGLE.md",
]


def main():
    missing = [f for f in INCLUDE if not (ROOT / f).exists()]
    if missing:
        raise SystemExit(f"missing files: {missing}")
    n_agentic = sum(1 for line in open(ROOT / "data/out/agentic.jsonl", encoding="utf-8") if line.strip())

    DIST.mkdir(exist_ok=True)
    target = DIST / "forge-kit.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in INCLUDE:
            z.write(ROOT / rel, f"forge-kit/{rel}")
        # empty package markers so `training` and `data` import cleanly
        z.writestr("forge-kit/training/__init__.py", "")
        z.writestr("forge-kit/kit_info.json", json.dumps({"agentic_conversations": n_agentic}, indent=2))
    print(f"wrote {target} ({target.stat().st_size / 1e6:.1f} MB, {n_agentic} agentic conversations)")


if __name__ == "__main__":
    main()
