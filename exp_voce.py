"""
exp_voce.py - runs on the rented GPU (sent by test_voce_curata.py). A/B test for a cleaner,
more faithful Romanian voice, variants 2 and 3 (variant 1 = the app's current dub, made on the PC):

  2. clean clone: the original voices separated from music/noise (RoFormer), the clearest 10-20 s
     of each speaker as the reference, VoiceStudio clones from that.
  3. clean clone + Seed-VC: the lines of variant 2 get the original speaker's timbre put on them
     once more by Seed-VC (from the same clean reference): closer to the original voice.
Only the voices from the clip itself are used.

Lines are never slowed down; a line longer than its slot is sped up (tone kept) up to 1.35x.
Job folder: job.json {segments, steps, cfg, speed}, audio.wav (original sound of the excerpt).
Results in out/.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
import uuid
from pathlib import Path

JD = Path(sys.argv[1])
OUT = JD / "out"
OUT.mkdir(exist_ok=True)
WORK = JD / "work"
WORK.mkdir(exist_ok=True)
HERE = Path(__file__).resolve().parent
WIN = sys.platform.startswith("win")
# on the rented GPU: next to the worker; on this PC: a folder next to the app
BASE = Path(os.environ.get("LUCRATOR_DIR") or (HERE / "unelte_test" if WIN else "/workspace/lucrator"))
BASE.mkdir(parents=True, exist_ok=True)
VENV = BASE / "venv_exp"
SEEDVC = BASE / "seed-vc"
VS_URL = os.environ.get("VS_URL", "http://127.0.0.1:3900").rstrip("/")
SR = 24000
MAX_UP = 1.35


def say(m):
    print(m, flush=True)


def prog(p):
    print(f"PROGRES {p:.1f}", flush=True)


def sh(cmd, timeout=3600, env=None, cwd=None):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, timeout=timeout,
                       env=env, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"{str(cmd)[:120]}: " + (r.stderr or r.stdout).decode("utf-8", "replace")[-800:])
    return r.stdout.decode("utf-8", "replace")


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


FF = ffmpeg()


def to_wav(src, dst, sr=SR):
    sh([FF, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", str(sr), str(dst)])


def load(path):
    import numpy as np
    import soundfile as sf
    a, sr = sf.read(str(path), dtype="float32", always_2d=True)
    a = a.mean(axis=1)
    if sr != SR:
        tmp = WORK / f"rs_{uuid.uuid4().hex[:6]}.wav"
        to_wav(path, tmp)
        a, _ = sf.read(str(tmp), dtype="float32")
    return np.asarray(a, dtype="float32")


def save(path, a):
    import soundfile as sf
    sf.write(str(path), a, SR, subtype="PCM_16")


# ---------------------------------------------------------------- tools (once per GPU)
def setup():
    py = VENV / ("Scripts/python.exe" if WIN else "bin/python")
    marker = VENV / ".gata"
    if marker.exists():
        return py
    say("pregatesc uneltele pe GPU (o singura data, cateva minute)...")
    if not py.exists():
        sh([sys.executable, "-m", "venv", "--system-site-packages", str(VENV)])
    try:
        import torch  # the installed torch (with CUDA): kept as is inside the venv
        import torchaudio
    except ImportError:
        raise RuntimeError("lipseste PyTorch cu placa video: in aplicatie, Setari -> Imagine si export -> "
                           "stergere subtitrari AI -> butonul de instalare (pune PyTorch pentru placa), "
                           "apoi porneste testul din nou")
    if not torch.cuda.is_available():
        raise RuntimeError("PyTorch nu vede placa video (NVIDIA); testul ar merge foarte incet pe procesor")
    cons = WORK / "constraints.txt"
    cons.write_text(f"torch=={torch.__version__}\ntorchaudio=={torchaudio.__version__}\n")
    pip = [str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-c", str(cons)]
    sh(pip + ["audio-separator[gpu]", "soundfile"], timeout=2400)
    if not (SEEDVC / "inference.py").exists():
        shutil.rmtree(SEEDVC, ignore_errors=True)
        import io
        import zipfile
        say("descarc Seed-VC...")
        with urllib.request.urlopen("https://codeload.github.com/Plachtaa/seed-vc/zip/refs/heads/main",
                                    timeout=300) as r:
            z = zipfile.ZipFile(io.BytesIO(r.read()))
        tmpd = BASE / "seed-vc-zip"
        shutil.rmtree(tmpd, ignore_errors=True)
        z.extractall(tmpd)
        shutil.move(str(next(p for p in tmpd.iterdir() if p.is_dir())), str(SEEDVC))
        shutil.rmtree(tmpd, ignore_errors=True)
    req = (SEEDVC / "requirements.txt").read_text(encoding="utf-8", errors="replace").splitlines()
    skip = ("torch", "torchvision", "torchaudio", "gradio", "freesimplegui", "sounddevice", "--")
    keep = [l for l in req if l.strip() and not l.strip().startswith("#")
            and not l.strip().lower().startswith(skip)]
    (WORK / "req_seedvc.txt").write_text("\n".join(keep) + "\n")
    sh(pip + ["-r", str(WORK / "req_seedvc.txt")], timeout=2400)
    marker.write_text("ok")
    return py


# ---------------------------------------------------------------- 1. separation
def separate(py, audio):
    say("separ vocile de muzica si zgomot (RoFormer)...")
    outd = WORK / "sep"
    outd.mkdir(exist_ok=True)
    code = r'''
import sys
from audio_separator.separator import Separator
src, outd = sys.argv[1], sys.argv[2]
for model in ("model_bs_roformer_ep_317_sdr_12.9755.ckpt", "mel_band_roformer_kim_ft_unwa.ckpt", None):
    try:
        s = Separator(output_dir=outd, output_format="WAV", model_file_dir=outd + "/models")
        s.load_model(model_filename=model) if model else s.load_model()
        files = s.separate(src)
        print("FILES", "|".join(files))
        break
    except Exception as e:
        print("model", model, "nu merge:", e)
'''
    log = sh([str(py), "-c", code, str(audio), str(outd)], timeout=1800)
    voc = [p for p in outd.glob("*.wav") if "vocal" in p.name.lower() and "instrum" not in p.name.lower()]
    if not voc:
        raise RuntimeError("separarea nu a dat vocile: " + log[-400:])
    return voc[0]


# ---------------------------------------------------------------- 2. clean references
def references(vocals, segs):
    import numpy as np
    a = load(vocals)
    by = {}
    for s in segs:
        by.setdefault(s.get("speaker_id") or "vorbitor", []).append(s)
    refs = {}
    for spk, lst in by.items():
        cands = []
        for s in lst:
            i0, i1 = int(max(0, s["start"] - 0.05) * SR), int((s["end"] + 0.05) * SR)
            piece = a[i0:i1]
            if len(piece) < SR * 1.0:
                continue
            rms = float(np.sqrt(np.mean(piece ** 2)) + 1e-9)
            cands.append((len(piece) / SR, rms, piece, s.get("text_original") or s.get("text") or ""))
        if not cands:
            continue
        loud = sorted(c[1] for c in cands)
        floor = loud[len(loud) // 4] * 0.5                    # skip the faint ones (far / mumbled)
        cands = sorted([c for c in cands if c[1] >= floor], key=lambda c: -c[0])
        parts, texts, tot = [], [], 0.0
        gap = np.zeros(int(SR * 0.25), dtype="float32")
        for dur, _, piece, txt in cands:
            if tot >= 12.0 or (tot and tot + dur > 20.0):
                continue
            parts += [piece, gap]
            texts.append(txt)
            tot += dur
        ref = np.concatenate(parts)
        ref = ref / (np.max(np.abs(ref)) + 1e-6) * 0.9
        path = OUT / f"mostra_{safe(spk)}.wav"
        save(path, ref)
        refs[spk] = (path, " ".join(texts))
        say(f"   mostra curata pentru {spk}: {tot:.1f}s din {len(lst)} replici")
    return refs


def safe(x):
    return "".join(ch if ch.isalnum() else "_" for ch in str(x))[:30]


# ---------------------------------------------------------------- VoiceStudio on this machine
def vs_generate(text, ref_wav, ref_text, spec, dst):
    key = os.environ.get("OMNIVOICE_API_KEY", "")
    boundary = uuid.uuid4().hex
    fields = {"text": text, "language": "Romanian", "ref_text": ref_text or "", "num_step": str(spec["steps"]),
              "guidance_scale": str(spec["cfg"]), "speed": str(spec["speed"])}
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode("utf-8")
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"ref_audio\"; filename=\"ref.wav\"\r\n"
             "Content-Type: audio/wav\r\n\r\n").encode() + Path(ref_wav).read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    hdr = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if key:
        hdr["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(VS_URL + "/generate", data=body, method="POST", headers=hdr)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                Path(dst).write_bytes(r.read())
            return
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                raise RuntimeError(f"VoiceStudio /generate: {e}")
            time.sleep(5)


# ---------------------------------------------------------------- timeline
def place(lines, total):
    """lines: [(start, end, audio)] -> one track; never slowed down, sped up (tone kept) to fit."""
    import numpy as np
    out = np.zeros(int((total + 30) * SR), dtype="float32")
    for st, en, a in lines:
        slot = max(0.3, en - st)
        dur = len(a) / SR
        if dur > slot * 1.02:
            f = min(MAX_UP, dur / slot)
            src, dst = WORK / f"t_{uuid.uuid4().hex[:6]}.wav", WORK / f"u_{uuid.uuid4().hex[:6]}.wav"
            save(src, a)
            sh([FF, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-af", f"atempo={f:.4f}",
                "-ar", str(SR), "-ac", "1", str(dst)])
            a = load(dst)
        i = int(st * SR)
        out[i:i + len(a)] += a[: max(0, len(out) - i)]
    end = max([int(st * SR) + len(a) for st, _, a in lines] + [int(total * SR)])
    return out[:end]


def master(src_wav, dst_mp3):
    sh([FF, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src_wav), "-af",
        "highpass=f=70,deesser=i=0.4,acompressor=threshold=-20dB:ratio=3:attack=5:release=80,"
        "loudnorm=I=-16:TP=-1.5:LRA=9", "-ar", "44100", "-b:a", "192k", str(dst_mp3)])


# ---------------------------------------------------------------- 3. Seed-VC
def seedvc(py, src, target, dst):
    outd = WORK / f"svc_{uuid.uuid4().hex[:6]}"
    outd.mkdir()
    env = dict(os.environ, HF_HUB_ENABLE_HF_TRANSFER="0")
    sh([str(py), "inference.py", "--source", str(src), "--target", str(target), "--output", str(outd),
        "--diffusion-steps", "30", "--length-adjust", "1.0", "--inference-cfg-rate", "0.7"],
       timeout=1800, env=env, cwd=str(SEEDVC))
    wavs = sorted(outd.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    if not wavs:
        raise RuntimeError("Seed-VC nu a scos nimic")
    shutil.move(str(wavs[-1]), str(dst))


def main():
    import numpy as np
    spec = json.loads((JD / "job.json").read_text(encoding="utf-8"))
    segs = [s for s in spec["segments"] if (s.get("text") or "").strip()]
    audio = WORK / "orig.wav"
    to_wav(JD / "audio.wav", audio)
    total = len(load(audio)) / SR
    py = setup()
    prog(10)
    vocals = separate(py, audio)
    save(OUT / "original_doar_vocea.wav", load(vocals))
    prog(20)
    refs = references(vocals, segs)
    if not refs:
        raise RuntimeError("nu am gasit replici destul de lungi pentru mostre")
    fallback = next(iter(refs.values()))

    say("varianta 2: clona din mostra curata...")
    v2, by = [], {}
    for k, s in enumerate(segs):
        ref, rtext = refs.get(s.get("speaker_id") or "vorbitor", fallback)
        dst = WORK / f"v2_{k:03d}.wav"
        vs_generate(s["text"], ref, rtext, spec, dst)
        a = load(dst)
        v2.append((s["start"], s["end"], a))
        by.setdefault(s.get("speaker_id") or "vorbitor", []).append((k, a))
        prog(20 + 55 * (k + 1) / len(segs))
    t2 = WORK / "v2.wav"
    save(t2, place(v2, total))
    master(t2, OUT / "v2_clona_curata.mp3")

    say("varianta 3: aceeasi clona + timbrul vorbitorului pus inca o data (Seed-VC)...")
    v3 = {}
    gap = np.zeros(int(SR * 0.6), dtype="float32")
    for spk, items in by.items():
        ref = refs.get(spk, fallback)[0]
        joined, marks, pos = [], [], 0
        for k, a in items:
            marks.append((k, pos, len(a)))
            joined += [a, gap]
            pos += len(a) + len(gap)
        src = WORK / f"ro_{safe(spk)}.wav"
        save(src, np.concatenate(joined))
        conv = WORK / f"vc_{safe(spk)}.wav"
        seedvc(py, src, ref, conv)
        c = load(conv)
        scale = len(c) / max(1, pos)                          # length kept by Seed-VC; tiny drift fixed here
        for k, p0, n in marks:
            v3[k] = c[int(p0 * scale): int((p0 + n) * scale)]
        say(f"   {spk}: {len(items)} replici convertite")
    prog(92)
    t3 = WORK / "v3.wav"
    save(t3, place([(segs[k]["start"], segs[k]["end"], v3[k]) for k in sorted(v3)], total))
    master(t3, OUT / "v3_seedvc.mp3")
    print("REZULTAT " + json.dumps({"ok": True, "speakers": len(refs)}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        print("EROARE " + (str(e) or e.__class__.__name__)[:600], flush=True)
        sys.exit(1)
