#!/usr/bin/env python3
"""
randare.py - pasul final pentru clipurile video, pe calculatorul tau, cu ffmpeg:
vocea dublata peste video-ul original + subtitrari arse + watermark, intr-o
singura trecere. Foloseste placa NVIDIA (NVENC) daca se poate, altfel procesorul.
"""
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGO_FILE = HERE / "watermark.png"
_cache = {}


class RenderError(Exception):
    pass


_tls = threading.local()      # per thread: do the helper processes it starts run at low priority?


def _no_window():
    if not sys.platform.startswith("win"):
        return {}
    # 0x4000 = BELOW_NORMAL_PRIORITY_CLASS (e.g. the CapCut prep while a game runs: the game gets the CPU first)
    return {"creationflags": 0x08000000 | (0x00004000 if getattr(_tls, "low", False) else 0)}


class low_priority:
    """with randare.low_priority(on): the ffmpeg / ffprobe this THREAD starts inside the block run at low priority."""

    def __init__(self, on=True):
        self.on = bool(on)

    def __enter__(self):
        self.prev = getattr(_tls, "low", False)
        _tls.low = self.on or self.prev
        return self

    def __exit__(self, *exc):
        _tls.low = self.prev
        return False


# ---------------------------------------------------------------- ffmpeg
def find_ffmpeg():
    """(ffmpeg, ffprobe) paths, or (None, None)."""
    if "ff" in _cache:
        return _cache["ff"]
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    # an ffmpeg next to the app wins (the GPU worker downloads a recent one there:
    # the pod's own ffmpeg is 4.x and misses options this app uses)
    exe = ".exe" if sys.platform.startswith("win") else ""
    own = HERE / "ffmpeg" / "bin" / ("ffmpeg" + exe)
    if own.is_file() and (HERE / "ffmpeg" / "bin" / ("ffprobe" + exe)).is_file():
        ff, fp = str(own), str(HERE / "ffmpeg" / "bin" / ("ffprobe" + exe))
    if not ff and sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", "")
        cands = [os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe")]
        cands += glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "**", "bin",
                                        "ffmpeg.exe"), recursive=True)
        cands += [str(HERE / "ffmpeg" / "bin" / "ffmpeg.exe"), r"C:\ffmpeg\bin\ffmpeg.exe"]
        for c in cands:
            if c and os.path.isfile(c):
                ff = c
                probe = os.path.join(os.path.dirname(c), "ffprobe.exe")
                fp = probe if os.path.isfile(probe) else fp
                break
    _cache["ff"] = (ff, fp)
    return ff, fp


def ffmpeg_ok() -> bool:
    return bool(find_ffmpeg()[0])


RUN_MAX_S = 2 * 3600      # a short ffmpeg / ffprobe step that hangs is stopped after this (never forever)


def _run(args, timeout=None, cwd=None):
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout or RUN_MAX_S, cwd=cwd, **_no_window())
    except subprocess.TimeoutExpired:
        raise RenderError(f"{Path(str(args[0])).stem} s-a blocat (oprit dupa {int((timeout or RUN_MAX_S) // 60)} min)")
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


FF_STALL_S = 10 * 60      # ffmpeg writes its progress every half second: this long without a line = stuck
AI_STALL_S = 20 * 60      # sterge_ai.py writes a line per batch of frames (slow cards included)


class _Guard:
    """Watches a helper process from the side: kills it when `cancel` is set (without waiting for its
    next line) or when it stops writing for `stall` seconds. why: None, "anulat" or the stall text."""

    def __init__(self, procs, cancel=None, stall=None, kill=None):
        self.procs, self.cancel, self.stall = list(procs), cancel, stall
        self.kill = kill or _kill
        self.last, self.why = time.time(), None
        self._end = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def beat(self):
        self.last = time.time()

    def _run(self):
        while not self._end.wait(1.0):
            if self.cancel is not None and self.cancel.is_set():
                self.why = "anulat"
            elif self.stall and time.time() - self.last > self.stall:
                self.why = f"s-a blocat (nicio iesire de {int(self.stall // 60)} minute)"
            if self.why:
                for p in self.procs:
                    self.kill(p)
                return

    def done(self):
        self._end.set()


def nvenc_ok() -> bool:
    if "nvenc" not in _cache:
        ff = find_ffmpeg()[0]
        ok = False
        if ff:
            try:
                code, _, _ = _run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                   "color=s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"], timeout=60)
                ok = code == 0
            except Exception:
                ok = False
        _cache["nvenc"] = ok
    return _cache["nvenc"]


def probe(video: Path) -> dict:
    ff, fp = find_ffmpeg()
    if not fp:
        raise RenderError("lipseste ffprobe (vine cu ffmpeg)")
    code, out, err = _run([fp, "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                           str(video)], timeout=120)
    if code != 0:
        raise RenderError(f"nu pot citi clipul video: {err.strip()[-200:]}")
    data = json.loads(out or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise RenderError("fisierul nu are imagine video")
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    rot = 0
    try:
        rot = int((v.get("tags") or {}).get("rotate") or 0)
    except ValueError:
        pass
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = int(sd["rotation"])
            except (TypeError, ValueError):
                pass
    if abs(rot) in (90, 270):
        w, h = h, w
    dur = float((data.get("format") or {}).get("duration") or v.get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    return {"w": w, "h": h, "dur": dur, "audio": has_audio}


def extract_audio(video: Path, out: Path):
    ff = find_ffmpeg()[0]
    code, _, err = _run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video), "-vn",
                         "-map", "0:a:0", "-c:a", "aac", "-b:a", "192k", str(out)], timeout=1800)
    if code != 0 or not out.exists():
        raise RenderError(f"nu pot scoate sunetul din video: {err.strip()[-200:]}")


# ---------------------------------------------------------------- subtitles (ASS)
COLORS = {"verde": "3DDC84", "galben": "FFE700", "alb": "FFFFFF", "roz": "FF6FB5", "albastru": "4FB3FF"}


def _ass_color(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _t(x: float) -> str:
    cs = max(0, int(round(x * 100)))
    h, r = divmod(cs, 360000)
    m, r = divmod(r, 6000)
    s, c = divmod(r, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _esc(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).replace("\\", "\\\\").replace("{", "(").replace("}", ")").strip()


def _karaoke(words, a, b, karaoke=True):
    """words: strings, or lists of pieces (one word split on several lines: the pieces are joined with \\N
    and each piece gets its share of the word's time)."""
    items = [w if isinstance(w, list) else [w] for w in words]
    if not karaoke:
        return " ".join("\\N".join(_esc(p) for p in it) for it in items)
    total = sum(len("".join(it)) + 1 for it in items) or 1
    dur = max(0.05, b - a) * 100
    out = []
    for it in items:
        d = dur * (len("".join(it)) + 1) / total
        letters = sum(len(p) for p in it) or 1
        out.append("\\N".join("{\\kf%d}%s" % (max(5, round(d * len(p) / letters)), _esc(p)) for p in it))
    return " ".join(out)


# Subtitle chunks must stay inside the video: words are grouped by their real width, not by a fixed count.
# Advance widths (1/1000 em) of Arial (Liberation Sans / Arimo have the same metrics).
_ARIAL_W = dict(zip('0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~ ăâîșțĂÂÎȘȚşţŞŢ„”“…–—’‘«»é',
    (556,556,556,556,556,556,556,556,556,556,556,556,500,556,556,278,556,556,222,222,500,222,833,556,556,556,556,333,500,278,556,500,722,500,500,500,667,667,722,722,667,611,778,722,278,500,667,556,833,722,778,667,778,722,667,611,722,667,944,667,667,611,278,355,556,556,889,667,191,333,333,389,584,278,333,278,278,278,278,584,584,584,556,1015,278,278,278,469,556,333,334,260,334,584,278,556,556,278,500,278,667,667,278,667,611,500,278,667,611,333,333,333,1000,556,1000,222,222,556,556,556)))
_ARIAL_BW = dict(zip('0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~ ăâîșțĂÂÎȘȚşţŞŢ„”“…–—’‘«»é',
    (556,556,556,556,556,556,556,556,556,556,556,611,556,611,556,333,611,611,278,278,556,278,889,611,611,611,611,389,556,333,611,556,778,556,556,500,722,722,722,722,667,611,778,722,278,556,722,611,833,722,778,667,778,722,667,611,722,667,944,667,667,611,333,474,556,556,889,722,238,333,333,389,584,278,333,278,278,333,333,584,584,584,611,975,333,278,333,584,556,333,389,280,389,584,278,556,556,278,556,333,722,722,278,667,611,556,333,667,611,500,500,500,1000,556,1000,278,278,556,556,556)))


def _text_em(text: str, bold: bool) -> float:
    tab = _ARIAL_BW if bold else _ARIAL_W
    return sum(tab.get(ch, 722 if ch.isupper() else 600) for ch in text) / 1000.0


def _font_fudge(bold: bool) -> float:
    """How much wider than Arial the font libass really draws here is. Windows/macOS have Arial; a Linux
    GPU without Arial or a metric twin (Liberation Sans, Arimo) falls back to a wider font (DejaVu Sans)."""
    key = "font_fudge_b" if bold else "font_fudge"
    if key not in _cache:
        f = 1.0
        if sys.platform.startswith("linux"):
            try:
                fam = subprocess.run(["fc-match", "-f", "%{family}", "Arial:bold" if bold else "Arial"],
                                     capture_output=True, timeout=10).stdout.decode("utf-8", "replace").lower()
            except Exception:  # noqa: BLE001 - no fontconfig tools: assume the wide fallback
                fam = ""
            if not any(x in fam for x in ("arial", "liberation sans", "arimo")):
                f = 1.24 if bold else 1.18
        _cache[key] = f
    return _cache[key]


_VOWELS = set("aeiouăâîAEIOUĂÂÎyY")


def _cut_score(word: str, j: int) -> int:
    """0 when cutting before word[j] falls between two syllables (Romanian rules of thumb: e-le, lec-tro),
    1 otherwise."""
    a, b = word[j - 1], word[j]
    if not (a.isalpha() and b.isalpha()):
        return 1
    if a in _VOWELS and b not in _VOWELS and j + 1 < len(word) and word[j + 1] in _VOWELS:
        return 0                                     # V-CV
    if a not in _VOWELS and b not in _VOWELS and j >= 2 and word[j - 2] in _VOWELS:
        return 0                                     # VC-C
    return 1


def _split_word(word: str, fits) -> list:
    """A word wider than the whole line, split on several lines at the SAME font size: pieces of about equal
    length, cut between syllables where possible, every piece but the last ending in a hyphen."""
    n = len(word)
    for k in range(2, max(2, n // 2) + 1):          # the fewest lines that work
        cuts, start = [], 0
        for part in range(1, k):
            ideal = round(n * part / k)
            best = None
            for dist in range(0, 7):
                for j in {ideal - dist, ideal + dist}:
                    if j - start < 2 or n - j < 2 or not fits(word[start:j] + "-"):
                        continue
                    cand = (_cut_score(word, j), dist, j)
                    if best is None or cand < best:
                        best = cand
            if best is None:
                break
            cuts.append(best[2])
            start = best[2]
        else:
            if fits(word[start:]):
                edges = [0] + cuts + [n]
                return [word[x:y] + ("-" if y < n else "") for x, y in zip(edges, edges[1:])]
    return [word]                                    # (can't be split usefully: left as it is)


def sub_chunks(words, per: int, size: int, bold: bool, max_w: float, outline: float = 0.0, full_w: float = 0.0):
    """Groups of at most `per` words that fit max_w pixels on one line: [(words, items, wide)]. The font size
    never changes. A single word wider than the line gets the whole width of the video (wide: narrow side
    margins, up to full_w); one wider still is split on two (or more) lines with a hyphen (items holds the
    pieces as a list). per = 0: the whole line at once (libass wraps it; only too-wide words are split)."""
    fudge = _font_fudge(bold)
    full_w = max(max_w, full_w or max_w)

    def width(text):
        return _text_em(text, bold) * size * fudge * 1.03 + 2 * outline

    def fits(text):
        return width(text) <= max_w

    if not per:
        items = [_split_word(wd, fits) if not fits(wd) else wd for wd in words]
        return [(list(words), items, False)]
    out, cur = [], []
    for wd in words:
        if cur and (len(cur) >= per or not fits(" ".join(cur + [wd]))):
            out.append(cur)
            cur = []
        cur.append(wd)
    if cur:
        out.append(cur)
    res = []
    for g in out:
        text = " ".join(g)
        if fits(text):
            res.append((g, list(g), False))
        elif width(text) <= full_w:
            res.append((g, list(g), True))           # (only a lone word gets here)
        else:
            res.append((g, [_split_word(text, fits)], False))
    return res


def cover_zone(c: dict, w: int, h: int):
    """Pixel box (x, y, zw, zh) over the original burned-in subtitles, or None."""
    if not c.get("sterge_sub_orig"):
        return None
    z = c.get("sterge_zona") or {}
    try:
        fy, fh = float(z.get("y")), float(z.get("h"))
    except (TypeError, ValueError):
        return None
    # always the full width of the clip; only the height of the band changes
    x, zw = 0, w // 2 * 2
    y, zh = int(fy * h) // 2 * 2, int(fh * h) // 2 * 2
    zh = min(zh, (h - y) // 2 * 2)
    if zw < 8 or zh < 8:
        return None
    return x, y, zw, zh


CUVANT_FONT = "Poppins"     # "Un cuvânt" style (bold); the .ttf rides along in fonturi/ for the rented GPUs
FONT_DIR = HERE / "fonturi"


def fonts_for(work) -> str:
    """Put the subtitle fonts next to subs.ass (a machine without them installed, e.g. a rented GPU, uses
    them from there) -> the ass filter's fontsdir option, or ""."""
    try:
        fs = list(FONT_DIR.glob("*.ttf"))
        if not fs:
            return ""
        d = Path(work) / "fonturi"
        d.mkdir(exist_ok=True)
        for f in fs:
            if not (d / f.name).exists():
                shutil.copy2(f, d / f.name)
        return ":fontsdir=fonturi"
    except OSError:
        return ""


def resolve_style(c: dict, w: int, h: int) -> str:
    st = c.get("subtitrari", "auto")
    if st == "auto":
        return "tiktok" if h > w else "clasic"
    return st


def build_ass(segments, w, h, c, duration=0.0) -> str:
    """segments: [{"start","end","text"}]. Returns the ASS script (subtitles + text watermark)."""
    style = resolve_style(c, w, h)
    vertical = h > w
    base = min(w, h)
    color = COLORS.get(c.get("sub_culoare", "verde"), "3DDC84")
    pos = c.get("sub_pozitie", "auto")
    if pos == "auto":
        pos = "mijloc" if style == "tiktok" else "jos"
    align = {"jos": 2, "mijloc": 5, "sus": 8}.get(pos, 2)
    margin_v = int(h * (0.08 if pos != "mijloc" else 0))
    if style == "cuvant" and c.get("sub_pozitie", "auto") == "auto":
        margin_v = int(h * 0.22)          # a bit under the middle, like the TikTok edits it copies
    # watermark centred at the bottom/top: move the subtitles out of its way
    wm_where = c.get("watermark_pozitie", "dreapta-sus") if c.get("watermark_activ") else ""
    if wm_where in ("mijloc-jos", "mijloc-sus") and align == (2 if wm_where == "mijloc-jos" else 8):
        wm_size0 = int(base * 0.045 * (float(c.get("watermark_marime", 1.0)) or 1.0))
        block = int(h * 0.04) + int(base * 0.02)
        if c.get("watermark_logo_activ") and LOGO_FILE.exists():
            block += logo_box(w, h, c)[1] + int(base * 0.012)
        if str(c.get("watermark_text") or "").strip():
            block += int(wm_size0 * 1.3)
        margin_v = max(margin_v, block)
    zone = cover_zone(c, w, h) if c.get("sub_in_zona", True) else None
    k = float(c.get("sub_marime", 1.0) or 1.0)       # fixed size the user picked; never auto-scaled
    measure = 1.0
    if style == "cuvant":
        # one word at a time, CAPITALS, white Poppins Bold with a thick black outline and a soft shadow
        size = int(base * (0.21 if vertical else 0.14) * k)   # (Poppins draws smaller than Arial at one size)
        bold, outline, margin_h = True, max(4, size // 11), int(w * 0.06)
        measure = 0.45                    # Poppins at this size, measured on a render (vs the Arial estimate)
        sub_style = (f"Style: Sub,{CUVANT_FONT},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,"
                     f"-1,0,0,0,100,100,1,0,1,{outline},{max(2, size // 22)},{align},"
                     f"{margin_h},{margin_h},{margin_v},1")
    elif style == "tiktok":
        size = int(base * (0.075 if vertical else 0.085) * k)
        bold, outline, margin_h = True, max(3, size // 12), int(w * 0.07)
        sub_style = (f"Style: Sub,Arial,{size},{_ass_color(color)},&H00FFFFFF,&H00000000,&H64000000,"
                     f"-1,0,0,0,100,100,0,0,1,{outline},{max(1, size // 30)},{align},"
                     f"{margin_h},{margin_h},{margin_v},1")
    else:
        size = int((h * 0.058 if not vertical else w * 0.06) * k)
        prim = _ass_color(color) if style == "clasic" else "&H00FFFFFF"
        bold, outline, margin_h = False, max(2, size // 20), int(w * 0.05)
        sub_style = (f"Style: Sub,Arial,{size},{prim},&H00FFFFFF,&H00101010,&H7F000000,"
                     f"0,0,0,0,100,100,0,0,1,{outline},1,{align},"
                     f"{margin_h},{margin_h},{margin_v},1")
    max_w = w - 2 * margin_h                          # a chunk never gets wider than this
    edge = max(4, int(w * 0.02))                      # a lone word too long for that may use the whole width
    full_w = w - 2 * edge
    wm_size = int(base * 0.045 * (float(c.get("watermark_marime", 1.0)) or 1.0))
    wm_alpha = int(255 * (1 - float(c.get("watermark_opacitate", 0.6))))
    wm_style = (f"Style: WM,Arial,{wm_size},{_ass_color('FFFFFF', wm_alpha)},&H00FFFFFF,"
                f"{_ass_color('000000', min(255, wm_alpha + 80))},&H00000000,-1,0,0,0,100,100,0,0,1,"
                f"{max(1, wm_size // 16)},0,7,0,0,0,1")
    out = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}",
        "WrapStyle: 0",          # chunks are measured to fit one line; if a font is wider still, wrap, never cut
        "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        sub_style, wm_style, "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if style != "nu":
        karaoke = style in ("clasic", "tiktok") and c.get("sub_culoare", "verde") != "alb"
        per = int(c.get("sub_cuvinte", 5) or 0)          # 0 = the whole line
        if style == "cuvant":
            per = 1
        place = ""
        if zone:
            zx, zy, zw, zh = zone
            place = f"\\an5\\pos({zx + zw // 2},{zy + zh // 2})"
        for s in segments:
            text = str(s.get("text") or "").strip()
            a, b = float(s["start"]), float(s["end"])
            if not text or b - a < 0.05:
                continue
            if style == "cuvant":
                text = text.upper()
            words = text.split()
            if style == "cuvant":                # no commas / full stops on a lone word (? and ! stay)
                words = [x for x in (re.sub(r"[,.;:…\"„”«»]+$", "", re.sub(r"^[\"„”«»(]+", "", wd)) for wd in words) if x]
            # at most `per` words, only as many as fit on one line (per = 0: the whole line, wrapped);
            # always at the size the user picked: a word too long is split on two lines, never shrunk
            if style == "cuvant":
                # one word each; a word too wide for the screen gets smaller letters (only it), never cut in two
                groups = [([wd], [wd], False) for wd in words]
            else:
                groups = sub_chunks(words, per if per > 0 else 0, int(size * measure), bold, max_w, outline, full_w)
            total = sum(len(" ".join(g)) + 1 for g, _, _ in groups)
            t = a
            for gi, (g, items, wide) in enumerate(groups):
                line = " ".join(g)
                cb = b if gi == len(groups) - 1 else t + (b - a) * (len(line) + 1) / total
                tags = place + ("\\fad(60,0)" if style == "tiktok" else "")
                if style == "cuvant":           # each word pops in (a quick zoom from 85%)
                    est = _text_em(line, bold) * size * _font_fudge(bold) * 1.03 * measure + 2 * outline
                    fs = f"\\fs{int(size * max_w / est)}" if est > max_w else ""
                    tags = place + fs + "\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)"
                ml = edge if wide else 0         # (0 = the style's margins)
                out.append(f"Dialogue: 0,{_t(t)},{_t(cb)},Sub,,{ml},{ml},0,," + (f"{{{tags}}}" if tags else "")
                           + _karaoke(items, t, cb, karaoke))
                t = cb
    # text watermark (drawn by libass: no font paths / escaping on Windows)
    wm_text = _esc(c.get("watermark_text", ""))
    if c.get("watermark_activ") and wm_text and duration > 0:
        mx, my = int(w * 0.04), int(h * 0.04)
        corners = {"stanga-sus": (7, mx, my), "dreapta-sus": (9, w - mx, my),
                   "stanga-jos": (1, mx, wm_bottom(w, h, c, h - my - int(h * 0.10))),
                   "dreapta-jos": (3, w - mx, wm_bottom(w, h, c, h - my - int(h * 0.10))),
                   "mijloc-sus": (8, w // 2, my), "mijloc-jos": (2, w // 2, wm_bottom(w, h, c, h - my))}
        where = c.get("watermark_pozitie", "dreapta-sus")
        if where == "se-muta":
            order = ["stanga-sus", "dreapta-sus", "dreapta-jos", "stanga-jos"]
            t, i = 0.0, 0
            while t < duration:
                an, x, y = corners[order[i % 4]]
                out.append(f"Dialogue: 1,{_t(t)},{_t(min(duration, t + 4))},WM,,0,0,0,,"
                           f"{{\\an{an}\\pos({x},{y})}}{wm_text}")
                t += 4
                i += 1
        else:
            an, x, y = corners.get(where, corners["dreapta-sus"])
            if c.get("watermark_logo_activ") and LOGO_FILE.exists():
                off = logo_box(w, h, c)[1] + int(base * 0.012)          # sit under/over the logo
                y += off if an in (7, 8, 9) else -off
            out.append(f"Dialogue: 1,{_t(0)},{_t(duration)},WM,,0,0,0,,{{\\an{an}\\pos({x},{y})}}{wm_text}")
    return "\n".join(out) + "\n"


def logo_box(w, h, c):
    """(width, height) the logo gets on a w x h video."""
    lw = max(32, int(min(w, h) * 0.24 * float(c.get("watermark_marime", 1.0) or 1.0)))
    pw, ph = 4, 3
    try:
        head = LOGO_FILE.read_bytes()[:24]
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            pw, ph = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    except OSError:
        pass
    return lw, int(lw * ph / max(1, pw))


def wm_bottom(w, h, c, default_bottom):
    """Bottom edge of a bottom-placed watermark block; moved above the covered
    subtitle zone when they would overlap."""
    zone = cover_zone(c, w, h)
    if not zone:
        return default_bottom
    base = min(w, h)
    block = 0
    if c.get("watermark_logo_activ") and LOGO_FILE.exists():
        block += logo_box(w, h, c)[1] + int(base * 0.012)
    if str(c.get("watermark_text") or "").strip():
        block += int(base * 0.045 * float(c.get("watermark_marime", 1.0) or 1.0) * 1.3)
    zy, zh = zone[1], zone[3]
    if default_bottom - block < zy + zh and default_bottom > zy:
        return max(block, zy - int(base * 0.015))
    return default_bottom


def write_srt(segments, path: Path):
    def ts(x):
        ms = max(0, int(round(float(x) * 1000)))
        h, r = divmod(ms, 3600000)
        m, r = divmod(r, 60000)
        s, r = divmod(r, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{r:03d}"
    n, lines = 0, []
    for s in segments:
        text = re.sub(r"\s+", " ", str(s.get("text") or "")).strip()
        if not text:
            continue
        n += 1
        lines += [str(n), f"{ts(s['start'])} --> {ts(s['end'])}", text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------- render
SHADER = HERE / "modele" / "fsrcnnx_x2.glsl"
SHADER_NAME = "fsrcnnx.glsl"          # copied into ffmpeg's working folder (no Windows path escaping)


def placebo_ok() -> bool:
    """Can this ffmpeg run libplacebo on the video card (Vulkan) with the FSRCNNX shader?"""
    if "placebo" not in _cache:
        ok = False
        ff = find_ffmpeg()[0]
        if ff and SHADER.exists():
            tmp = Path(tempfile.mkdtemp(prefix="placebo-"))
            try:
                shutil.copy2(SHADER, tmp / SHADER_NAME)
                code, _, _ = _run([ff, "-hide_banner", "-loglevel", "error", "-init_hw_device", "vulkan",
                                   "-f", "lavfi", "-i", "testsrc2=s=128x96:d=0.2",
                                   "-vf", f"format=yuv420p,libplacebo=w=256:h=192:upscaler=ewa_lanczossharp:"
                                          f"custom_shader_path={SHADER_NAME}:format=yuv420p",
                                   "-f", "null", "-"], timeout=90, cwd=str(tmp))
                ok = code == 0
            except Exception:
                ok = False
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        _cache["placebo"] = ok
    return _cache["placebo"]


def detect_bars(video: Path, dur: float, w: int, h: int):
    """Picture area without the black bars (x, y, cw, ch), from ffmpeg cropdetect on ~10 moments.
    The union of all moments is kept, so a dark scene does not cut real picture."""
    key = ("bars", str(video), w, h)
    if key in _cache:
        return _cache[key]
    ff = find_ffmpeg()[0]
    box = None
    n = 10
    for i in range(n):
        at = max(0.0, dur * (i + 0.5) / n) if dur > 0 else 0.0
        try:
            code, _, err = _run([ff, "-hide_banner", "-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "6",
                                 "-vf", "cropdetect=limit=24:round=2:reset=0", "-f", "null", "-"], timeout=60)
        except Exception:
            continue
        m = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", err)
        if not m:
            continue
        cw, ch, cx, cy = map(int, m[-1])
        if cw < w * 0.3 or ch < h * 0.2:      # an almost black moment (fade): says nothing
            continue
        b = (cx, cy, cx + cw, cy + ch)
        box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
    if box is None:
        res = (0, 0, w, h)
    else:
        x0, y0 = box[0] // 2 * 2, box[1] // 2 * 2
        x1, y1 = min(w, (box[2] + 1) // 2 * 2), min(h, (box[3] + 1) // 2 * 2)
        # bars thinner than 1.5% are noise / compression edges: keep the full frame on that side
        if x0 < w * 0.015 and w - x1 < w * 0.015:
            x0, x1 = 0, w
        if y0 < h * 0.015 and h - y1 < h * 0.015:
            y0, y1 = 0, h
        res = (x0, y0, x1 - x0, y1 - y0)
    _cache[key] = res
    return res


def map_zone(c: dict, info: dict) -> dict:
    """The subtitle band is drawn on the original frame; after the 9:16 layout the picture
    moved (bars cut, scaled, centred), so the band is moved along with it."""
    lay = info.get("lay")
    z = c.get("sterge_zona")
    if not lay or not z:
        return c
    try:
        fy, fh = float(z["y"]), float(z["h"])
    except (TypeError, ValueError, KeyError):
        return c
    H0 = lay["src_h"]
    cy, ch, s, oy, H = lay["cy"], lay["ch"], lay["s"], lay["oy"], info["h"]
    top = max(fy * H0, cy)
    bot = min((fy + fh) * H0, cy + ch)
    if bot <= top:                     # the band was entirely on a black bar
        top, bot = cy + ch * 0.75, cy + ch * 0.9
    ny, nb = oy + (top - cy) * s, oy + (bot - cy) * s
    # band touching the cut edge of the picture: go a little over it, so the blur also covers
    # the last rows of the letters (subtitles often sit half on the black bar)
    pad = H * 0.02
    if (fy + fh) * H0 >= cy + ch - 2:
        nb = min(H, nb + pad)
    if fy * H0 <= cy + 2:
        ny = max(0, ny - pad)
    return dict(c, sterge_zona=dict(z, y=round(ny / H, 5), h=round((nb - ny) / H, 5)))


def with_upscale(info: dict, c: dict, video: Path = None) -> dict:
    """Layout of the export: optional 9:16 (black bars cut, the same clip blurred behind),
    optional 1080p (FSRCNNX or lanczos), then sharpening. Returns info with the new w/h,
    the filter chain in 'pre' and the geometry in 'lay' (for map_zone)."""
    res = None
    if c.get("format_916") and video is not None and info["w"] > 0 and info["h"] > 0:
        res = _layout_916(info, c, video)
    res = res or _plain_upscale(info, c)
    if c.get("_up_done"):                  # Real-ESRGAN already ran on the video card
        res["up"] = c["_up_done"] + (" · 9:16" if res.get("lay") else "")
    return res


def _upscale_filter(tw, th, c):
    if c.get("upscale_metoda", "fsrcnnx") != "lanczos" and not getattr(_tls, "low", False) and placebo_ok():
        return (f"format=yuv420p,libplacebo=w={tw}:h={th}:upscaler=ewa_lanczossharp:"
                f"custom_shader_path={SHADER_NAME}:format=yuv420p"), "FSRCNNX", True
    return f"scale={tw}:{th}:flags=lanczos", "lanczos", False


CANVAS_MAX_W = 1080     # the 9:16 canvas is never wider than 1080 (1080x1920)


def _canvas_w(w, c):
    """Width of the 9:16 canvas: as wide as the original frame (1080 with 1080p export), at most 1080."""
    W = w
    if c.get("iesire_1080") and min(w, round(w * 16 / 9)) < 1080:
        W = 1080
    return min(W, CANVAS_MAX_W) // 2 * 2


def _layout_916(info, c, video):
    w, h = info["w"], info["h"]
    if c.get("_bars_done"):
        # the video card already cut the bars (or found none) on the original: this is the
        # Real-ESRGAN output, nothing left to look for
        cx, cy, cw, ch = 0, 0, w, h
    else:
        cx, cy, cw, ch = detect_bars(video, info.get("dur") or 0, w, h)
    # canvas: 9:16, as wide as the original frame (1080 wide with 1080p export), never over 1080
    W = _canvas_w(w, c)
    H = int(round(W * 16 / 9 / 2)) * 2
    if (cx, cy, cw, ch) == (0, 0, w, h) and abs(w / h - 9 / 16) < 0.01:
        return None                              # already 9:16 without bars: nothing to lay out
    # the picture keeps the size it had in the original frame (only scaled with the canvas)
    s = W / w
    if ch * s > H:
        s = H / ch
    fw, fh = int(round(cw * s / 2)) * 2, int(round(ch * s / 2)) * 2
    ox, oy = (W - fw) // 2 // 2 * 2, (H - fh) // 2 // 2 * 2
    amt = max(0.0, min(1.5, float(c.get("upscale_claritate", 0.5) or 0)))
    if c.get("_up_done"):
        amt = 0.0                 # Real-ESRGAN output is sharp already: unsharp would only add halos
    if fw > cw * 1.05:
        up_f, up, vk = _upscale_filter(fw, fh, c)
    else:
        up_f, up, vk = f"scale={fw}:{fh}:flags=lanczos", None, False
    sharp = f",unsharp=5:5:{amt:.2f}:5:5:0" if amt > 0 else ""
    # background: the same clip, zoomed until it covers the canvas, blurred and a bit darker
    # (blurred at 1/4 size: much faster and looks the same)
    bw, bh = max(2, W // 4 // 2 * 2), max(2, H // 4 // 2 * 2)
    pre = (f"crop={cw}:{ch}:{cx}:{cy},split[lz_a][lz_b];"
           f"[lz_a]{up_f}{sharp}[lz_fg];"
           f"[lz_b]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
           f"gblur=sigma=12:steps=2,eq=brightness=-0.06:saturation=1.1,scale={W}:{H},format=yuv420p[lz_bg];"
           f"[lz_bg][lz_fg]overlay={ox}:{oy}")
    lay = {"src_h": h, "cy": cy, "ch": ch, "s": s, "oy": oy, "bars": (cx, cy, cw, ch)}
    return dict(info, w=W, h=H, pre=pre, vk=vk, up=(up or "") + (" · 9:16" if up else "9:16"), lay=lay)


def _plain_upscale(info: dict, c: dict) -> dict:
    """Export at 1080p: the short side goes to 1080 px, then a little sharpening.
    Mărire: FSRCNNX (small neural shader on the video card, via libplacebo) or lanczos.
    A clip that is already 1080p only gets the sharpening. Everything after it
    (blur band, subtitles, logo) is laid out on the new size."""
    w, h = info["w"], info["h"]
    amt = max(0.0, min(1.5, float(c.get("upscale_claritate", 0.5) or 0)))
    if c.get("_up_done"):
        amt = 0.0                 # already sharpened by Real-ESRGAN
    sharp = f"unsharp=5:5:{amt:.2f}:5:5:0" if amt > 0 else ""
    if not c.get("iesire_1080") or min(w, h) <= 0:
        return dict(info, pre="", vk=False, up=None)
    if min(w, h) >= 1080:
        return dict(info, pre=sharp, vk=False, up=None)
    k = 1080 / min(w, h)
    nw, nh = int(round(w * k / 2)) * 2, int(round(h * k / 2)) * 2
    # (low priority, e.g. during a game: no video card at all -> lanczos instead of FSRCNNX)
    if c.get("upscale_metoda", "fsrcnnx") != "lanczos" and not getattr(_tls, "low", False) and placebo_ok():
        pre = (f"format=yuv420p,libplacebo=w={nw}:h={nh}:upscaler=ewa_lanczossharp:"
               f"custom_shader_path={SHADER_NAME}:format=yuv420p")
        up, vk = "FSRCNNX", True
    else:
        pre, up, vk = f"scale={nw}:{nh}:flags=lanczos", "lanczos", False
    if sharp:
        pre += "," + sharp
    return dict(info, w=nw, h=nh, pre=pre, vk=vk, up=up)


def hw_args(info: dict, work) -> list:
    """Extra ffmpeg args (before the inputs) + the shader next to ffmpeg, when FSRCNNX is used."""
    if not info.get("vk"):
        return []
    shutil.copy2(SHADER, Path(work) / SHADER_NAME)
    return ["-init_hw_device", "vulkan"]


def _filters(info, c, has_logo):
    """filter_complex for: [0]=video (+ subs.ass) [2]=logo png -> [v]"""
    w, h = info["w"], info["h"]
    head, src = (f"[0:v]{info['pre']}[up];", "[up]") if info.get("pre") else ("", "[0:v]")
    zone = cover_zone(c, w, h)
    mode = c.get("sterge_mod", "blur")
    if zone and mode in ("ai", "ai_gata", "vmake"):
        # AI: the picture was already cleaned before (ai_gata); in a preview (ai, vmake: cleaned on the
        # Vmake site for the real clip) only outline the band
        x, y, zw, zh = zone
        box = (f"drawbox=x={x}:y={y}:w={zw}:h={zh}:color=0x39d98a@0.9:t=3,"
               if mode in ("ai", "vmake") else "")
        chain = head + f"{src}{box}ass=subs.ass{c.get('_fontsdir', '')}[s]"
    elif zone:
        x, y, zw, zh = zone
        sig = max(10, min(60, zh // 2))
        band = (f",drawbox=x={x}:y={y}:w={zw}:h={zh}:color=black@0.75:t=fill"
                if mode == "banda" else "")
        chain = head + (f"{src}split[c0][c1];[c1]crop={zw}:{zh}:{x}:{y},gblur=sigma={sig}:steps=3,"
                        f"eq=brightness=-0.10[cb];[c0][cb]overlay={x}:{y}{band}[c];")
        chain += f"[c]ass=subs.ass{c.get('_fontsdir', '')}[s]"
    else:
        chain = head + f"{src}ass=subs.ass{c.get('_fontsdir', '')}[s]"
    last = "[s]"
    if has_logo:
        lw = logo_box(w, h, c)[0]
        op = max(0.05, min(1.0, float(c.get("watermark_opacitate", 0.6))))
        mx, my = int(w * 0.04), int(h * 0.04)
        lh = logo_box(w, h, c)[1]
        corner_b = wm_bottom(w, h, c, h - my - int(h * 0.10))
        pos = {"stanga-sus": (f"{mx}", f"{my}"), "dreapta-sus": (f"W-w-{mx}", f"{my}"),
               "stanga-jos": (f"{mx}", f"{corner_b - lh}"),
               "dreapta-jos": (f"W-w-{mx}", f"{corner_b - lh}"),
               "mijloc-sus": ("(W-w)/2", f"{my}"), "mijloc-jos": ("(W-w)/2", f"{wm_bottom(w, h, c, h - my) - lh}")}
        where = c.get("watermark_pozitie", "dreapta-sus")
        if where == "se-muta":
            # changes corner every 4 s, clockwise
            x = f"if(lt(mod(t,16),4),{mx},if(lt(mod(t,16),12),W-w-{mx},{mx}))"
            y = f"if(lt(mod(t,16),8),{my},H-h-{my}-{int(h*0.10)})"
        else:
            x, y = pos.get(where, pos["dreapta-sus"])
        chain += (f";[2:v]format=rgba,scale={lw}:-1,colorchannelmixer=aa={op:.2f}[l]"
                  f";{last}[l]overlay=x='{x}':y='{y}':eval=frame[v]")
    else:
        chain += ";[s]null[v]"
    return chain


# NVENC through the pod image's old ffmpeg (4.x) when the new one cannot talk to the driver:
# lucrator.py writes its path here; legacy preset names, which every ffmpeg version knows
ALT_NVENC_FILE = HERE / "ffmpeg_nvenc.txt"
ALT_VENC = ["-c:v", "h264_nvenc", "-preset", "medium", "-rc", "vbr", "-cq", "21", "-b:v", "0"]


def alt_nvenc():
    """Path of an older ffmpeg whose NVENC works (only when the main ffmpeg's does not), or None."""
    if "alt_nvenc" not in _cache:
        alt = None
        try:
            cand = ALT_NVENC_FILE.read_text(encoding="utf-8").strip() if ALT_NVENC_FILE.is_file() else ""
            if cand and os.path.isfile(cand) and cand != find_ffmpeg()[0]:
                code, _, _ = _run([cand, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                   "color=s=256x256:d=0.2", "-pix_fmt", "yuv420p", *ALT_VENC,
                                   "-f", "null", "-"], timeout=60)
                alt = cand if code == 0 else None
        except Exception:  # noqa: BLE001
            alt = None
        _cache["alt_nvenc"] = alt
    return _cache["alt_nvenc"]


def _kill(proc):
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


def _drain(stream, box, key):
    """Read a process's stderr to the end in its own thread: a pipe nobody reads fills up (64 KB) and
    ffmpeg then waits forever, writing a repeated warning (corrupt frames, timestamps...)."""
    def run():
        try:
            box[key] = stream.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            box[key] = ""
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _encode_pipe(args_a, args_b, dur, work, progress, cancel):
    """ffmpeg A (filters) | y4m | ffmpeg B (encoder). Returns (ok, error text)."""
    pa = subprocess.Popen(args_a, cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_no_window())
    try:
        pb = subprocess.Popen(args_b, cwd=str(work), stdin=pa.stdout, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, **_no_window())
    except Exception as e:  # noqa: BLE001
        _kill(pa)
        pa.wait()
        return False, str(e)
    pa.stdout.close()           # B owns the pipe now: A gets a broken pipe if B dies
    errs = {}
    ts = [_drain(pa.stderr, errs, "a"), _drain(pb.stderr, errs, "b")]
    g = _Guard([pa, pb], cancel, FF_STALL_S)
    try:
        for line in pb.stdout:
            g.beat()
            if cancel is not None and cancel.is_set():
                raise RenderError("anulat")
            m = re.match(rb"out_time_(?:ms|us)=(\d+)", line.strip())
            if m and dur > 0:
                progress(min(99.0, int(m.group(1)) / 1e6 / dur * 100))
    except BaseException:
        _kill(pa)
        _kill(pb)
        pa.wait()
        pb.wait()
        raise
    finally:
        g.done()
    pb.wait()
    pa.wait()
    for t in ts:
        t.join(5)
    if g.why == "anulat":
        raise RenderError("anulat")
    if pb.returncode == 0 and pa.returncode == 0:
        return True, ""
    if g.why:
        return False, f"ffmpeg {g.why}"
    return False, (errs.get("a", "") + " | " + errs.get("b", "")).strip(" |")[-400:]


def _encode(make_args, dur, work, progress, cancel, make_pipe=None):
    """Run ffmpeg (NVENC first, CPU if that fails). Returns True when NVENC was used.
    make_pipe(alt_ffmpeg) -> (filter args, encoder args): when only the image's old ffmpeg can use
    NVENC, the new one does the filters and pipes the frames to the old one for the encode."""
    low = getattr(_tls, "low", False)       # low priority (e.g. during a game): CPU only, the video card stays free
    use_nvenc = nvenc_ok() and not low
    tried_alt = low
    while True:
        if not use_nvenc and make_pipe is not None and not tried_alt:
            tried_alt = True
            alt = alt_nvenc()
            if alt:
                try:
                    ok, why = _encode_pipe(*make_pipe(alt), dur, work, progress, cancel)
                except RenderError:
                    raise
                except Exception as e:  # noqa: BLE001
                    ok, why = False, str(e)
                if ok:
                    return True
                _cache["alt_nvenc"] = None       # not again in this process: CPU from now on
                _alt_log("codare NVENC cu ffmpeg-ul vechi nu a mers, trec pe procesor: " + " ".join(why.split())[-300:])
        proc = subprocess.Popen(make_args(use_nvenc), cwd=str(work), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, **_no_window())
        errs = {}
        et = _drain(proc.stderr, errs, "e")
        g = _Guard([proc], cancel, FF_STALL_S)
        try:
            for line in proc.stdout:
                g.beat()
                if cancel is not None and cancel.is_set():
                    raise RenderError("anulat")
                m = re.match(rb"out_time_(?:ms|us)=(\d+)", line.strip())
                if m and dur > 0:
                    progress(min(99.0, int(m.group(1)) / 1e6 / dur * 100))
        except BaseException:
            _kill(proc)
            proc.wait()
            raise
        finally:
            g.done()
        proc.wait()
        et.join(10)
        if g.why == "anulat":
            raise RenderError("anulat")
        err = errs.get("e", "")
        if proc.returncode == 0:
            return use_nvenc
        if use_nvenc:
            # NVENC busy / unsupported / stuck: redo on the CPU (or the old ffmpeg)
            _cache["nvenc"] = False
            use_nvenc = False
            continue
        if g.why:
            raise RenderError(f"ffmpeg {g.why}")
        # ffmpeg often repeats the whole (huge) cut expression: keep the lines that say what went wrong
        lines = [ln.strip() for ln in err.splitlines() if ln.strip() and "gte(t," not in ln]
        short = " | ".join(lines[-5:])[-400:]
        raise RenderError(f"ffmpeg: {short or err.strip()[:200] + ' ... ' + err.strip()[-150:] or 'eroare necunoscuta'}")


# ---------------------------------------------------------------- pause removal (first step)
def _script_opt(ff):
    """How this ffmpeg reads a filter graph from a file: '-/filter_complex' (7.0+)
    or the older '-filter_complex_script'. The graph of a long clip is too big for
    the Windows command line."""
    if "fcs" not in _cache:
        work = Path(tempfile.mkdtemp(prefix="fc-"))
        try:
            (work / "t.txt").write_text("[0:a]anull[a]", encoding="utf-8")
            opt = "-filter_complex_script"
            try:
                code, _, _ = _run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                   "anullsrc=d=0.1", "-/filter_complex", str(work / "t.txt"), "-map", "[a]",
                                   "-f", "null", "-"], timeout=60)
                if code == 0:
                    opt = "-/filter_complex"
            except Exception:
                pass
            _cache["fcs"] = opt
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return _cache["fcs"]


def detect_silences(media: Path, prag: float, durata_min: float, dur: float, until: float = None) -> list:
    """[(start, end)] of the pauses in the first audio track (ffmpeg silencedetect).
    prag = amplitude (0.012 = -38 dB); pauses shorter than durata_min are ignored.
    until: read only the first `until` seconds (a long clip of which only the start is used);
    a pause still going on at that point is left out (its end is not known)."""
    ff = find_ffmpeg()[0]
    partial = until is not None and dur and until < dur - 0.01
    code, _, err = _run([ff, "-hide_banner", "-nostats"] + (["-t", f"{until:.3f}"] if partial else []) +
                        ["-i", str(media), "-map", "0:a:0", "-vn",
                         "-af", f"silencedetect=noise={max(0.0001, float(prag)):.4f}:d={max(0.01, float(durata_min)):.3f}",
                         "-f", "null", "-"], timeout=3600)
    if code != 0:
        raise RenderError(f"nu pot analiza pauzele: {err.strip()[-200:]}")
    out, start = [], None
    for line in err.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end:\s*(-?[\d.]+)", line)
        if m and start is not None:
            out.append((start, min(float(m.group(1)), dur or float(m.group(1)))))
            start = None
    if partial:
        # newer ffmpeg also closes a pause at the end of what it read: that end is not real
        return [(a, b) for a, b in out if b < until - 0.05]
    if start is not None and dur and dur - start >= durata_min:
        out.append((start, dur))          # pause that runs to the end of the clip
    return out


def _take_parts(parts, need: float, fps: float, src_limit: float = None):
    """The first `need` seconds of output from `parts` (the last part shortened, on the frame
    grid). None when they don't reach `need` before src_limit (read more of the clip)."""
    out, total = [], 0.0
    for a, b in parts:
        if total + (b - a) >= need:
            end = a + (need - total)
            if fps and fps > 0:
                end = max(a + 1.0 / fps, round(end * fps) / fps)
            if src_limit is not None and end > src_limit:
                return None
            out.append([a, end])
            return out
        out.append([a, b])
        total += b - a
    return None


def needed_parts(media: Path, c: dict, dur: float, keep: float, fps: float, max_out: float):
    """Pause cut of a long clip of which only the first `max_out` seconds (after the cut) are
    kept: silences are looked for only in as much of the clip as needed. -> (parts, truncated)."""
    prag, dmin = c.get("pauze_prag", 0.012), c.get("pauze_durata_min", 0.22)
    margin = 60.0                 # parts must end this far before the end of what was read
    win = min(dur, max_out * 1.5 + 2 * margin)
    while True:
        partial = win < dur - 0.01
        sil = detect_silences(media, prag, dmin, dur, until=win if partial else None)
        parts = speech_parts(sil, win if partial else dur, keep, fps)
        took = _take_parts(parts, max_out, fps, (win - margin) if partial else None)
        if took is not None:
            return took, True
        if not partial:
            return parts, False   # the whole clip (after the cut) is shorter than max_out
        win = min(dur, win * 2)


def speech_parts(silences, dur: float, keep: float, fps: float = 0.0) -> list:
    """What stays: everything except the pauses, each pause shortened to `keep`
    seconds (half on each side). Cut points are put on the video's frame grid so
    picture and sound stay in sync after every cut."""
    half = max(0.0, float(keep)) / 2
    parts, t = [], 0.0
    for s, e in silences:
        a, b = (0.0 if s <= 0.01 else s + half), (dur if e >= dur - 0.01 else e - half)
        if b - a < 0.02:                  # pause shorter than what we keep: leave it
            continue
        if a > t:
            parts.append([t, a])
        t = b
    if dur - t > 0.001:
        parts.append([t, dur])
    if fps and fps > 0:
        snap = lambda x: round(x * fps) / fps
        parts = [[snap(a), snap(b)] for a, b in parts]
    merged = []
    for a, b in parts:
        if b - a < (1.0 / fps if fps else 0.01):
            continue
        if merged and a <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def _fps(video: Path) -> float:
    fp = find_ffmpeg()[1]
    code, out, _ = _run([fp, "-v", "error", "-select_streams", "v:0", "-show_entries",
                         "stream=avg_frame_rate,r_frame_rate", "-of", "json", str(video)], timeout=60)
    try:
        st = (json.loads(out or "{}").get("streams") or [{}])[0]
    except json.JSONDecodeError:
        st = {}
    for k in ("avg_frame_rate", "r_frame_rate"):
        try:
            n, d = str(st.get(k) or "0/0").split("/")
            v = float(n) / float(d)
            if 5 <= v <= 120:
                return v
        except (ValueError, ZeroDivisionError):
            pass
    return 30.0


def _sum_tree(terms):
    """a+b+c+... as a balanced tree ((a+b)+(c+d)): recent ffmpeg refuses a flat sum of
    ~100 terms ("Cannot allocate memory"), the tree is only log2(n) deep."""
    terms = list(terms) or ["0"]
    while len(terms) > 1:
        terms = [f"({terms[i]}+{terms[i + 1]})" if i + 1 < len(terms) else terms[i]
                 for i in range(0, len(terms), 2)]
    return terms[0]


def _alt_log(line):
    try:
        with open(HERE / "sterge_ai.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except OSError:
        pass


def _venc_mid(nvenc):
    """A file only ffmpeg reads again (the final render): fast and visually lossless."""
    return (["-c:v", "h264_nvenc", "-preset", "p1", "-rc", "constqp", "-qp", "13"] if nvenc
            else ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "12"])


def remove_pauses(media: Path, out: Path, c: dict, audio_only: bool = False,
                  progress=lambda p: None, cancel=None, voice: Path = None, fast: bool = False,
                  video_parts: bool = False, max_out: float = None) -> dict:
    """Cut the pauses out of the voice AND the same moments out of the picture.
    `voice`: take the voice from this file instead of from `media` (the dubbed sound
    after dubbing); `out` then gets the picture of `media` + that voice.
    Returns {"before", "after", "cuts", "encoder", "parts"}; "cuts" == 0 means nothing
    to cut (then `out` is not written).
    fast: `out` is only an intermediate file for the final render (quick, visually lossless encode).
    video_parts: with audio_only on a video file: cut the sound at exactly the (frame-aligned)
    places the picture will be cut later, so both can be made separately and still match.
    max_out: only the first max_out seconds of the result are needed (length limit): only that
    part of the clip is analysed, read and encoded ("trunc" True when the rest was dropped).
    The sound and the picture of the same clip get exactly the same parts for the same max_out."""
    ff = find_ffmpeg()[0]
    if not ff:
        raise RenderError("lipseste ffmpeg")
    media, out = Path(media).resolve(), Path(out).resolve()
    voice = Path(voice).resolve() if voice else None
    if audio_only and video_parts:
        info = probe(media)
        dur, has_audio = info["dur"], info["audio"]
        fps = _fps(media)
    elif audio_only:
        code, o, _ = _run([find_ffmpeg()[1], "-v", "error", "-show_entries", "format=duration",
                           "-of", "json", str(media)], timeout=60)
        dur = float((json.loads(o or "{}").get("format") or {}).get("duration") or 0)
        fps, has_audio = 0.0, True
    else:
        info = probe(media)
        dur, has_audio = info["dur"], info["audio"] or bool(voice)
        fps = _fps(media)
    none = {"before": dur, "after": dur, "cuts": 0, "encoder": None, "parts": None, "trunc": False}
    if not has_audio or dur <= 0:
        return none
    keep = 0.0 if c.get("pauze_tiktok") else float(c.get("pauze_pastrata", 0.04))
    trunc = False
    if max_out and max_out > 0 and not voice:
        parts, trunc = needed_parts(media, c, dur, keep, fps, float(max_out))
    else:
        silences = detect_silences(voice or media, c.get("pauze_prag", 0.012), c.get("pauze_durata_min", 0.22), dur)
        parts = speech_parts(silences, dur, keep, fps)
    after = sum(b - a for a, b in parts)
    if not parts or (dur - after < 0.1 and not trunc):
        return none
    src_end = parts[-1][1]                # nothing after this is read (long clip, length limit)
    ain = "1:a:0" if voice else "0:a:0"
    eps = 0.5 / fps if fps else 0.0005
    # sound: exactly [a, b); picture: the frames that start inside [a, b)
    asel = _sum_tree([f"gte(t,{a:.5f})*lt(t,{b:.5f})" for a, b in parts])
    vsel = _sum_tree([f"gte(t,{a - eps:.5f})*lt(t,{b - eps:.5f})" for a, b in parts])
    # sound in 1 ms blocks (aselect keeps whole blocks) so every cut is within 1 ms
    graph = f"[{ain}]aresample=48000,asetnsamples=n=48:p=0,aselect='{asel}',asetpts=N/SR/TB[a]"
    if not audio_only:
        graph = f"[0:v:0]fps={fps:.6f},select='{vsel}',setpts=N/FRAME_RATE/TB[v];" + graph
    work = Path(tempfile.mkdtemp(prefix="pauze-"))
    part = str(out) + ".part" + out.suffix
    try:
        (work / "graph.txt").write_text(graph, encoding="utf-8")
        opt = _script_opt(ff)

        def make_args(nvenc):
            args = [ff, "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats"] + (
                ["-t", f"{src_end + 1.0:.3f}"] if trunc else []) + [
                    "-i", str(media)] + (["-i", str(voice)] if voice else []) + [
                    opt, "graph.txt", "-map", "[a]"]
            if audio_only:
                return args + ["-c:a", "pcm_s16le", part]
            args += ["-map", "[v]"]
            if fast:
                args += _venc_mid(nvenc)
            else:
                args += (["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "18", "-b:v", "0"] if nvenc
                         else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "17"])
            return args + ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", part]
        nv = _encode(make_args, after, work, progress, cancel)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    Path(part).replace(out)
    progress(100.0)
    return {"before": dur, "after": after,
            "cuts": len(parts) - 1 + (parts[0][0] > 0) + (parts[-1][1] < dur and not trunc),
            "encoder": None if audio_only else ("NVENC (placa video)" if nv else "procesor"), "parts": parts,
            "trunc": trunc}


def gap_parts(spans, dur: float, fps: float, min_gap: float = 0.2, keep: float = 0.08, tail: float = 0.25) -> list:
    """What stays when the gaps between dubbed lines are cut ("sincron" without pauses): every line
    [start, end] (sorted), a gap longer than `min_gap` shortened to `keep` (half on each side), the
    lead-in before the first line shortened the same way, at most `tail` s after the last one. Cut
    points on the video's frame grid. -> [[a, b], ...] (the whole clip when nothing is cut)."""
    def grid(t):
        return round(round(t * fps) / fps, 6) if fps else round(t, 6)
    half = max(0.0, keep) / 2
    spans = sorted((max(0.0, float(a)), min(dur, float(b))) for a, b in spans if float(b) > float(a))
    if not spans or dur <= 0:
        return [[0.0, dur]]
    parts, cur_a, prev_end = [], None, 0.0
    for a, b in spans:
        gap = a - prev_end
        if cur_a is None:
            cur_a = grid(max(0.0, a - half)) if a > min_gap else 0.0
        elif gap > min_gap:
            parts.append([cur_a, grid(prev_end + half)])
            cur_a = grid(a - half)
        prev_end = max(prev_end, b)
    parts.append([cur_a, grid(min(dur, prev_end + tail))])
    parts = [[a, b] for a, b in parts if b - a > 0.02]
    return parts or [[0.0, dur]]


def keep_parts(media: Path, out: Path, parts, cancel=None, progress=lambda p: None) -> dict:
    """Only `parts` ([[a, b], ...], on the frame grid) of a clip, picture and sound, joined
    (a quick, visually lossless file for the final render). -> {"before", "after", "encoder"}"""
    ff = find_ffmpeg()[0]
    if not ff:
        raise RenderError("lipseste ffmpeg")
    media, out = Path(media).resolve(), Path(out).resolve()
    info = probe(media)
    fps = _fps(media)
    eps = 0.5 / fps if fps else 0.0005
    after = sum(b - a for a, b in parts)
    vsel = _sum_tree([f"gte(t,{a - eps:.5f})*lt(t,{b - eps:.5f})" for a, b in parts])
    graph = f"[0:v:0]fps={fps:.6f},select='{vsel}',setpts=N/FRAME_RATE/TB[v]"
    if info["audio"]:
        asel = _sum_tree([f"gte(t,{a:.5f})*lt(t,{b:.5f})" for a, b in parts])
        graph += f";[0:a:0]aresample=48000,asetnsamples=n=48:p=0,aselect='{asel}',asetpts=N/SR/TB[a]"
    work = Path(tempfile.mkdtemp(prefix="goluri-"))
    part = str(out) + ".part" + out.suffix
    try:
        (work / "graph.txt").write_text(graph, encoding="utf-8")
        opt = _script_opt(ff)

        def make_args(nvenc):
            args = [ff, "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
                    "-i", str(media), opt, "graph.txt", "-map", "[v]"]
            if info["audio"]:
                args += ["-map", "[a]", "-c:a", "aac", "-b:a", "256k"]
            return args + _venc_mid(nvenc) + ["-pix_fmt", "yuv420p", "-movflags", "+faststart", part]
        nv = _encode(make_args, after, work, progress, cancel)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    Path(part).replace(out)
    return {"before": info["dur"], "after": after, "encoder": "NVENC (placa video)" if nv else "procesor"}


def trim_to(media: Path, out: Path, limit: float, c: dict, audio_only=False,
            progress=lambda p: None, cancel=None, end: float = None) -> dict:
    """Keep only the first `limit` seconds (picture + sound), ending in a small gap between
    words when there is one in the last 15 s. -> {"before", "after", "cut"}"""
    ff = find_ffmpeg()[0]
    media, out = Path(media).resolve(), Path(out).resolve()
    dur = media_duration(media)
    if end is None:          # `end` given: cut exactly there (to match a sound already cut)
        if dur <= limit + 0.5:
            return {"before": dur, "after": dur, "cut": False}
        end = limit
        try:
            # only the gaps in the last 15 s before the limit matter: don't read the rest
            gaps = detect_silences(media, c.get("pauze_prag", 0.012), 0.03, dur, until=limit + 1.0)
            inside = [(a + b) / 2 for a, b in gaps if limit - 15 <= (a + b) / 2 <= limit]
            if inside:
                end = inside[-1]
        except RenderError:
            pass
    elif dur <= end + 0.05:
        return {"before": dur, "after": dur, "cut": False}
    part = str(out) + ".part" + out.suffix
    if not audio_only and _copy_trim(media, part, end):
        Path(part).replace(out)
        return {"before": dur, "after": end, "cut": True}

    def make_args(nvenc):
        args = [ff, "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
                "-i", str(media), "-t", f"{end:.3f}"]
        if audio_only:
            return args + ["-vn", "-c:a", "pcm_s16le", part]
        args += ["-map", "0:v:0", "-map", "0:a?"]
        args += (["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "18", "-b:v", "0"] if nvenc
                 else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "17"])
        return args + ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", part]
    work = Path(tempfile.mkdtemp(prefix="scurtare-"))
    try:
        _encode(make_args, end, work, progress, cancel)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    Path(part).replace(out)
    return {"before": dur, "after": end, "cut": True}


def _copy_trim(media: Path, part: str, end: float) -> bool:
    """Only the end is cut: copy the streams as they are (no re-encode, no quality lost), when
    the clip is plain H.264 4:2:0 + AAC/MP3 without rotation going into an .mp4. False -> re-encode."""
    try:
        if not str(part).lower().endswith(".mp4"):
            return False
        fp = find_ffmpeg()[1]
        code, o, _ = _run([fp, "-v", "error", "-show_streams", "-of", "json", str(media)], timeout=60)
        st = json.loads(o or "{}").get("streams") or []
        vs = [s for s in st if s.get("codec_type") == "video"]
        aus = [s for s in st if s.get("codec_type") == "audio"]
        if code != 0 or len(vs) != 1 or vs[0].get("codec_name") != "h264" or vs[0].get("pix_fmt") != "yuv420p":
            return False
        v = vs[0]
        if (v.get("tags") or {}).get("rotate") not in (None, "0") or any(
                "rotation" in sd and int(sd.get("rotation") or 0) % 360 for sd in v.get("side_data_list") or []):
            return False
        if any(a.get("codec_name") not in ("aac", "mp3") for a in aus):
            return False
        if any(s.get("codec_type") not in ("video", "audio") for s in st):
            return False          # data / subtitle tracks: let the normal path drop them
        ff = find_ffmpeg()[0]
        code, _, _ = _run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(media), "-t", f"{end:.3f}",
                           "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-movflags", "+faststart", str(part)],
                          timeout=1800)
        if code == 0:
            # copied packets end on whole frames / B-frame order: accept only a few frames of slack
            code, o, _ = _run([fp, "-v", "error", "-show_entries", "stream=duration", "-of", "json", str(part)],
                              timeout=60)
            durs = [float(x.get("duration") or 0) for x in json.loads(o or "{}").get("streams") or []]
            if code == 0 and durs and all(end - 0.3 <= d <= end + 0.2 for d in durs):
                return True
    except Exception:  # noqa: BLE001 - re-encode instead
        pass
    try:
        Path(part).unlink(missing_ok=True)
    except OSError:
        pass
    return False


def media_duration(media: Path) -> float:
    fp = find_ffmpeg()[1]
    code, o, _ = _run([fp, "-v", "error", "-show_entries", "format=duration", "-of", "json",
                       str(Path(media).resolve())], timeout=60)
    try:
        return float((json.loads(o or "{}").get("format") or {}).get("duration") or 0)
    except (ValueError, json.JSONDecodeError):
        return 0.0


def _atempo(f: float) -> str:
    """atempo chain for any speed factor (one atempo only takes 0.5..2)."""
    parts = []
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.5f}")
    return ",".join(parts)


VOICE_PAUSE_MIN = 0.12      # "only the voice": every pause at least this long is cut out of the voice
VOICE_PAUSE_KEEP = 0.03     # what is left of each pause (a hair, so words don't click into each other)


def fit_voice(voice: Path, target: float, c: dict, out_wav: Path, cancel=None) -> dict:
    """"Only the voice, fitted to the clip": every pause cut out of the dubbed voice, then the
    whole voice sped up - pitch going up with it, like a tape played faster - so it ends with the
    clip (`target` seconds). Never slowed down: a voice already shorter than the clip stays at
    1x and the rest is silence. -> {"before", "after", "factor", "parts"}"""
    ff = find_ffmpeg()[0]
    if not ff:
        raise RenderError("lipseste ffmpeg")
    voice, out_wav = Path(voice).resolve(), Path(out_wav).resolve()
    work = Path(tempfile.mkdtemp(prefix="voce-"))
    try:
        dur = media_duration(voice)
        if dur <= 0 or target <= 0:
            raise RenderError("nu pot citi durata vocii sau a clipului")
        cv = dict(c, pauze_durata_min=VOICE_PAUSE_MIN, pauze_pastrata=VOICE_PAUSE_KEEP, pauze_tiktok=False)
        cut = remove_pauses(voice, work / "fara_pauze.wav", cv, audio_only=True, cancel=cancel)
        if cut["cuts"]:
            src, parts, length = work / "fara_pauze.wav", cut["parts"], cut["after"]
        else:
            src, parts, length = voice, [[0.0, dur]], dur
        factor = max(1.0, length / target)
        sr = 48000
        # asetrate = played faster with the pitch going up (no pitch correction), then back to 48 kHz
        speed = (f"asetrate={sr * factor:.3f},aresample={sr}," if factor > 1.0005 else "")
        af = f"aresample={sr},{speed}apad=whole_dur={target:.3f},atrim=0:{target:.3f}"
        code, _, err = _run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-af", af,
                             "-ar", str(sr), "-c:a", "pcm_s16le", str(out_wav)], timeout=1800)
        if code != 0:
            raise RenderError(f"nu pot potrivi viteza vocii: {err.strip()[-200:]}")
        return {"before": dur, "after": target, "factor": factor, "parts": parts, "voice_len": length}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# "Studio sound" for the dubbed voice: rumble cut, a little less boom and a little more presence,
# softer harsh "s" sounds, the lines' levels evened out, then the whole voice at TikTok's loudness
# (-14 LUFS, true peak -1.5 dB). Volume and tone only: the length and every line's place stay
# exactly as they are.
VOICE_POLISH = ("highpass=f=70,equalizer=f=250:t=q:w=1:g=-2,equalizer=f=3500:t=q:w=1.2:g=2,"
                "deesser=i=0.35:m=0.5:f=0.5,acompressor=threshold=0.1:ratio=2.5:attack=8:release=120:makeup=1")
VOICE_LUFS, VOICE_TP = -14.0, -1.5


def polish_voice(voice: Path, out_wav: Path) -> dict:
    """The dubbed voice made to sound finished (VOICE_POLISH + two-pass loudness normalization).
    -> {"before_lufs", "after_lufs"}; raises RenderError (the caller keeps the plain voice)."""
    ff = find_ffmpeg()[0]
    if not ff:
        raise RenderError("lipseste ffmpeg")
    voice, out_wav = Path(voice).resolve(), Path(out_wav).resolve()
    dur = media_duration(voice)
    if dur <= 0:
        raise RenderError("nu pot citi durata vocii")
    target = f"I={VOICE_LUFS}:TP={VOICE_TP}:LRA=11"
    code, _, err = _run([ff, "-hide_banner", "-nostats", "-i", str(voice), "-af",
                         f"aresample=48000,{VOICE_POLISH},loudnorm={target}:print_format=json",
                         "-f", "null", "-"], timeout=900)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err or "")
    if code != 0 or not m:
        raise RenderError(f"nu pot masura volumul vocii: {(err or '').strip()[-200:]}")
    st = json.loads(m.group(0))
    if not all(_finite(st.get(k)) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")):
        raise RenderError("vocea e prea linistita ca sa-i masor volumul")
    norm = (f"loudnorm={target}:measured_I={st['input_i']}:measured_TP={st['input_tp']}:"
            f"measured_LRA={st['input_lra']}:measured_thresh={st['input_thresh']}:"
            f"offset={st['target_offset']}:linear=true")
    part = out_wav.with_name(out_wav.stem + ".part.wav")
    code, _, err = _run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(voice), "-af",
                         f"aresample=48000,{VOICE_POLISH},{norm},aresample=48000",
                         "-ar", "48000", "-c:a", "pcm_s16le", str(part)], timeout=1800)
    if code != 0:
        part.unlink(missing_ok=True)
        raise RenderError(f"nu pot curata vocea: {(err or '').strip()[-200:]}")
    got = media_duration(part)
    if abs(got - dur) > 0.05:          # never move the lines: a different length = keep the plain voice
        part.unlink(missing_ok=True)
        raise RenderError(f"vocea curatata are alta lungime ({got:.2f}s fata de {dur:.2f}s)")
    part.replace(out_wav)
    return {"before_lufs": float(st["input_i"]), "after_lufs": VOICE_LUFS}


def _finite(v) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def retime_audio(src: Path, out_wav: Path, tempo: float):
    """The sound played `tempo` times as fast, same pitch (atempo) -> AAC .m4a (the "sincron" voice
    factor: the clip's sound goes up slowed down, so every line gets that much more room)."""
    ff = find_ffmpeg()[0]
    if not ff:
        raise RenderError("lipseste ffmpeg")
    out_wav = Path(out_wav).resolve()
    code, _, err = _run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(Path(src).resolve()), "-vn",
                         "-af", _atempo(tempo), "-c:a", "aac", "-b:a", "192k", str(out_wav)], timeout=1800)
    if code != 0 or not out_wav.exists():
        raise RenderError(f"nu pot schimba viteza sunetului: {(err or '').strip()[-200:]}")


def speed_pitch(voice: Path, out_wav: Path, factor: float, target: float):
    """The voice played `factor` times faster with the pitch going up (like a tape played faster),
    padded / cut to exactly `target` seconds (the clip's length) -> WAV."""
    ff = find_ffmpeg()[0]
    if not ff:
        raise RenderError("lipseste ffmpeg")
    voice, out_wav = Path(voice).resolve(), Path(out_wav).resolve()
    if not target or target <= 0 or not factor or factor <= 0:
        raise RenderError(f"lungime sau viteza gresita pentru voce ({target}, {factor})")
    sr = 48000
    af = (f"aresample={sr},asetrate={sr * factor:.3f},aresample={sr},"
          f"apad=whole_dur={target:.3f},atrim=0:{target:.3f}")
    code, _, err = _run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(voice), "-af", af,
                         "-ar", str(sr), "-c:a", "pcm_s16le", str(out_wav)], timeout=1800)
    if code != 0 or not out_wav.exists():
        raise RenderError(f"nu pot accelera vocea: {(err or '').strip()[-200:]}")


def fit_segments(segments, parts, factor):
    """Subtitle times after the voice's pauses were cut and its speed changed."""
    f = factor or 1.0
    return [dict(s, start=s["start"] / f, end=s["end"] / f) for s in remap_segments(segments, parts)]


def encode_audio(wav: Path, out: Path, bitrate="256"):
    ff = find_ffmpeg()[0]
    out = Path(out).resolve()
    part = out.with_name(out.name + ".part" + out.suffix)
    codec = (["-c:a", "libmp3lame", "-b:a", f"{bitrate}k"] if out.suffix.lower() == ".mp3"
             else ["-c:a", "pcm_s16le"])
    code, _, err = _run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(Path(wav).resolve()),
                         *codec, str(part)], timeout=1800)
    if code != 0:
        raise RenderError(f"nu pot salva vocea: {err.strip()[-200:]}")
    part.replace(out)


def remap_time(t: float, parts) -> float:
    """Where moment `t` of the uncut clip lands in the clip without pauses."""
    out = 0.0
    for a, b in parts:
        if t <= a:
            return out
        if t < b:
            return out + (t - a)
        out += b - a
    return out


def remap_segments(segments, parts):
    res = []
    for s in segments:
        a, b = remap_time(float(s["start"]), parts), remap_time(float(s["end"]), parts)
        if b - a >= 0.05:
            res.append(dict(s, start=a, end=b))
    return res


# ---------------------------------------------------------------- AI subtitle removal (STTN)
AI_SCRIPT = HERE / "sterge_ai.py"


def run_ai(video: Path, out: Path, y: int, h: int, progress=lambda p: None, cancel=None,
           method: str = "litere") -> str:
    """Clean the band y..y+h (pixels of this video) with sterge_ai.py in its own process."""
    ff = find_ffmpeg()[0]
    if not AI_SCRIPT.exists():
        raise RenderError("lipseste sterge_ai.py")
    return _run_helper([sys.executable, str(AI_SCRIPT), "run", str(video), str(out), str(y), str(h), ff, method],
                       out, progress, cancel, 4, "stergerea AI nu a mers")


def _kill_tree(proc):
    """The helper and what it started (its ffmpeg decoders / encoders)."""
    if proc.poll() is not None:
        return
    if sys.platform.startswith("win"):
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15,
                           **_no_window())
        except Exception:  # noqa: BLE001
            pass
    _kill(proc)


def _run_helper(args, out, progress, cancel, keep, fail_msg) -> str:
    """sterge_ai.py in its own process: PROGRES / INFO / EROARE lines. Stopped when cancelled or when it
    stops writing for AI_STALL_S (a hung CUDA call / decoder would otherwise block this clip forever)."""
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **_no_window())
    last, err = [], ""
    g = _Guard([proc], cancel, AI_STALL_S, kill=_kill_tree)
    try:
        for raw in proc.stdout:
            g.beat()
            if cancel is not None and cancel.is_set():
                _kill_tree(proc)
                raise RenderError("anulat")
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("PROGRES "):
                try:
                    progress(float(line.split()[1]))
                except ValueError:
                    pass
            elif line.startswith("EROARE "):
                err = line[7:]
            elif line:
                last = (last + [line[5:] if line.startswith("INFO ") else line])[-keep:]
        proc.wait()
    finally:
        g.done()
    if g.why == "anulat":
        raise RenderError("anulat")
    if g.why:
        raise RenderError(f"pasul pe placa video {g.why}")
    if proc.returncode != 0 or not Path(out).exists():
        raise RenderError(err or " | ".join(last) or fail_msg)
    return " | ".join(x for x in last if x.startswith(("placa", "gata")))


ESR_KINDS = {"esrgan": ("esr_rapid", "Real-ESRGAN rapid"), "esrgan_general": ("esr_general", "Real-ESRGAN general")}


def run_gpu(video: Path, out: Path, opts: dict, progress=lambda p: None, cancel=None) -> str:
    """sterge_ai.py process (subtitles out / black bars out / Real-ESRGAN) in its own process."""
    ff = find_ffmpeg()[0]
    if not AI_SCRIPT.exists():
        raise RenderError("lipseste sterge_ai.py")
    return _run_helper([sys.executable, str(AI_SCRIPT), "process", str(video), str(out), json.dumps(opts), ff],
                       out, progress, cancel, 5, "pasul pe placa video nu a mers")


def _gpu_plan(video: Path, c: dict):
    """What the video card does before ffmpeg: clean (subtitles), crop (bars), up (Real-ESRGAN)."""
    info = probe(video)
    w, h = info["w"], info["h"]
    opts = {}
    if c.get("sterge_sub_orig") and c.get("sterge_mod") == "ai":
        zone = cover_zone(c, w, h)
        if zone:
            opts["clean"] = {"y": zone[1], "h": zone[3], "method": c.get("ai_metoda", "litere")}
    esr = ESR_KINDS.get(c.get("upscale_metoda"))
    if esr and c.get("iesire_1080") and w > 0 and h > 0:
        cx, cy, cw, ch = 0, 0, w, h
        if c.get("format_916"):
            cx, cy, cw, ch = detect_bars(video, info["dur"], w, h)
            W = _canvas_w(w, c)          # same canvas as _layout_916 (iesire_1080 is on here)
            H = int(round(W * 16 / 9 / 2)) * 2
            s = W / w
            if ch * s > H:
                s = H / ch
        else:
            s = 1080 / min(w, h) if min(w, h) < 1080 else 1.0
        fw, fh = int(round(cw * s / 2)) * 2, int(round(ch * s / 2)) * 2
        if fw > cw * 1.05:
            opts["up"] = {"model": esr[0], "w": fw, "h": fh}
            if c.get("upscale_prescale"):
                opts["up"]["prescale"] = True     # input shrunk first: ~4x less Real-ESRGAN work
            if (cx, cy, cw, ch) != (0, 0, w, h):
                opts["crop"] = [cx, cy, cw, ch]
    return opts, info


def _zone_after_crop(c, crop, h0):
    z = c.get("sterge_zona")
    if not crop or not z:
        return c
    try:
        fy, fh = float(z["y"]), float(z["h"])
    except (TypeError, ValueError, KeyError):
        return c
    cy, ch = crop[1], crop[3]
    top, bot = max(fy * h0, cy), min((fy + fh) * h0, cy + ch)
    if bot <= top:
        top, bot = cy + ch * 0.75, cy + ch * 0.9
    if (fy + fh) * h0 >= cy + ch - 2:
        bot = cy + ch                      # band on the cut edge: to the very bottom
    return dict(c, sterge_zona=dict(z, y=round((top - cy) / ch, 5), h=round((bot - top) / ch, 5)))


def gpu_step(video: Path, work: Path, c: dict, progress=lambda p: None, cancel=None):
    """Before ffmpeg: on the video card, in one pass, the burned-in subtitles out (AI),
    the black bars out and Real-ESRGAN. Returns (video, c, note). When it fails: blur and
    the fast upscaler instead, so the clip still comes out."""
    opts, info = _gpu_plan(video, c)
    if not opts:
        return video, c, None
    if getattr(_tls, "low", False):         # low priority (e.g. during a game): no video card -> blur / lanczos
        c2 = dict(c)
        if "clean" in opts:
            c2["sterge_mod"] = "blur"
        if "up" in opts:
            c2["upscale_metoda"] = "lanczos"
        return video, c2, "în timpul jocului: blur / mărire lanczos (fără placa video)"
    out = Path(work) / "placa_video.mp4"
    t0 = time.time()
    try:
        det = run_gpu(video, out, opts, progress, cancel)
    except RenderError as e:
        if str(e) == "anulat":
            raise
        try:
            with open(HERE / "sterge_ai.log", "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + f"FALLBACK pe {Path(video).name}: {e}\n")
        except OSError:
            pass
        c2 = dict(c)
        if "clean" in opts:
            c2["sterge_mod"] = "blur"
        if "up" in opts:
            c2["upscale_metoda"] = "fsrcnnx"
        return video, c2, f"pasul pe placa video nu a mers ({e}); am pus blur / marire rapida"
    c2 = dict(c)
    parts = []
    if "clean" in opts:
        c2["sterge_mod"] = "ai_gata"
        parts.append("subtitrari sterse cu AI")
    if "up" in opts:
        c2 = _zone_after_crop(c2, opts.get("crop"), info["h"])
        c2["_up_done"] = ESR_KINDS[c["upscale_metoda"]][1]
        c2["_bars_done"] = True           # bars looked for (and cut) on the original already
        parts.append(f"marit cu {c2['_up_done']} la {opts['up']['w']}x{opts['up']['h']}"
                     + (" (fara benzi negre)" if opts.get("crop") else ""))
    return out, c2, f"{', '.join(parts)} in {time.time() - t0:.0f}s ({det})"


ai_step = gpu_step


def cover_only(video: Path, out: Path, c: dict, progress=lambda p: None, cancel=None) -> dict:
    """Test: only cover the original burned-in subtitles (original sound, nothing else)."""
    ff = find_ffmpeg()[0]
    if not ff:
        raise RenderError("lipseste ffmpeg")
    video, out = Path(video).resolve(), Path(out).resolve()
    work = Path(tempfile.mkdtemp(prefix="acoperire-"))
    note = None
    if (c.get("sterge_zona") and c.get("sterge_mod") == "ai") or c.get("upscale_metoda") in ESR_KINDS:
        base = progress
        cc = dict(c, sterge_sub_orig=bool(c.get("sterge_zona")))
        video, c, note = gpu_step(video, work, cc, lambda p: base(p * 0.85), cancel)
        progress = lambda p: base(85 + p * 0.15)
    info = with_upscale(probe(video), c, video)
    c = map_zone(c, info)
    zone = cover_zone(dict(c, sterge_sub_orig=True), info["w"], info["h"]) if c.get("sterge_zona") else None
    if c.get("sterge_mod") == "ai_gata":
        zone = None                       # already cleaned
    if not zone and not info["pre"] and not note:
        shutil.rmtree(work, ignore_errors=True)
        raise RenderError("nimic de facut: nici banda de blur, nici export la 1080p")
    head, srcl = (f"[0:v]{info['pre']}[up];", "[up]") if info["pre"] else ("", "[0:v]")
    if zone:
        x, y, zw, zh = zone
        sig = max(10, min(60, zh // 2))
        band = (f",drawbox=x={x}:y={y}:w={zw}:h={zh}:color=black@0.75:t=fill"
                if c.get("sterge_mod", "blur") == "banda" else "")
        fc = head + (f"{srcl}split[c0][c1];[c1]crop={zw}:{zh}:{x}:{y},gblur=sigma={sig}:steps=3,"
                     f"eq=brightness=-0.10[cb];[c0][cb]overlay={x}:{y}{band}[v]")
    else:
        fc = head + f"{srcl}null[v]"
    part = str(out) + ".part.mp4"
    hw = hw_args(info, work)

    def make_args(nvenc):
        args = [ff, "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats", *hw,
                "-i", str(video), "-filter_complex", fc, "-map", "[v]", "-map", "0:a?"]
        args += (["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21", "-b:v", "0"] if nvenc
                 else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])
        return args + ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", part]
    try:
        nv = _encode(make_args, info["dur"], work, progress, cancel)
        Path(part).replace(out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        Path(part).unlink(missing_ok=True)      # stopped / failed: no half file left behind
    progress(100.0)
    return {"encoder": "NVENC (placa video)" if nv else "procesor", "ai": note, "up": info.get("up")}


def render(video: Path, dub_audio: Path, segments, out: Path, c: dict, progress=lambda p: None,
           cancel=None, work_dir: Path = None):
    """Video + dubbed audio + subtitles + watermark -> out (mp4)."""
    ff = find_ffmpeg()[0]
    if not ff:
        raise RenderError("lipseste ffmpeg; porneste din nou 'Porneste dublarea.bat' ca sa-l instaleze")
    video, dub_audio, out = Path(video).resolve(), Path(dub_audio).resolve(), Path(out).resolve()
    work = Path(work_dir or tempfile.mkdtemp(prefix="randare-")).resolve()
    work.mkdir(parents=True, exist_ok=True)
    cut = None
    if c.get("taie_pauze"):
        # the dubbed voice is shorter than the original, so the slots leave new pauses:
        # cut them out of the dubbed voice and the same moments out of the picture
        tmp = work / "dublaj_fara_pauze.mp4"
        cut = remove_pauses(video, tmp, c, progress=lambda p: progress(p * 0.4), cancel=cancel,
                            voice=dub_audio, fast=True)
        if cut["cuts"]:
            video = dub_audio = tmp
            segments = remap_segments(segments, cut["parts"])
            c = dict(c, pastreaza_audio_original=False)   # the original sound is not cut
        base = progress
        progress = lambda p: base(40 + p * 0.6)
    ai_note = None
    if (c.get("sterge_sub_orig") and c.get("sterge_mod") == "ai") or c.get("upscale_metoda") in ESR_KINDS:
        base2 = progress
        video, c, ai_note = gpu_step(video, work, c, lambda p: base2(p * 0.8), cancel)
        progress = lambda p: base2(80 + p * 0.2)
    info = with_upscale(probe(video), c, video)
    c = map_zone(c, info)
    (work / "subs.ass").write_text(build_ass(segments, info["w"], info["h"], c, info["dur"]), encoding="utf-8")
    c = dict(c, _fontsdir=fonts_for(work))
    has_logo = bool(c.get("watermark_activ") and c.get("watermark_logo_activ") and LOGO_FILE.exists())
    part = str(out) + ".part.mp4"
    hw = hw_args(info, work)

    def make_args(nvenc):
        args = [ff, "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats", *hw,
                "-i", str(video), "-i", str(dub_audio)]
        if has_logo:
            args += ["-loop", "1", "-i", str(LOGO_FILE)]
        args += ["-filter_complex", _filters(info, c, has_logo), "-map", "[v]", "-map", "1:a:0"]
        if c.get("pastreaza_audio_original") and info["audio"]:
            args += ["-map", "0:a:0", "-metadata:s:a:1", "title=Original"]
        args += ["-metadata:s:a:0", f"language={c.get('limba', 'ro')}", "-disposition:a:0", "default"]
        args += (["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21", "-b:v", "0"] if nvenc
                 else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])
        args += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                 "-t", f"{info['dur']:.3f}", part]
        return args

    def make_pipe(alt):
        """The new ffmpeg makes the picture (filters, subtitles, logo) and pipes it as y4m to the
        old ffmpeg, which encodes it on the GPU and adds the sound (same maps as make_args)."""
        a = [ff, "-y", "-hide_banner", "-loglevel", "error", "-nostats", *hw,
             "-i", str(video), "-i", str(dub_audio)]
        if has_logo:
            a += ["-loop", "1", "-i", str(LOGO_FILE)]
        a += ["-filter_complex", _filters(info, c, has_logo), "-map", "[v]", "-an",
              "-t", f"{info['dur']:.3f}", "-pix_fmt", "yuv420p", "-f", "yuv4mpegpipe", "-"]
        b = [alt, "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
             "-f", "yuv4mpegpipe", "-i", "-", "-i", str(dub_audio)]
        if c.get("pastreaza_audio_original") and info["audio"]:
            b += ["-i", str(video)]
        b += ["-map", "0:v:0", "-map", "1:a:0"]
        if c.get("pastreaza_audio_original") and info["audio"]:
            b += ["-map", "2:a:0", "-metadata:s:a:1", "title=Original"]
        b += ["-metadata:s:a:0", f"language={c.get('limba', 'ro')}", "-disposition:a:0", "default"]
        b += [*ALT_VENC, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
              "-t", f"{info['dur']:.3f}", part]
        return a, b

    try:
        use_nvenc = _encode(make_args, info["dur"], work, progress, cancel, make_pipe)
        Path(part).replace(out)
    finally:
        Path(part).unlink(missing_ok=True)
    teaser = None
    if c.get("inceput_dramatic"):
        # the most dramatic 3-4 s once more at the start (the clip stays as it is if this fails)
        try:
            win = pick_teaser(segments, dub_audio, info["dur"], c.get("_teaser_id"))
            if win:
                add_teaser(out, win[0], win[1], c.get("limba", "ro"), use_nvenc, cancel)
                teaser = win
                d = win[1] - win[0]
                segments = [dict(s, start=float(s["start"]) + d, end=float(s["end"]) + d) for s in segments]
        except Exception as e:  # noqa: BLE001 - the clip is already made: it stays without it
            if isinstance(e, RenderError) and str(e) == "oprit":
                raise
            teaser = f"nu a mers: {str(e)[:200]}"
    progress(100.0)
    if c.get("subtitrari_srt"):
        write_srt(segments, out.with_suffix(".srt"))
    return {"encoder": "NVENC (placa video)" if use_nvenc else "procesor", "w": info["w"], "h": info["h"],
            "pauze": {k: cut[k] for k in ("before", "after", "cuts")} if cut and cut["cuts"] else None,
            "ai": ai_note, "up": info.get("up"), "inceput": teaser}


# ---------------------------------------------------------------- dramatic start (flash-forward)
# The clip's most dramatic 3-4 seconds shown once more at its very start (then the clip from the
# beginning): the line the agent chose, else the loudest stretch of the voice (shouts, conflict).
TEASER_MIN, TEASER_MAX = 3.0, 4.0


def _loudness_track(path: Path, rate=8000):
    """RMS of the sound every 0.1 s (pure Python: no numpy needed on the GPU)."""
    import array
    ff = find_ffmpeg()[0]
    raw = subprocess.run([ff, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
                         capture_output=True, timeout=600, **_no_window()).stdout
    a = array.array("h")
    a.frombytes(raw[: len(raw) // 2 * 2])
    hop = rate // 10
    return [(sum(x * x for x in a[i:i + hop]) / hop) ** 0.5 for i in range(0, len(a) - hop + 1, hop)]


def pick_teaser(segments, audio: Path, dur: float, want_id=None):
    """(start, end) in the final video's time of the flash-forward, or None (clip too short, nothing
    found). Starts at the chosen line (not in the first seconds), ends on a line's end 3-4 s later
    when there is one, else at 4 s."""
    if dur < 12:
        return None
    segs = sorted((s for s in segments if float(s.get("end", 0)) > float(s.get("start", 0))),
                  key=lambda s: float(s["start"]))
    start = None
    if want_id is not None:
        for s in segs:
            if str(s.get("id")) == str(want_id) and 4.0 <= float(s["start"]) <= dur - TEASER_MIN:
                start = float(s["start"])
                break
    if start is None:                   # the loudest 3.5 s that begin a line (not the first seconds)
        try:
            env = _loudness_track(audio)
        except Exception:  # noqa: BLE001
            env = []
        best = None
        for s in segs:
            a = float(s["start"])
            if a < 4.0 or a > dur - TEASER_MIN:
                continue
            w = env[int(a * 10):int((a + 3.5) * 10)]
            if not w:
                continue
            q = sum(w) / len(w) * (1.15 if "!" in str(s.get("text") or "") else 1.0)
            if best is None or q > best[0]:
                best = (q, a)
        start = best[1] if best else None
    if start is None:
        return None
    start = max(0.0, start - 0.12)
    ends = [float(s["end"]) + 0.12 for s in segs if start + TEASER_MIN <= float(s["end"]) + 0.12 <= start + TEASER_MAX]
    end = min(max(ends) if ends else start + TEASER_MAX, dur - 0.05)
    return (round(start, 3), round(end, 3)) if end - start >= TEASER_MIN - 0.5 else None


def _audio_streams(path: Path) -> int:
    ff = find_ffmpeg()[1]
    out = subprocess.run([ff, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, timeout=120, **_no_window()).stdout
    return len([x for x in out.split() if x.strip()])


def add_teaser(out: Path, t0: float, t1: float, lang="ro", nvenc=False, cancel=None):
    """out -> [out from t0 to t1] + out (the same picture, subtitles, watermark and sound; the sound
    fades out in the last 0.12 s of the flash-forward so the cut doesn't click)."""
    ff = find_ffmpeg()[0]
    out = Path(out)
    na = max(1, _audio_streams(out))
    d = t1 - t0
    parts = ["[0:v]setpts=PTS-STARTPTS[tv]"]
    for i in range(na):
        parts.append(f"[0:a:{i}]asetpts=PTS-STARTPTS,afade=t=out:st={max(0.0, d - 0.12):.3f}:d=0.12[ta{i}]")
    cat = "[tv]" + "".join(f"[ta{i}]" for i in range(na)) + "[1:v]" + "".join(f"[1:a:{i}]" for i in range(na))
    parts.append(f"{cat}concat=n=2:v=1:a={na}[v]" + "".join(f"[a{i}]" for i in range(na)))
    tmp = out.with_name(out.stem + ".inceput.mp4")
    for enc in ((["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21", "-b:v", "0"],) if nvenc else ()) + \
            (["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"],):
        args = [ff, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t0:.3f}", "-t", f"{d:.3f}", "-i", str(out),
                "-i", str(out), "-filter_complex", ";".join(parts), "-map", "[v]"]
        for i in range(na):
            args += ["-map", f"[a{i}]"]
        args += ["-metadata:s:a:0", f"language={lang}", "-disposition:a:0", "default"]
        if na > 1:
            args += ["-metadata:s:a:1", "title=Original"]
        args += enc + ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp)]
        p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **_no_window())
        while p.poll() is None:
            if cancel is not None and cancel.is_set():
                p.kill()
                tmp.unlink(missing_ok=True)
                raise RenderError("oprit")
            time.sleep(0.3)
        err = p.stderr.read().decode("utf-8", "replace") if p.stderr else ""
        if p.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(out)
            return
        tmp.unlink(missing_ok=True)
    raise RenderError("inceputul dramatic nu a mers: " + err[-300:])


def contact_sheet(video, times, out_jpg: Path):
    """Frames at `times` with a 10% grid, tiled into one image for the agent. -> (n, cols, rows)"""
    ff = find_ffmpeg()[0]
    video = Path(video).resolve()
    info = probe(video)
    dur = info["dur"] or 1.0
    times = [min(max(0.0, t), max(0.0, dur - 0.1)) for t in times][:12] or [dur * (i + 0.5) / 12 for i in range(12)]
    vertical = info["h"] > info["w"]
    scale = "scale=-2:480" if vertical else "scale=480:-2"
    work = Path(tempfile.mkdtemp(prefix="cadre-"))
    try:
        n = 0
        for t in times:
            code, _, _ = _run([ff, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
                               "-frames:v", "1", "-vf", f"{scale},drawgrid=w=iw/10:h=ih/10:t=1:c=red@0.55",
                               "-q:v", "3", str(work / f"f{n:02d}.jpg")], timeout=60)
            if code == 0 and (work / f"f{n:02d}.jpg").exists():
                n += 1
        if not n:
            raise RenderError("nu pot scoate cadre din clip")
        cols = min(n, 4 if vertical else 3)
        rows = (n + cols - 1) // cols
        code, _, err = _run([ff, "-y", "-hide_banner", "-loglevel", "error", "-framerate", "1",
                             "-i", str(work / "f%02d.jpg"),
                             "-vf", f"tile={cols}x{rows}:padding=6:color=white", "-frames:v", "1", "-q:v", "3",
                             str(Path(out_jpg).resolve())], timeout=60)
        if code != 0:
            raise RenderError(f"nu pot lipi cadrele: {err.strip()[-200:]}")
        return n, cols, rows
    finally:
        shutil.rmtree(work, ignore_errors=True)


def frame(video, at: float, out_jpg: Path) -> dict:
    """Plain frame (no effects) for choosing the zone to cover."""
    ff = find_ffmpeg()[0]
    if not ff:
        raise RenderError("lipseste ffmpeg")
    video = Path(video).resolve()
    info = probe(video)
    at = max(0.0, min(float(at), max(0.0, info["dur"] - 0.1)))
    code, _, err = _run([ff, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(video),
                         "-frames:v", "1", "-vf", "scale='min(720,iw)':-2", "-q:v", "3", str(Path(out_jpg).resolve())],
                        timeout=60)
    if code != 0:
        raise RenderError(f"nu pot scoate cadrul: {err.strip()[-200:]}")
    return info


def preview(video, c, out_jpg: Path, at: float = None, sample=None):
    """One frame with the current subtitle + watermark settings."""
    ff = find_ffmpeg()[0]
    if not ff:
        raise RenderError("lipseste ffmpeg")
    work = Path(tempfile.mkdtemp(prefix="previzualizare-"))
    try:
        out_jpg = Path(out_jpg).resolve()
        if video:
            video = Path(video).resolve()
            info = with_upscale(probe(video), c, video)
            c = map_zone(c, info)
            src = ["-i", str(video)]
        else:
            info = {"w": 1080, "h": 1920, "dur": 10.0, "audio": False}
            src = ["-f", "lavfi", "-i", "gradients=s=1080x1920:c0=0x223a2e:c1=0x0b100d:d=10"]
        at = at if at is not None else min(5.0, max(1.5, info["dur"] / 3))
        text = sample or "Lucy a murit de pe urma orelor suplimentare, căci Anna o obliga mereu să rămână."
        style = resolve_style(c, info["w"], info["h"])
        words = text.split()
        seg = [{"start": 0.0, "end": 3.0, "text": text}]
        # the clip is cut to start 1.2 s before the frame, so the line is ~40% "sung" in the preview
        (work / "subs.ass").write_text(build_ass(seg, info["w"], info["h"], c, 3.0), encoding="utf-8")
        c = dict(c, _fontsdir=fonts_for(work))
        has_logo = bool(c.get("watermark_activ") and c.get("watermark_logo_activ") and LOGO_FILE.exists())
        args = [ff, "-y", "-hide_banner", "-loglevel", "error", *hw_args(info, work),
                "-ss", f"{max(0.0, at - 1.2):.2f}", *src]
        if has_logo:
            args += ["-loop", "1", "-i", str(LOGO_FILE)]
        fc = _filters(info, c, has_logo).replace("[2:v]", "[1:v]")
        args += ["-filter_complex", fc + ";[v]scale='min(720,iw)':-2[o]", "-map", "[o]",
                 "-ss", "1.2", "-frames:v", "1", "-q:v", "3", str(out_jpg)]
        code, _, err = _run(args, timeout=180, cwd=str(work))
        if code != 0:
            raise RenderError(f"previzualizarea nu a mers: {err.strip()[-300:]}")
        return {"style": style, "w": info["w"], "h": info["h"]}
    finally:
        shutil.rmtree(work, ignore_errors=True)
