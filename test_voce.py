#!/usr/bin/env python3
"""
test_voce.py - test de accent, separat de aplicatie (nu o atinge cat lucreaza).

Ia un clip din folderul clipuri, alege din el o mostra de voce (vorbitorul rus) si
genereaza ACELEASI propozitii romanesti in mai multe feluri, ca sa le asculti una
langa alta:
  A_clona_din_rusa      - cum face aplicatia acum (vocea clonata direct din rusa)
  B_mostra_romaneasca   - clona din prima replica romaneasca generata (scade accentul)
  C_voce_implicita      - vocea romaneasca a modelului, fara clona (accent curat, alta voce)
  D_voce_fixa           - doar daca pui un fisier "voce_romana.wav/mp3" langa aplicatie:
                          vocea aia romaneasca reala, ca mostra

Foloseste VoiceStudio de pe un GPU inchiriat pornit (sau de pe PC daca nu e niciunul).
Rezultatul: folderul  test_voce\\<clip>\\  cu cate un .mp3 pe varianta.

Pornire:  python test_voce.py ["nume clip.mp4"]   (sau dublu-click pe "Test voce.bat")
"""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import randare  # noqa: E402

LINES = [
    "Nu mi-am imaginat niciodată că o să ajung aici, dar uite că viața te surprinde.",
    "Mama mi-a spus să nu am încredere în nimeni, mai ales în cei care zâmbesc prea mult.",
    "Ce faci aici atât de târziu? Toată lumea a plecat deja acasă.",
    "Dacă afli adevărul, nimic nu va mai fi la fel ca înainte.",
    "Am muncit trei ani pentru casa asta și nu o să renunț la ea așa ușor.",
]
STEPS, CFG, SPEED = 64, 1.5, 1.0


def log(m):
    print(time.strftime("[%H:%M:%S] ") + m, flush=True)


def pick_machine(c):
    rented = [m for m in c.get("masini", []) if m.get("activ") and m.get("adresa")
              and "proxy.runpod.net" in m.get("adresa", "")]
    local = [m for m in c.get("masini", []) if m.get("id") == "local" and m.get("adresa")]
    for m in rented + local:
        s = requests.Session()
        if m.get("cheie"):
            s.headers["Authorization"] = f"Bearer {m['cheie']}"
        try:
            if s.get(m["adresa"].rstrip("/") + "/health", timeout=20).ok:
                return m, s
        except requests.RequestException:
            pass
    raise SystemExit("Nu gasesc niciun VoiceStudio pornit (nici GPU inchiriat, nici pe PC).")


def ff():
    f = randare.find_ffmpeg()[0]
    if not f:
        raise SystemExit("Lipseste ffmpeg.")
    return f


def to_wav(src: Path, start=None, dur=None, rate=24000) -> bytes:
    args = [ff(), "-v", "error"]
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(src)]
    if dur is not None:
        args += ["-t", f"{dur:.3f}"]
    args += ["-vn", "-ac", "1", "-ar", str(rate), "-f", "wav", "-"]
    return subprocess.run(args, capture_output=True, check=True).stdout


def transcribe(s, base, wav):
    r = s.post(f"{base}/v1/audio/transcriptions", files={"file": ("a.wav", wav, "audio/wav")},
               data={"model": "whisper-1", "response_format": "verbose_json"}, timeout=(15, 600))
    r.raise_for_status()
    return r.json()


def choose_reference(segs):
    """6-12 s of continuous speech (consecutive segments, small gaps)."""
    best = None
    for i in range(len(segs)):
        a = segs[i]["start"]
        text = []
        for j in range(i, len(segs)):
            if j > i and segs[j]["start"] - segs[j - 1]["end"] > 0.6:
                break
            text.append(segs[j]["text"].strip())
            dur = segs[j]["end"] - a
            if 6 <= dur <= 12:
                cand = (a, dur, " ".join(text))
                if best is None or abs(dur - 9) < abs(best[1] - 9):
                    best = cand
            if dur > 12:
                break
    if best is None and segs:
        sg = max(segs, key=lambda x: x["end"] - x["start"])
        best = (sg["start"], min(12.0, sg["end"] - sg["start"]), sg["text"].strip())
    return best


def make_profile(s, base, wav, text, label, made):
    r = s.post(f"{base}/profiles", files={"ref_audio": ("mostra.wav", wav, "audio/wav")},
               data={"name": f"test-voce-{label}-{uuid.uuid4().hex[:6]}", "ref_text": text,
                     "language": "Romanian", "kind": "clone", "seed": "12345"}, timeout=(15, 180))
    r.raise_for_status()
    pid = r.json()["id"]
    made.append(pid)
    return pid


def speak(s, base, text, voice):
    body = {"model": "omnivoice", "input": text, "voice": voice, "language": "ro",
            "response_format": "wav", "num_step": STEPS, "guidance_scale": CFG, "speed": SPEED, "seed": 12345}
    for attempt in range(3):
        try:
            r = s.post(f"{base}/v1/audio/speech", json=body, timeout=(15, 600))
            if r.status_code < 400:
                return pcm16(r.content)
            err = f"HTTP {r.status_code} {r.text[:200]}"
        except requests.RequestException as e:
            err = str(e)
        log(f"   reincerc ({err})")
        time.sleep(10)
    raise RuntimeError(err)


def pcm16(w: bytes) -> bytes:
    """Any wav (float32 too) -> 16-bit mono 24 kHz, so the takes can be joined."""
    return subprocess.run([ff(), "-v", "error", "-i", "-", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le",
                           "-f", "wav", "-"], input=w, capture_output=True, check=True).stdout


def join_wavs(wavs, gap=0.45):
    """All takes one after the other (same format), with a short silence between."""
    out, params = io.BytesIO(), None
    frames = []
    for w in wavs:
        with wave.open(io.BytesIO(w)) as x:
            p = (x.getnchannels(), x.getsampwidth(), x.getframerate())
            data = x.readframes(x.getnframes())
        if params and p != params:
            continue
        params = p
        frames.append(data)
        frames.append(b"\0" * int(p[2] * gap) * p[0] * p[1])
    with wave.open(out, "wb") as o:
        o.setnchannels(params[0])
        o.setsampwidth(params[1])
        o.setframerate(params[2])
        o.writeframes(b"".join(frames))
    return out.getvalue()


def save_mp3(wav: bytes, dest: Path):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as t:
        t.write(wav)
    try:
        subprocess.run([ff(), "-v", "error", "-y", "-i", t.name, "-c:a", "libmp3lame", "-b:a", "192k", str(dest)],
                       check=True)
    finally:
        Path(t.name).unlink(missing_ok=True)


def main():
    c = json.loads((HERE / "setari.json").read_text(encoding="utf-8"))
    clips_dir = Path(c.get("folder_clipuri") or "clipuri")
    clips_dir = clips_dir if clips_dir.is_absolute() else HERE / clips_dir
    vids = sorted(p for p in clips_dir.iterdir() if p.suffix.lower() in (".mp4", ".mkv", ".mov", ".webm"))
    if len(sys.argv) > 1:
        vids = [p for p in vids if p.name == sys.argv[1]] or vids
    if not vids:
        raise SystemExit("Nu am clipuri in folderul clipuri.")
    clip = vids[0]
    m, s = pick_machine(c)
    base = m["adresa"].rstrip("/")
    out = HERE / "test_voce" / clip.stem
    out.mkdir(parents=True, exist_ok=True)
    log(f"Clip: {clip.name} | VoiceStudio: {m.get('nume')}")

    log("Transcriu primul minut ca sa aleg o mostra de voce...")
    first = to_wav(clip, 0, 90, 16000)
    segs = transcribe(s, base, first).get("segments") or []
    ref = choose_reference(segs)
    if not ref:
        raise SystemExit("Nu am gasit vorbire in primele 90 de secunde ale clipului.")
    a, dur, text = ref
    ref_wav = to_wav(clip, a, dur, 24000)
    save_mp3(ref_wav, out / "0_mostra_originala_rusa.mp3")
    log(f"Mostra: {a:.1f}s, {dur:.1f}s: {text[:80]}")

    made = []
    results = {}
    try:
        log("A) clona direct din rusa (ca acum)...")
        pa = make_profile(s, base, ref_wav, text, "A", made)
        a_takes = [speak(s, base, ln, pa) for ln in LINES]
        results["A_clona_din_rusa"] = a_takes

        log("B) mostra romaneasca (clona din primele replici romanesti)...")
        b_ref = join_wavs(a_takes[:2], gap=0.3)
        pb = make_profile(s, base, b_ref, " ".join(LINES[:2]), "B", made)
        results["B_mostra_romaneasca"] = [speak(s, base, ln, pb) for ln in LINES]

        log("C) vocea romaneasca a modelului, fara clona...")
        results["C_voce_implicita"] = [speak(s, base, ln, "default") for ln in LINES]

        fixed = next((HERE / n for n in ("voce_romana.wav", "voce_romana.mp3", "voce_romana.m4a")
                      if (HERE / n).exists()), None)
        if fixed:
            log(f"D) voce fixa din {fixed.name}...")
            fw = to_wav(fixed, 0, 15, 24000)
            ft = (transcribe(s, base, to_wav(fixed, 0, 15, 16000)).get("text") or "").strip()
            pd = make_profile(s, base, fw, ft, "D", made)
            results["D_voce_fixa"] = [speak(s, base, ln, pd) for ln in LINES]
        else:
            log("D) sar peste: pune un fisier voce_romana.wav (10-15 s, un vorbitor roman) langa aplicatie.")

        for name, takes in results.items():
            save_mp3(join_wavs(takes), out / f"{name}.mp3")
        (out / "propozitii.txt").write_text("\n".join(LINES), encoding="utf-8")
    finally:
        for pid in made:
            try:
                s.delete(f"{base}/profiles/{pid}", timeout=30)
            except requests.RequestException:
                pass
    log(f"Gata. Asculta fisierele din: {out}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        log(str(e))
    except Exception as e:  # noqa: BLE001
        log(f"EROARE: {e}")
