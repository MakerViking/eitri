"""The UI must never use fire imagery or fire wording: owners worry about printer fires, and the
smith motif (anvil, steel, tempering, runes) says everything the product needs without it."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BANNED = re.compile(r"\b(fires?|flames?|flaming|embers?|blaze|blazing|white-hot|sparks?|sparkl\w*|inferno|ignit\w*|burn\w*|torch\w*|smolder\w*|cinders?|kenaz)\b"
                    r"|\bis hot\b|\bhot metal\b", re.I)
SCANNED = [*(ROOT / "web").rglob("*"), *(ROOT / "eitri_configurator").rglob("*.py"), ROOT / "README.md", ROOT / "tools" / "screenshots.py"]
TEXT = {".js", ".css", ".html", ".svg", ".py", ".md", ".json"}


def test_no_fire_imagery_or_wording_in_user_facing_files():
    hits = []
    for path in SCANNED:
        if path.suffix not in TEXT or "fonts" in path.parts or "__pycache__" in path.parts:
            continue
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if m := BANNED.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {m.group(0)!r}")
    assert not hits, "fire wording or imagery found:\n" + "\n".join(hits)


def test_icons_and_tokens_have_no_fire_names():
    dom = (ROOT / "web" / "js" / "dom.js").read_text()
    icons = set(re.findall(r"^  (\w+):\s*['\"]", dom, re.M))
    assert {"hammer", "nozzle"} <= icons and not {"flame", "fire", "torch"} & icons
    css = "".join(p.read_text() for p in (ROOT / "web" / "css").glob("*.css"))
    assert "--accent:" in css and "--ember" not in css


def test_no_animated_particles():
    """No rising or flying particles (the old drifting-ember and spark-shower effects)."""
    css = "".join(p.read_text() for p in (ROOT / "web" / "css").glob("*.css"))
    assert not re.search(r"@keyframes\s+(ember|fly|flicker)", css)
    index = (ROOT / "web" / "index.html").read_text()
    assert 'id="embers"' not in index
