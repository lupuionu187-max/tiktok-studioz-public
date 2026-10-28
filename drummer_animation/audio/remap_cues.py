"""Rebuild the `sfx` list of a split-screen cue file from its source short.

Each segment in src/story/same.json says "panel P shows scene S from source time
s0..s1 during t0..t1". Every SFX cue of src/story/story.json inside a segment's
source window is re-timed (and its duration scaled) into the new timeline; the
top (EASY NOW) panel sits a little quieter so both halves stay readable.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
target = ROOT / "src" / "story" / (sys.argv[1] if len(sys.argv) > 1 else "same.json")
cfg = json.loads(target.read_text())
src = json.loads((ROOT / "src" / "story" / "story.json").read_text())

PANEL_GAIN = {"top": 0.55, "bottom": 1.0}
out = []
for seg in cfg["segments"]:
    rate = (seg["t1"] - seg["t0"]) / (seg["s1"] - seg["s0"])
    for c in src["sfx"]:
        if not (seg["s0"] <= c["t"] < seg["s1"]):
            continue
        n = dict(c)
        n["t"] = round(seg["t0"] + (c["t"] - seg["s0"]) * rate, 3)
        if "dur" in n:
            n["dur"] = round(min(n["dur"] * rate, seg["t1"] - n["t"]), 3)
        if "rate" in n:
            n["rate"] = round(n["rate"] * rate, 3)
        n["vol"] = round(c.get("vol", 0.6) * PANEL_GAIN[seg["panel"]], 3)
        out.append(n)
out += cfg.get("sfx_extra", [])
out.sort(key=lambda c: c["t"])
cfg["sfx"] = out
target.write_text(json.dumps(cfg, indent=1))
print(f"{len(out)} cues -> {target.name}")
