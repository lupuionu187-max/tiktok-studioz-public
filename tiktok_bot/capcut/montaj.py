"""Pregătirea pentru CapCut + montajul final + subtitrările românești (folosit de aplicația de dublare, de fila CapCut
și din linia de comandă).

Două variante (alese în aplicația de dublare, setarea capcut_pregatire; montajul o află din marcajul clipului pregătit):

  doar_final (din 21 oct 2026, nicio pauză tăiată):
    pregătire (înainte de CapCut): clipul descărcat -> doar scurtat de la final la cel mult 4:00 (dacă la 4:00 se
                                    vorbește: în ultima pauză dintre cuvinte din ultimele 10 s); mai mic (~1,5 Mbit/s)
    montaj final (după CapCut):     clipul pregătit + vocea română, netăiate -> capcut_final\\<nume>_ro.mp4
  ca_inainte (cum era până pe 21 oct):
    pregătire: întâi fără nicio pauză (detector de voce Silero + cuvintele găsite de Whisper), apoi (doar dacă tot
               trece de 4:30) tăiat de la final la 4:30, între două fraze; la fiecare îmbinare sunetul e estompat 10 ms
               și imaginea se taie în aceleași locuri, pe grila cadrelor (randare.keep_parts)
    montaj final: clipul pregătit + vocea română -> fără niciun gol -> capcut_final\\<nume>_ro.mp4
  (+ la amândouă doar vocea: capcut_final\\doar_vocea\\<nume>_ro.m4a)

Linia de comandă:  python montaj.py --toate            (montaj pentru toate traducerile care nu au încă)
                   python montaj.py <nume_ro.mp4> ...   (doar acestea, refăcute)
                   python montaj.py --subtitrari <ieșire.json> <fișier> ...   (replicile românești, Whisper turbo)"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

APP = Path(os.environ.get("APLICATIE_DIR") or r"D:\Aplicatie")   # aplicația de dublare (pe alt PC: APLICATIE_DIR)
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
import randare  # noqa: E402  (aplicația de dublare: ffmpeg, NVENC, tăierea pe grila cadrelor)

IN_DIR = APP / "capcut_de_tradus"
OUT_DIR = APP / "capcut_traduse"
FINAL_DIR = APP / "capcut_final"
MODES = ("doar_final", "ca_inainte")
# marcajul fișierelor pregătite (comment din metadate, tag_of îl citește): după el știe montajul varianta clipului
TAG = "capcut_pregatit_v2"            # ca_inainte (și toate clipurile pregătite până pe 21 oct 2026)
TAG_FINAL = "capcut_pregatit_v3_doar_final"   # doar_final (același text e în dublare_ui.py, CAPCUT_TAG_FINAL)
MAX_S = 240.0                         # doar_final: ce trece de 4:00 se taie de la final (nicio pauză nu se taie)
END_WIN_S = 10.0                      # dacă la 4:00 se vorbește: termin în ultima pauză dintre cuvinte din ultimele 10 s
MAX_S_VECHI = 270.0                   # ca_inainte: după ce scot toate pauzele, ce trece de 4:30 se taie de la final
MIN_S = 5.5
NOWIN = 0x08000000

# Varianta „ca înainte” (tăierea golurilor). Reglaje măsurate pe 7 voci CapCut și 6 clipuri descărcate (detector de voce + orele cuvintelor date de Whisper):
#   orice pauză de peste ~120 ms rămâne de 60 ms (30 ms lângă fiecare cuvânt: nu sună robotic) — și la montajul final,
#   și la pregătirea dinainte de CapCut (cerut: „absolut toate pauzele”; verificat: 2 cuvinte din 5719 atinse)
FINAL = dict(thr=0.4, min_sil_ms=50, margin=0.03)
PREP = dict(FINAL)
WORD_PROB = 0.35                      # un cuvânt Whisper atât de sigur, pe care detectorul de voce nu-l aude, se păstrează
FADE_S = 0.010
MIN_GAP = 0.06                        # goluri mai scurte de atât nu se taie (nu se aud și ar face imaginea să sară)

_wm = None
_wm_lock = threading.Lock()
SOURCES = OUT_DIR / ".surse"          # <voce>.txt = clipul din gata pe care l-a tradus telefonul (scris de capcut_job.py)


def _game_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq League of Legends.exe", "/NH"], capture_output=True,
                             text=True, timeout=10, creationflags=NOWIN).stdout
        return "League of Legends.exe" in out
    except Exception:  # noqa: BLE001
        return False


def _low() -> bool:
    """În blocul `with randare.low_priority(...)` (ex. cât rulează League of Legends): cu grijă de joc."""
    return bool(getattr(getattr(randare, "_tls", None), "low", False))


def _cf() -> int:
    """Procesele ajutătoare (ffmpeg / ffprobe): fără fereastră; cu prioritate mică în blocul low_priority."""
    return randare._no_window().get("creationflags", NOWIN)


def _threads(normal: int) -> int:
    """Câte fire de procesor: `normal`, sau un sfert din procesor cât e cu grijă de joc."""
    return max(2, (os.cpu_count() or 4) // 4) if _low() else normal




class MontajError(Exception):
    pass


def _ff():
    ff, fp = randare.find_ffmpeg()[:2]
    if not ff:
        raise MontajError("lipsește ffmpeg")
    return ff, fp


def decode(f: Path, sr: int, ch: int) -> np.ndarray:
    """Sunetul (prima pistă) ca int16 [n, ch]."""
    r = subprocess.run([_ff()[0], "-v", "error", "-i", str(f), "-map", "0:a:0", "-ac", str(ch), "-ar", str(sr),
                        "-f", "s16le", "-"], capture_output=True, timeout=1800, creationflags=_cf())
    if r.returncode != 0 or not r.stdout:
        raise MontajError(f"nu pot citi sunetul din {Path(f).name}")
    return np.frombuffer(r.stdout, np.int16).reshape(-1, ch)


def _load_whisper(name, **kw):
    """Modelul Whisper din cache; dacă lipsește (PC nou), îl descarc o dată de pe HuggingFace."""
    from faster_whisper import WhisperModel
    try:
        return WhisperModel(name, local_files_only=True, **kw)
    except Exception:  # noqa: BLE001 - nu e în cache: descarc
        return WhisperModel(name, local_files_only=False, **kw)


def _whisper():
    """Whisper base pe procesor; cu grijă de joc un model separat, cu mai puține fire (ambele ținute minte)."""
    global _wm
    n = _threads(max(2, (os.cpu_count() or 4) // 2))
    with _wm_lock:
        if not isinstance(_wm, dict):
            _wm = {}
        if n not in _wm:
            _wm[n] = _load_whisper("base", device="cpu", compute_type="int8", cpu_threads=n)
        return _wm[n]


def _word_span(w) -> tuple[float, float]:
    """Whisper întinde începutul primului cuvânt de după o pauză peste liniștea dinainte: limitez lungimea
    cuvântului la cât ar putea dura după numărul de litere."""
    s, e = float(w.start), float(w.end)
    est = 0.09 * len(w.word.strip()) + 0.15
    return max(s, e - est), e


def speech_regions(a16: np.ndarray, cfg: dict, use_words: bool = True, language: str | None = None) -> tuple[list, dict]:
    """[(început, sfârșit)] în secunde, unde se aude vorbire: detectorul de voce Silero decide unde sunt pauzele
    (precis), iar un cuvânt găsit de Whisper pe care detectorul nu l-a auzit deloc se păstrează și el. Whisper nu
    decide pauzele (lipește cuvintele între ele peste pauze). Fiecare bucată primește `margin` de fiecare parte."""
    import bisect
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    dur = len(a16) / 16000
    a = a16.astype(np.float32) / 32768 if a16.dtype != np.float32 else a16
    seg = get_speech_timestamps(a, VadOptions(threshold=cfg["thr"], min_silence_duration_ms=cfg["min_sil_ms"],
                                              speech_pad_ms=0))
    m = cfg["margin"]
    vad = [(s["start"] / 16000 - m, s["end"] / 16000 + m) for s in seg]
    regs = list(vad)
    info = {"vad": len(vad), "words": 0, "rescued": 0, "lang": None}
    if use_words:
        try:
            segs, ti = _whisper().transcribe(a, beam_size=5, word_timestamps=True, vad_filter=False,
                                             condition_on_previous_text=False, language=language)
            starts = [s for s, _ in vad]
            for sg in segs:
                for w in sg.words or []:
                    if w.probability < WORD_PROB or w.end <= w.start:
                        continue
                    info["words"] += 1
                    ws, we = _word_span(w)
                    cov, i = 0.0, max(0, bisect.bisect_right(starts, ws) - 1)
                    while i < len(vad) and vad[i][0] < we:
                        cov += max(0.0, min(we, vad[i][1]) - max(ws, vad[i][0]))
                        i += 1
                    if cov < 0.5 * (we - ws):            # detectorul nu l-a auzit: îl păstrez
                        regs.append((ws - m, we + m))
                        info["rescued"] += 1
            info["lang"] = ti.language
        except Exception as ex:  # noqa: BLE001  (fără Whisper rămâne doar detectorul de voce)
            info["words_error"] = str(ex)[:200]
    regs = sorted((max(0.0, s), min(dur, e)) for s, e in regs if e > s)
    merged = []
    for s, e in regs:
        if merged and s - merged[-1][1] < MIN_GAP:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if e - s > 0.03], info


def grid_parts(regions, fps: float, dur: float) -> list:
    """Bucățile păstrate pe grila cadrelor (lărgite spre exterior: nu tai din vorbire), unite dacă se ating."""
    if not fps or fps <= 0:
        fps = 30.0
    out = []
    for s, e in regions:
        a = math.floor(s * fps + 1e-6) / fps
        b = min(dur, math.ceil(e * fps - 1e-6) / fps)
        if out and a <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], b)
        elif b - a >= 1.0 / fps:
            out.append([a, b])
    return [[round(a, 6), round(b, 6)] for a, b in out]


def gate(full: np.ndarray, parts, sr: int) -> np.ndarray:
    """Sunetul doar în bucățile păstrate, cu 10 ms estompare la margini; în rest liniște."""
    out = np.zeros_like(full)
    n = max(1, int(FADE_S * sr))
    for a, b in parts:
        i, j = int(round(a * sr)), min(len(full), int(round(b * sr)))
        if j <= i:
            continue
        seg = full[i:j].astype(np.float32)
        k = min(n, (j - i) // 2)
        if k > 0:
            ramp = (0.5 - 0.5 * np.cos(np.linspace(0, np.pi, k))).astype(np.float32)[:, None]
            seg[:k] *= ramp
            seg[-k:] *= ramp[::-1]
        out[i:j] = np.clip(seg, -32768, 32767).astype(np.int16)
    return out


def write_wav(p: Path, a: np.ndarray, sr: int):
    with wave.open(str(p), "wb") as w:
        w.setnchannels(a.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.ascontiguousarray(a).tobytes())


def _run(args, timeout=3600, cancel=None):
    with tempfile.TemporaryFile() as errf:          # (nu PIPE: un ffmpeg vorbăreț nu se poate bloca)
        pr = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errf,
                              creationflags=_cf())
        t0 = time.time()
        while pr.poll() is None:
            if (cancel is not None and cancel.is_set()) or time.time() - t0 > timeout:
                pr.kill()
                pr.wait()
                raise MontajError("oprit" if cancel is not None and cancel.is_set() else "ffmpeg a durat prea mult")
            time.sleep(0.2)
        if pr.returncode != 0:
            errf.seek(0)
            raise MontajError("ffmpeg: " + errf.read().decode("utf-8", "replace").strip()[-300:])


AAC = ["-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2"]      # sunetul ca înainte: AAC 48 kHz stereo


def _venc(maxrate: str, cpu_only: bool) -> list:
    """Codarea imaginii, în ordinea încercărilor: [(nume, argumente)] — NVENC (placa video), altfel procesor
    (cu grijă de joc: doar un sfert din fire)."""
    cap = ["-maxrate", maxrate, "-bufsize", str(int(maxrate.rstrip("k")) * 2) + "k", "-pix_fmt", "yuv420p"]
    thr = ["-threads", str(_threads(0))] if _low() else []
    return ([] if cpu_only else [("NVENC (placa video)", ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                                                          "-cq", "23", "-b:v", "0", *cap])]) + \
        [("procesor", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", *thr, *cap])]


def _encode_final(src: Path, dst: Path, maxrate: str, cancel=None, tag: str = "", cpu_only: bool = False,
                  t: float | None = None, audio: list | None = None) -> str:
    """Codare finală: aceeași rezoluție, plafon de bitrate (NVENC, altfel procesor); sunetul copiat (sau codat cu
    `audio`); `t` = doar primele t secunde. -> codorul folosit."""
    ff = _ff()[0]
    meta = ["-metadata", f"comment={tag}"] if tag else []
    last = None
    for name, venc in _venc(maxrate, cpu_only):
        try:
            _run([ff, "-v", "error", "-y", "-i", str(src), *(["-t", f"{t:.3f}"] if t else []),
                  "-map", "0:v:0", "-map", "0:a:0?", *venc, *(audio or ["-c:a", "copy"]), *meta,
                  "-movflags", "+faststart", str(dst)], cancel=cancel)
            return name
        except MontajError as e:
            if cancel is not None and cancel.is_set():
                raise
            last = e
    raise last or MontajError("codarea nu a mers")


def _publish(src: Path, dst: Path):
    """Copiez prin .tmp și redenumesc (telefoanele / alte programe nu văd niciodată un fișier pe jumătate)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copyfile(src, tmp)
    for i in range(10):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            time.sleep(0.3 * (i + 1))
    tmp.unlink(missing_ok=True)
    raise MontajError(f"nu pot scrie {dst.name} (e deschis în alt program?)")


def cut_av(video: Path, audio_src: Path, out: Path, cfg: dict, max_s: float | None = None, maxrate: str = "2000k",
           cancel=None, tag: str = "", language: str | None = None, work: Path | None = None) -> dict:
    """Imaginea din `video` + sunetul din `audio_src` (pot fi același fișier), fără golurile din voce, cel mult
    `max_s` secunde (tăiat într-un gol). -> raport."""
    own = work is None
    work = Path(tempfile.mkdtemp(prefix="montaj-")) if own else work
    try:
        info = randare.probe(video)
        vdur = float(info["dur"])
        fps = randare._fps(video) or 30.0
        a16 = decode(audio_src, 16000, 1)[:, 0]
        if cancel is not None and cancel.is_set():
            raise MontajError("oprit")
        adur = len(a16) / 16000
        # exportul CapCut cu videoclipul păstrat are la final ~2 s cu sigla CapCut: vocea poate fi cu până la 5 s mai
        # lungă (folosesc doar cât durează clipul); mai scurtă cu mult = altă potrivire, nu risc
        if audio_src != video and (adur - vdur > 5.0 or vdur - adur > 2.0):
            raise MontajError(f"vocea ({adur:.1f} s) și clipul ({vdur:.1f} s) nu au aceeași lungime")
        dur = min(vdur, adur)
        regions, rinfo = speech_regions(a16, cfg, language=language)
        if not regions:
            raise MontajError("nu aud nicio vorbire în clip")
        parts = grid_parts(regions, fps, dur)
        trimmed = False
        if max_s:
            kept, acc = [], 0.0
            for a, b in parts:
                if acc + (b - a) > max_s:
                    trimmed = True
                    rest = max_s - acc
                    if rest > 20:                    # mult loc rămas: și începutul bucății lungi (altfel ies mult sub limită)
                        kept.append([a, a + math.floor(rest * fps) / fps])
                    break
                kept.append([a, b])
                acc += b - a
            if not kept:                             # o singură bucată de vorbire mai lungă decât max_s
                a, b = parts[0]
                kept, trimmed = [[a, a + math.floor(max_s * fps) / fps]], True
            parts = kept
        after = sum(b - a for a, b in parts)
        # imaginea + sunetul cu liniște în goluri și estompare la margini, apoi doar bucățile păstrate
        full = decode(audio_src, 48000, 2)
        write_wav(work / "voce.wav", gate(full, parts, 48000), 48000)
        ff = _ff()[0]
        _run([ff, "-v", "error", "-y", "-i", str(video), "-i", str(work / "voce.wav"), "-map", "0:v:0", "-map", "1:a:0",
              "-c:v", "copy", "-c:a", "pcm_s16le", "-t", f"{dur:.3f}", str(work / "m.mkv")], cancel=cancel)
        cut_file = work / "cut.mp4"
        cpu_only = _game_running()                 # cât rulează League of Legends: doar procesor (nu iau placa video)
        had, prev = "nvenc" in randare._cache, randare._cache.get("nvenc")
        if cpu_only:
            randare._cache["nvenc"] = False        # doar pentru tăierea asta (randare.keep_parts), apoi pun la loc
        try:
            r = randare.keep_parts(work / "m.mkv", cut_file, parts, cancel=cancel)
        finally:
            if cpu_only:
                if had:
                    randare._cache["nvenc"] = prev
                else:
                    randare._cache.pop("nvenc", None)
        _encode_final(cut_file, work / "final.mp4", maxrate, cancel=cancel, tag=tag, cpu_only=cpu_only)
        got = randare.media_duration(work / "final.mp4")
        if abs(got - after) > 1.0:
            raise MontajError(f"rezultatul are {got:.1f} s în loc de {after:.1f} s")
        _publish(work / "final.mp4", out)
        return {"before": round(dur, 2), "after": round(got, 2), "removed": round(dur - after, 2),
                "cuts": len(parts) - 1, "trimmed": trimmed, "words": rinfo.get("words"),
                "rescued": rinfo.get("rescued"), "lang": rinfo.get("lang"),
                "encoder": r.get("encoder"), "mb": round(out.stat().st_size / 1e6, 1),
                "parts": [[round(a, 3), round(b, 3)] for a, b in parts]}
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- pregătire (înainte de CapCut)
def tag_of(f: Path) -> str:
    try:
        r = subprocess.run([_ff()[1], "-v", "error", "-show_entries", "format_tags=comment", "-of", "csv=p=0", str(f)],
                           capture_output=True, text=True, timeout=60, creationflags=_cf())
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def end_point(src: Path, limit: float = MAX_S) -> float:
    """Unde se termină un clip mai lung de `limit`: chiar la `limit`; dar dacă acolo se aude vorbire, la mijlocul
    ultimei pauze dintre cuvinte din ultimele END_WIN_S secunde (detectorul de voce Silero), ca CapCut să nu
    primească un cuvânt tăiat în două. Fără nicio pauză acolo (sau fără detector) -> exact `limit`."""
    w0 = max(0.0, limit - END_WIN_S)
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        r = subprocess.run([_ff()[0], "-v", "error", "-ss", f"{w0:.3f}", "-t", f"{limit - w0:.3f}", "-i", str(src),
                            "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                           capture_output=True, timeout=300, creationflags=_cf())
        if r.returncode != 0 or not r.stdout:
            return limit
        a = np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768
        seg = get_speech_timestamps(a, VadOptions(threshold=0.4, min_silence_duration_ms=150, speech_pad_ms=30))
    except Exception:  # noqa: BLE001  (fără detector: exact la limită)
        return limit
    spans = [(s["start"] / 16000, s["end"] / 16000) for s in seg]
    rel = limit - w0
    if not spans or spans[-1][1] < rel - 0.05:       # la limită nu se vorbește: tai chiar acolo
        return limit
    gaps = ([(0.0, spans[0][0])] if spans[0][0] > 0 else []) + \
        [(spans[i][1], spans[i + 1][0]) for i in range(len(spans) - 1)]
    gaps = [(a_, b_) for a_, b_ in gaps if b_ - a_ >= 0.08]
    return round(w0 + (gaps[-1][0] + gaps[-1][1]) / 2, 3) if gaps else limit


def mod_of(video: Path) -> str:
    """Varianta cu care a fost pregătit clipul (după marcaj); clipurile fără marcajul nou = ca_inainte."""
    return "doar_final" if tag_of(video) == TAG_FINAL else "ca_inainte"


def prepare(src: Path, out: Path, cancel=None, mod: str = "doar_final") -> dict:
    """Clip descărcat -> pentru CapCut, ~1,5 Mbit/s, aceeași rezoluție, marcat ca pregătit (cu varianta lui).
    doar_final: NETĂIAT (toate pauzele rămân), doar scurtat de la final la cel mult 4:00 (vezi end_point).
    ca_inainte: fără nicio pauză în vorbire, apoi tăiat de la final la 4:30 dacă tot e mai lung."""
    if mod == "ca_inainte":
        rep = cut_av(src, src, out, PREP, max_s=MAX_S_VECHI, maxrate="1500k", cancel=cancel, tag=TAG)
        if rep["after"] < MIN_S:
            out.unlink(missing_ok=True)
            raise MontajError(f"fără goluri are doar {rep['after']:.1f} s (CapCut traduce de la 5 s)")
        return dict(rep, mod=mod)
    dur = float(randare.probe(src)["dur"])
    if dur < MIN_S:
        raise MontajError(f"are doar {dur:.1f} s (CapCut traduce de la 5 s)")
    trimmed = dur > MAX_S + 0.5
    end = end_point(src, MAX_S) if trimmed else dur
    if cancel is not None and cancel.is_set():
        raise MontajError("oprit")
    work = Path(tempfile.mkdtemp(prefix="montaj-"))
    try:
        enc = _encode_final(src, work / "p.mp4", "1500k", cancel=cancel, tag=TAG_FINAL, cpu_only=_game_running(),
                            t=end if trimmed else None, audio=AAC)
        got = randare.media_duration(work / "p.mp4")
        if abs(got - end) > 1.0:
            raise MontajError(f"rezultatul are {got:.1f} s în loc de {end:.1f} s")
        _publish(work / "p.mp4", out)
        return {"before": round(dur, 2), "after": round(got, 2), "trimmed": trimmed, "end": round(end, 3),
                "cuts": 0, "removed": round(max(0.0, dur - got), 2), "encoder": enc,
                "mb": round(out.stat().st_size / 1e6, 1), "mod": "doar_final"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- montaj final (după CapCut)
def find_video(stem: str, voice: Path | None = None) -> Path | None:
    """Clipul pregătit pe care l-a tradus CapCut: întâi ce a notat telefonul (capcut_traduse\\.surse), apoi numele
    exact din gata / partial (<nume>.mp4, sau <nume>_N.mp4 pentru vocea <nume>_ro_N), cel mai apropiat ca durată."""
    import re
    if voice is not None:
        try:
            p = Path((SOURCES / f"{voice.stem}.txt").read_text(encoding="utf-8").strip())
            if p.is_file():
                return p
        except OSError:
            pass
    m = re.fullmatch(r".+?_ro(?:_primele_4m59)?_(\d+)", voice.stem) if voice is not None else None
    names = ([f"{stem}_{m.group(1)}.mp4"] if m else []) + [f"{stem}.mp4"]
    dirs = ("partial", "gata") if voice is not None and "_primele_4m59" in voice.stem else ("gata", "partial")
    cands = [IN_DIR / d / n for n in names for d in dirs if (IN_DIR / d / n).is_file()]
    if len(cands) <= 1 or voice is None:
        return cands[0] if cands else None
    try:
        vd = randare.media_duration(voice)
        return min(cands, key=lambda c: abs(randare.media_duration(c) - vd))
    except Exception:  # noqa: BLE001
        return cands[0]


def glob_escape(s: str) -> str:
    from glob import escape
    return escape(s)


def final_paths(voice: Path) -> tuple[Path, Path, str]:
    """Numele scrise de telefoane: <nume>_ro.mp4, <nume>_ro_primele_4m59.mp4 (+ _2, _3 dacă existau deja)."""
    import re
    name = voice.stem
    m = re.fullmatch(r"(.+?)_ro(?:_primele_4m59)?(?:_\d+)?", name)
    stem = m.group(1) if m else name
    return FINAL_DIR / f"{name}.mp4", FINAL_DIR / "doar_vocea" / f"{name}.m4a", stem


def _audio_dur(f: Path) -> float:
    """Durata pistei de sunet (exportul CapCut: poate diferi puțin de a imaginii); altfel durata fișierului."""
    try:
        r = subprocess.run([_ff()[1], "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration",
                            "-of", "csv=p=0", str(f)], capture_output=True, text=True, timeout=60, creationflags=_cf())
        d = float((r.stdout or "").strip().splitlines()[0])
        if d > 0:
            return d
    except Exception:  # noqa: BLE001
        pass
    return randare.media_duration(f)


def mux_voice(video: Path, voice: Path, out: Path, maxrate: str = "2500k", cancel=None,
              work: Path | None = None) -> dict:
    """Imaginea din `video` (clipul pregătit) + vocea română din `voice`, NETĂIATE (fără tăierea golurilor), cât
    durează cea mai scurtă dintre ele. Imaginea se copiază așa cum e (altfel se recodează). -> raport."""
    own = work is None
    work = Path(tempfile.mkdtemp(prefix="montaj-")) if own else work
    try:
        vdur = float(randare.probe(video)["dur"])
        adur = _audio_dur(voice)
        # exportul CapCut cu videoclipul păstrat are la final ~2 s cu sigla CapCut: vocea poate fi cu până la 5 s mai
        # lungă (folosesc doar cât durează clipul); mai scurtă cu mult = altă potrivire, nu risc
        if adur - vdur > 5.0 or vdur - adur > 2.0:
            raise MontajError(f"vocea ({adur:.1f} s) și clipul ({vdur:.1f} s) nu au aceeași lungime")
        dur = min(vdur, adur)
        ff = _ff()[0]
        dst, last, enc = work / "final.mp4", None, None
        for name, venc in [("copiată (fără recodare)", ["-c:v", "copy"])] + _venc(maxrate, _game_running()):
            try:
                _run([ff, "-v", "error", "-y", "-i", str(video), "-i", str(voice), "-map", "0:v:0", "-map", "1:a:0",
                      "-t", f"{dur:.3f}", *venc, *AAC, "-map_metadata", "-1", "-movflags", "+faststart", str(dst)],
                     cancel=cancel)
                got = randare.media_duration(dst)
                if abs(got - dur) > 1.0:
                    raise MontajError(f"rezultatul are {got:.1f} s în loc de {dur:.1f} s")
                enc = name
                break
            except MontajError as e:
                if cancel is not None and cancel.is_set():
                    raise
                last = e
        if enc is None:
            raise last or MontajError("montajul nu a mers")
        _publish(dst, out)
        return {"before": round(dur, 2), "after": round(got, 2), "removed": 0.0, "cuts": 0, "trimmed": False,
                "voce_s": round(adur, 2), "clip_s": round(vdur, 2), "encoder": enc,
                "mb": round(out.stat().st_size / 1e6, 1), "parts": [[0.0, round(dur, 3)]]}
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)


def make_final(voice: Path, cancel=None, log=print) -> dict:
    """Vocea română de la CapCut + clipul pregătit -> capcut_final\\<nume>_ro.mp4 (+ doar vocea), după varianta cu care
    a fost pregătit clipul: doar_final = fără nicio tăiere; ca_inainte = fără niciun gol în voce."""
    out, out_voice, stem = final_paths(voice)
    video = find_video(stem, voice)
    if video is None:
        raise MontajError(f"nu găsesc clipul {stem}.mp4 în capcut_de_tradus\\gata (imaginea pentru montaj)")
    mod = mod_of(video)
    work = Path(tempfile.mkdtemp(prefix="montaj-"))
    try:
        if mod == "doar_final":
            rep = mux_voice(video, voice, out, maxrate="2500k", cancel=cancel, work=work)
        else:
            rep = cut_av(video, voice, out, FINAL, maxrate="2500k", cancel=cancel, language="ro", work=work)
        rep["mod"] = mod
        ff = _ff()[0]
        _run([ff, "-v", "error", "-y", "-i", str(out), "-map", "0:a:0", "-c:a", "copy", str(work / "v.m4a")],
             cancel=cancel)
        _publish(work / "v.m4a", out_voice)
        rep.update(video=video.name, out=str(out))
        (FINAL_DIR / "rapoarte").mkdir(parents=True, exist_ok=True)
        (FINAL_DIR / "rapoarte" / f"{voice.stem}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                                                   encoding="utf-8")
        if mod == "doar_final":
            log(f"   ✓ montaj {out.name}: {rep['after']:.0f}s, imaginea + vocea română netăiate ({rep['encoder']}, "
                f"{rep['mb']:.0f} MB)")
        else:
            log(f"   ✓ montaj {out.name}: {rep['before']:.0f}s -> {rep['after']:.0f}s ({rep['cuts']} goluri tăiate, "
                f"{rep['mb']:.0f} MB)")
        return rep
    finally:
        shutil.rmtree(work, ignore_errors=True)


def report_path(voice: Path) -> Path:
    return FINAL_DIR / "rapoarte" / f"{voice.stem}.json"


def has_final(v: Path) -> bool:
    """„Făcut” = are raport (scris la sfârșit, după video și voce) mai nou decât vocea: un montaj mutat sau șters după
    postare nu se mai face din nou, dar o voce nouă cu același nume (clip tradus din nou) primește montaj nou."""
    try:
        return report_path(v).stat().st_mtime >= v.stat().st_mtime
    except OSError:
        return False


def pending() -> list[Path]:
    """Traducerile care nu au încă montaj final."""
    return [v for v in sorted(OUT_DIR.glob("*_ro*.mp4")) if not has_final(v)]


def _cleanup_old_temp():
    """Resturi de la un montaj oprit forțat: foldere temporare montaj-* mai vechi de 6 ore și .tmp neterminate."""
    cut = time.time() - 6 * 3600
    try:
        for d in Path(tempfile.gettempdir()).glob("montaj-*"):
            if d.is_dir() and d.stat().st_mtime < cut:
                shutil.rmtree(d, ignore_errors=True)
        for t in list(FINAL_DIR.glob("*.tmp")) + list((FINAL_DIR / "doar_vocea").glob("*.tmp")):
            if t.stat().st_mtime < cut:
                t.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------- subtitrări românești (pentru aplicația de dublare)
WHISPER_TURBO = "deepdml/faster-whisper-large-v3-turbo-ct2"      # același model ca transcrierea aplicației pe PC
HALUCINATII = ("mulțumesc pentru vizionare", "multumesc pentru vizionare", "mulțumim pentru vizionare", "subtitrare",
               "subtitrarea", "abonați-vă", "abonati-va", "nu uitați să dați like")   # doar replici scurte care încep așa


def _turbo():
    """Whisper large-v3-turbo din cache-ul OmniVoice: pe placa video (cu bibliotecile CUDA ale lui torch), altfel
    pe procesor. -> (model, "cuda"/"cpu")."""
    root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "OmniVoice", "hf_cache")
    kw = dict(download_root=root if os.path.isdir(root) else None)
    joc = _game_running()          # cât rulează League of Legends: procesor, jumătate din fire (procesul e „sub normal”)
    if not joc:
        try:
            import torch                             # încarcă bibliotecile CUDA (cublas64_12 / cudnn64_9) pentru ctranslate2
            if not torch.cuda.is_available():
                raise RuntimeError("fără CUDA")
            os.add_dll_directory(os.path.join(os.path.dirname(torch.__file__), "lib"))
            return _load_whisper(WHISPER_TURBO, device="cuda", compute_type="float16", **kw), "cuda"
        except Exception:  # noqa: BLE001
            pass
    n = (os.cpu_count() or 4) // 2 if joc else (os.cpu_count() or 4) - 2
    return _load_whisper(WHISPER_TURBO, device="cpu", compute_type="int8", cpu_threads=max(2, n), **kw), "cpu"


def ro_segments(model, f: Path, dur: float | None = None) -> list:
    """Replicile românești cu orele lor: [{"id", "start", "end", "text"}] (cum le vrea randarea aplicației).
    Replicile lungi le împart la cuvinte (cel mult ~6 s / 110 litere); scot halucinațiile Whisper."""
    import re
    segs, info = model.transcribe(str(f), language="ro", word_timestamps=True, vad_filter=False,
                                  condition_on_previous_text=False, beam_size=5)
    dur = dur or info.duration
    out = []

    def add(a, b, text):
        text = re.sub(r"\s+", " ", text).strip()
        if not text or b - a < 0.05:
            return
        low = text.lower()
        norm = re.sub(r"[^\w\s.-]", "", low).strip()
        if "amara.org" in low or (len(norm) <= 60 and any(norm.startswith(h) for h in HALUCINATII)):
            return                                     # (o replică reală cu „vă mulțumesc” la mijloc rămâne)
        out.append({"id": str(len(out) + 1), "start": round(max(0.0, a), 3), "end": round(min(dur, b), 3),
                    "text": text})
    for sg in segs:
        if sg.no_speech_prob > 0.6 and sg.avg_logprob < -1.0:
            continue
        words = [w for w in (sg.words or []) if w.word.strip()]
        if (sg.end - sg.start <= 6.0 and len(sg.text) <= 110) or not words:
            add(sg.start, sg.end, sg.text)
            continue
        cur, a = [], None
        for w in words:
            if a is None:
                a = w.start
            cur.append(w)
            text = "".join(x.word for x in cur)
            if w.end - a >= 4.5 or len(text) >= 90 or text.rstrip().endswith((".", "?", "!")):
                add(a, w.end, text)
                cur, a = [], None
        if cur:
            add(a, cur[-1].end, "".join(x.word for x in cur))
    return out


def subtitrari_main(out_json: Path, files: list) -> int:
    """Linie de comandă: transcriu fișierele și scriu {fișier: [replici]} în out_json (proces separat: memoria
    plăcii video se eliberează când termin, înainte de randări)."""
    model, dev = _turbo()
    res = {"_device": dev}
    for f in files:
        try:
            res[str(f)] = ro_segments(model, Path(f))
        except Exception as e:  # noqa: BLE001
            res[str(f)] = {"error": str(e)[:300]}
    tmp = Path(str(out_json) + ".tmp")
    tmp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, out_json)
    return 0


def _lock(fd) -> bool:
    import msvcrt
    try:
        os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _unlock(fd):
    import msvcrt
    try:
        os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def main(argv=None):
    """--toate: toate traducerile fără montaj (și cele care apar între timp); altfel doar fișierele date (refăcute)."""
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--subtitrari"]:                # python montaj.py --subtitrari <ieșire.json> <fișier> ...
        return subtitrari_main(Path(argv[1]), argv[2:])
    auto = not argv or argv == ["--toate"]
    explicit = [] if auto else [OUT_DIR / a if not Path(a).is_absolute() else Path(a) for a in argv]
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(FINAL_DIR / ".montaj.lock"), os.O_CREAT | os.O_RDWR)
    ok = bad = 0
    try:
        deadline = time.time() + 3 * 3600
        while not _lock(fd):                   # alt montaj lucrează: aștept (apoi caut și ce a apărut între timp)
            if not auto or time.time() > deadline:
                print("montajul rulează deja (alt proces)", flush=True)
                return 1
            time.sleep(5)
        try:
            _cleanup_old_temp()
            tried: set = set()
            while True:
                if auto:     # doar cele a căror imagine e deja în gata (telefonul o mută imediat după voce)
                    todo = [v for v in pending() if v not in tried and find_video(final_paths(v)[2], v) is not None]
                else:
                    todo = [v for v in explicit if v not in tried]
                if not todo:
                    break
                print(f"montaj final: {len(todo)} traduceri", flush=True)
                for v in todo:
                    tried.add(v)
                    try:
                        with randare.low_priority(_game_running()):   # în timpul jocului: procesor, prioritate mică
                            make_final(v)
                        ok += 1
                    except Exception as e:  # noqa: BLE001
                        bad += 1
                        print(f"   ✗ {v.name}: {e}", flush=True)
                if not auto:
                    break
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
    print(f"gata: {ok} montaje" + (f", {bad} cu eroare" if bad else ""), flush=True)
    return 0 if not bad else 2


if __name__ == "__main__":
    sys.exit(main())
