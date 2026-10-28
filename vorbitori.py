#!/usr/bin/env python3
"""
vorbitori.py JOBDIR  -  who speaks when (pyannote speaker-diarization-community-1).

Job folder: job.json + the clip's sound. job.json:
  {"audio": "audio.wav", "num_speakers": 0, "vocals": true}
Writes out/vorbitori.json:
  {"variants": {"brut": {...}, "voci": {...}}}   one per sound diarized:
     "turns":     [[start, end, speaker], ...]   (overlapping speech kept)
     "exclusive": [[start, end, speaker], ...]   (one speaker at a time: the one to transcribe)
     "speakers":  how many, "secs": how long it took
  "brut" = the clip's sound as it is, "voci" = the voices separated from the music first (when the
  voice tools of voce_noua.py are installed on this machine).

Runs on the rented GPU (next to VoiceStudio, through the video worker) or on the PC. Its own small
Python (venv_vorbitori, pyannote.audio 4 on the machine's own PyTorch) is made on first use.
The model is gated on Hugging Face: the token comes from HF_TOKEN (the app gives it to the GPU).
Lines: "   ..." progress for the log, "PROGRES n", "REZULTAT {json}" at the end, "EROARE ..." on failure.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
WIN = sys.platform.startswith("win")
BASE = Path(os.environ.get("LUCRATOR_DIR") or (HERE / "unelte_voce" if WIN else "/workspace/lucrator"))
VENV = BASE / "venv_vorbitori"
HF_DIR = BASE / "hf"
MODEL = "pyannote/speaker-diarization-community-1"
TOOLS_VERSION = "1"
VOICE_VENV = BASE / "venv_voce"                      # voce_noua.py's tools: the voice separator
SEP_MODELS = BASE / "modele_separare"
SEP_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"

RUNNER = r'''
import json, os, sys, time
import numpy as np
import soundfile as sf
import torch
from pyannote.audio import Pipeline

files, cache, kw = json.loads(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
t0 = time.time()
pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1",
                                token=os.environ.get("HF_TOKEN") or None, cache_dir=cache)
if pipe is None:
    raise SystemExit("modelul nu s-a incarcat (token / conditii neacceptate pe Hugging Face?)")
dev = "cuda" if torch.cuda.is_available() else "cpu"
pipe.to(torch.device(dev))
print("LOG model incarcat pe %s in %.0f s" % (dev, time.time() - t0), flush=True)


def rows(ann):
    return [[round(s.start, 3), round(s.end, 3), str(lab)] for s, _, lab in ann.itertracks(yield_label=True)]


res = {}
for name, path in files.items():
    a, sr = sf.read(path, dtype="float32", always_2d=True)
    w = torch.from_numpy(np.ascontiguousarray(a.mean(axis=1))).unsqueeze(0)
    t1 = time.time()
    out = pipe({"waveform": w, "sample_rate": sr}, **kw)
    reg = getattr(out, "speaker_diarization", out)
    exc = getattr(out, "exclusive_speaker_diarization", None)
    res[name] = {"turns": rows(reg), "exclusive": rows(exc) if exc is not None else rows(reg),
                 "speakers": len(reg.labels()), "secs": round(time.time() - t1, 1)}
    print("LOG %s: %d vorbitori in %.1f s" % (name, res[name]["speakers"], res[name]["secs"]), flush=True)
print("JSON " + json.dumps(res), flush=True)
'''


def say(m):
    print(m, flush=True)


def prog(p):
    print(f"PROGRES {p:.1f}", flush=True)


def sh(cmd, timeout=3600, env=None):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"{str(cmd)[:100]}: " + (r.stderr or r.stdout).decode("utf-8", "replace")[-800:])
    return r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")


def venv_py(v):
    return v / ("Scripts/python.exe" if WIN else "bin/python")


def ffmpeg():
    try:
        sys.path.insert(0, str(HERE))
        import randare
        ff = randare.find_ffmpeg()[0]
        if ff:
            return ff
    except Exception:  # noqa: BLE001
        pass
    for c in (str(BASE / "app" / "ffmpeg" / "bin" / "ffmpeg"), shutil.which("ffmpeg")):
        if c and os.path.isfile(c):
            return c
    raise RuntimeError("lipseste ffmpeg")


def env_tools():
    return dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", HF_HOME=str(HF_DIR),
                HF_HUB_ENABLE_HF_TRANSFER="0")


def setup():
    """venv_vorbitori: pyannote.audio 4 on this machine's PyTorch (kept: never upgraded by pip)."""
    py = venv_py(VENV)
    mark = VENV / ".gata"
    try:
        if mark.read_text().strip() == "ok" + TOOLS_VERSION:
            return py
    except OSError:
        pass
    BASE.mkdir(parents=True, exist_ok=True)
    lock = BASE / "venv_vorbitori.lock"
    t0 = time.time()
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if mark.read_text().strip() == "ok" + TOOLS_VERSION:
                    return py
            except OSError:
                pass
            try:
                if time.time() - lock.stat().st_mtime > 1500:
                    lock.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.time() - t0 > 1800:
                raise RuntimeError("instalarea uneltelor de vorbitori dureaza prea mult")
            time.sleep(3)
    try:
        say("   instalez detectarea vorbitorilor (pyannote community-1, o singura data, cateva minute)...")
        import torch
        import torchaudio
        if not py.exists():
            sh([sys.executable, "-m", "venv", "--system-site-packages", str(VENV)])
        cons = BASE / "constraints_vorbitori.txt"
        lines = [f"torch=={torch.__version__}", f"torchaudio=={torchaudio.__version__}"]
        try:
            import torchvision
            lines.append(f"torchvision=={torchvision.__version__}")
        except Exception:  # noqa: BLE001
            pass
        cons.write_text("\n".join(lines) + "\n")
        sh([str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-c", str(cons),
            "pyannote.audio>=4.0,<5", "soundfile"], timeout=2400, env=env_tools())
        mark.write_text("ok" + TOOLS_VERSION)
        say("   uneltele de vorbitori sunt gata")
        return py
    finally:
        os.close(fd)
        try:
            lock.unlink()
        except OSError:
            pass


def separate(ff, src, work):
    """The voices without the music (the voice separator voce_noua.py installed), or None."""
    vpy = venv_py(VOICE_VENV)
    if not vpy.exists() or not (SEP_MODELS / SEP_MODEL).exists():
        say("   uneltele de separare a vocii nu sunt (inca) pe masina asta: testez doar sunetul brut")
        return None
    out = work / "sep"
    out.mkdir(exist_ok=True)
    code = ("import sys, os\n"
            "from audio_separator.separator import Separator\n"
            "s = Separator(output_dir=sys.argv[2], output_format='WAV', model_file_dir=sys.argv[3])\n"
            "s.load_model(model_filename=sys.argv[4])\n"
            "print('\\n'.join(s.separate(sys.argv[1])))\n")
    try:
        sh([str(vpy), "-c", code, str(src), str(out), str(SEP_MODELS), SEP_MODEL], timeout=1800, env=env_tools())
    except Exception as e:  # noqa: BLE001
        say(f"   separarea vocii nu a mers ({str(e)[:160]}); testez doar sunetul brut")
        return None
    got = sorted(f for f in out.glob("*.wav") if "vocal" in f.name.lower() and "instrumental" not in f.name.lower())
    if not got:
        return None
    voc = work / "voci_16k.wav"
    sh([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(got[0]), "-ac", "1", "-ar", "16000", str(voc)])
    return voc


def main(jd):
    jd = Path(jd)
    out_dir, work = jd / "out", jd / "work"
    out_dir.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    job = json.loads((jd / "job.json").read_text(encoding="utf-8"))
    if not os.environ.get("HF_TOKEN"):
        raise RuntimeError("masina asta nu are tokenul Hugging Face (HF_TOKEN): porneste un GPU nou din aplicatie")
    ff = ffmpeg()
    prog(2)
    py = setup()
    prog(30)
    raw = work / "brut_16k.wav"
    sh([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(jd / job["audio"]), "-vn", "-ac", "1",
        "-ar", "16000", str(raw)])
    files = {"brut": str(raw)}
    if job.get("vocals", True):
        say("   separ vocile de muzica...")
        voc = separate(ff, jd / job["audio"], work)
        if voc:
            files["voci"] = str(voc)
    prog(55)
    kw = {}
    if int(job.get("num_speakers") or 0) > 0:
        kw["num_speakers"] = int(job["num_speakers"])
    runner = work / "vorbitori_runner.py"
    runner.write_text(RUNNER, encoding="utf-8")
    say("   detectez vorbitorii (community-1)...")
    HF_DIR.mkdir(parents=True, exist_ok=True)
    errf = work / "runner_err.txt"
    with open(errf, "w", encoding="utf-8") as ef:
        p = subprocess.Popen([str(py), str(runner), json.dumps(files), str(HF_DIR), json.dumps(kw)],
                             stdout=subprocess.PIPE, stderr=ef, env=env_tools(), text=True,
                             encoding="utf-8", errors="replace")
        result = None
        for ln in p.stdout:
            ln = ln.rstrip()
            if ln.startswith("LOG "):
                say("   " + ln[4:])
            elif ln.startswith("JSON "):
                result = json.loads(ln[5:])
        p.wait()
    err = errf.read_text(encoding="utf-8", errors="replace")
    if p.returncode != 0 or result is None:
        tail = [x for x in err.splitlines() if x.strip()][-6:]
        raise RuntimeError("detectarea vorbitorilor a esuat: " + " | ".join(tail)[-700:])
    (out_dir / "vorbitori.json").write_text(json.dumps({"model": MODEL, "variants": result}, ensure_ascii=False),
                                            encoding="utf-8")
    prog(100)
    print("REZULTAT " + json.dumps({"ok": True, "speakers": {k: v["speakers"] for k, v in result.items()}}),
          flush=True)


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--setup":
            setup()
        else:
            main(sys.argv[1])
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        print("EROARE " + (str(e) or e.__class__.__name__)[:600], flush=True)
        sys.exit(1)
