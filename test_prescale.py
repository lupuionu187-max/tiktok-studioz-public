#!/usr/bin/env python3
"""
test_prescale.py - compara Real-ESRGAN normal cu Real-ESRGAN "prescale" (intrarea micsorata
intai, ca x4 sa ajunga cam la 1.3 x marimea finala: ~4x mai putin de lucru pe placa video).

Ia primele 20 de secunde din primul clip video din folderul clipuri, face pasul de pe placa
video (taiere benzi negre + Real-ESRGAN, exact ce ar face aplicatia, fara stergerea
subtitrarilor) o data normal si o data cu prescale, si le pune unul langa altul.

Ruleaza separat de aplicatie (placa video sa fie libera, altfel timpii nu spun nimic).
Rezultatul: folderul  test_voce\\prescale\\
    1_normal.mp4, 2_prescale.mp4, alaturate.mp4 (stanga normal, dreapta prescale), timpi.txt

Pornire: dublu-click pe "Test prescale.bat" (sau trage un clip peste el).
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dubleaza as engine  # noqa: E402
import randare  # noqa: E402
import sterge_ai  # noqa: E402

SECONDS = 20


def log(m):
    print(time.strftime("[%H:%M:%S] ") + m, flush=True)


def main():
    c = engine.load_settings()
    vids = [v for v in engine.list_videos(c) if not engine.is_audio(v)]
    if len(sys.argv) > 1:
        want = Path(sys.argv[1]).name
        vids = [v for v in vids if v.name == want] or vids
    if not vids:
        raise SystemExit("Nu am clipuri video in folderul clipuri.")
    ff = randare.find_ffmpeg()[0]
    if not ff:
        raise SystemExit("Lipseste ffmpeg.")
    clip = vids[0]
    out_dir = HERE / "test_voce" / "prescale"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"Clip: {clip.name} (primele {SECONDS} s)")

    src = out_dir / "_sursa.mp4"
    r = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(clip), "-t", str(SECONDS),
                        "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(src)],
                       capture_output=True, **randare._no_window())
    if r.returncode != 0 or not src.exists():
        raise SystemExit("Nu am putut taia primele secunde: " + r.stderr.decode("utf-8", "replace")[-300:])

    # same plan as the app: bars cut + Real-ESRGAN up to the 9:16 / 1080p size (no subtitle removal)
    cc = dict(c, iesire_1080=True, sterge_sub_orig=False, upscale_prescale=False)
    if cc.get("upscale_metoda") not in randare.ESR_KINDS:
        cc["upscale_metoda"] = "esrgan"
    opts, info = randare._gpu_plan(src, cc)
    opts.pop("clean", None)
    if "up" not in opts:
        raise SystemExit(f"Clipul e deja destul de mare ({info['w']}x{info['h']}): Real-ESRGAN nu ar rula "
                         "pe el, deci nici prescale nu schimba nimic. Incearca un clip mai mic.")
    up = opts["up"]
    log(f"Real-ESRGAN ({cc['upscale_metoda']}): {info['w']}x{info['h']}"
        + (f", taiat la {opts['crop'][2]}x{opts['crop'][3]}" if opts.get("crop") else "")
        + f" -> {up['w']}x{up['h']}")

    cw, ch = (opts["crop"][2], opts["crop"][3]) if opts.get("crop") else (info["w"], info["h"])
    kp = min(1.0, sterge_ai.PRESCALE * max(up["w"] / (4.0 * cw), up["h"] / (4.0 * ch)))
    same = kp >= 0.95
    if same:
        log("atentie: clipul e mic, prescale nu il micsoreaza (iese la fel); merita incercat pe un clip mai mare")
    else:
        log(f"prescale: Real-ESRGAN primeste ~{int(cw * kp)}x{int(ch * kp)} in loc de {cw}x{ch}")

    runs = []
    for label, name, pre in (("normal", "1_normal.mp4", False), ("prescale", "2_prescale.mp4", True)):
        o = dict(opts, up=dict(up, prescale=pre))
        log(f"=== {label} ===")
        t = time.time()
        det = randare.run_gpu(src, out_dir / name, o,
                              progress=lambda p: print(f"\r   {p:5.1f}%", end="", flush=True))
        dt = time.time() - t
        print()
        log(f"{label}: {dt:.1f} s  ({det})")
        runs.append((label, name, dt, det))

    log("le pun una langa alta...")
    side = out_dir / "alaturate.mp4"
    r = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(out_dir / runs[0][1]),
                        "-i", str(out_dir / runs[1][1]), "-filter_complex", "[0:v][1:v]hstack=inputs=2[v]",
                        "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                        "-pix_fmt", "yuv420p", "-c:a", "copy", str(side)],
                       capture_output=True, **randare._no_window())
    if r.returncode != 0:
        log("alaturarea nu a mers: " + r.stderr.decode("utf-8", "replace")[-300:])
    src.unlink(missing_ok=True)

    t0, t1 = runs[0][2], runs[1][2]
    lines = [f"Clip: {clip.name} (primele {SECONDS} s), {info['w']}x{info['h']} -> {up['w']}x{up['h']}, "
             f"{cc['upscale_metoda']}", ""]
    for label, name, dt, det in runs:
        lines.append(f"  {name:<16} {label:<9} {dt:6.1f} s   {det}")
    lines += ["", f"prescale a durat {t1 / max(0.01, t0) * 100:.0f}% din timpul normal "
                  f"(include pornirea modelului, ~cateva secunde la fiecare).",
              "", "alaturate.mp4: STANGA = normal, DREAPTA = prescale."]
    if same:
        lines += ["", "ATENTIE: clipul e prea mic ca prescale sa schimbe ceva (cele doua ies la fel)."]
    else:
        lines += ["", f"prescale: Real-ESRGAN a primit ~{int(cw * kp)}x{int(ch * kp)} in loc de {cw}x{ch}."]
    lines += ["Daca nu vezi diferenta, spune-mi: pun prescale pornit (setarea upscale_prescale)."]
    (out_dir / "timpi.txt").write_text("\n".join(lines), encoding="utf-8")
    log(f"Gata. Vezi {out_dir} (alaturate.mp4 + timpi.txt)")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        log(str(e))
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        log(f"EROARE: {e}")
