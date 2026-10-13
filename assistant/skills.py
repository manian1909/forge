"""Skills: short, tested how-to notes that are added to a request only when it needs them.

An 8B model knows roughly how reportlab or openpyxl work but gets details wrong. A recipe that is known to run, shown
right next to the request, fixes most of that without any training, and costs context only when it is relevant.

A skill is a markdown file with a small header:

    ---
    name: excel
    description: one line for /skills
    triggers: excel|xlsx|spreadsheet        (a regular expression, case-insensitive)
    ---
    the notes and example code

Built-in skills live in ``skills/`` in the repo. Your own go in ``~/.forge/skills/`` and win over a built-in one with
the same name.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from .session import forge_home

BUILTIN = Path(__file__).resolve().parent.parent / "skills"
MAX_PER_TURN = 2


@dataclass
class Skill:
    name: str
    description: str
    triggers: re.Pattern
    body: str
    path: Path


def parse(path):
    text = Path(path).read_text(encoding="utf-8")
    m = re.match(r"---\s*\n(.*?)\n---\s*\n(.*)", text, re.S)
    if not m:
        return None
    head = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
    head = {k.strip(): v.strip() for k, v in head.items()}
    try:
        trig = re.compile(head.get("triggers") or r"(?!)", re.I)
    except re.error:
        return None
    return Skill(head.get("name") or Path(path).stem, head.get("description", ""), trig, m.group(2).strip(), Path(path))


def load_skills(extra_dirs=None):
    found = {}
    for folder in [BUILTIN, forge_home() / "skills", *(extra_dirs or [])]:
        for p in sorted(Path(folder).glob("*.md")) if Path(folder).is_dir() else []:
            skill = parse(p)
            if skill:
                found[skill.name] = skill
    return found


def match(text, skills, already=()):
    """Skills whose triggers appear in the request, most trigger hits first, skipping ones already in the conversation."""
    scored = []
    for s in skills.values():
        if s.name in already:
            continue
        hits = len(s.triggers.findall(text))
        if hits:
            scored.append((hits, s.name, s))
    return [s for _, _, s in sorted(scored, key=lambda x: (-x[0], x[1]))[:MAX_PER_TURN]]


def render(skills):
    return "\n\n".join(f"(skill notes: {s.name})\n{s.body}" for s in skills)
