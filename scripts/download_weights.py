"""Download the Forge GGUF from Hugging Face into models/."""
import argparse
from pathlib import Path

from huggingface_hub import hf_hub_download

REPO = "manian1909/forge-8b-GGUF"
FILE = "forge-8b-Q4_K_M.gguf"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--file", default=FILE)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "models"))
    a = ap.parse_args()
    Path(a.out).mkdir(parents=True, exist_ok=True)
    path = hf_hub_download(a.repo, a.file, local_dir=a.out)
    print("saved", path)


if __name__ == "__main__":
    main()
