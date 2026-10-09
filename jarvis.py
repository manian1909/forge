"""Entry point for the `jarvis` command. Run `python scripts/install_jarvis.py` once to make `jarvis` work anywhere."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from assistant.tui import main  # noqa: E402

if __name__ == "__main__":
    main()
