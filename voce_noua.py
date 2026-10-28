"""
voce_noua.py - the dubbed voice of one clip, made line by line next to VoiceStudio (on the rented
GPU, started by the PC as a worker "script" job). Replaces VoiceStudio's own dub generation:

  1. the original voices are separated from music/noise (RoFormer); the music bed is kept for the mix
  2. speakers: VoiceStudio's labels, or (when it found only one) the lines grouped by voice (ECAPA)
  3. one clean reference per speaker (its clearest lines) + each line's own clip (its emotion)
  4. pass 1: every line generated from those references (OmniVoice, raw output, no studio effects)
  5. "Romanian reference": per speaker, its best Romanian lines from pass 1 become a new reference
     (same voice, already speaking Romanian: much less foreign accent); pass 2 generates the lines
     that are not perfect yet from it
     "native voice" (job native_voice): a native Romanian voice says the line, then Seed-VC puts the
     speaker's timbre on it ("nat"); the best of those become the speaker's native sample ("nr"),
     which pass 2 clones from instead: a Romanian speaking, sounding like the character
  6. every take is checked: Whisper (does it say the text), a Romanian speech model with no language
     model (sound by sound: the text aligned on its output, every letter's probability), UTMOS
     (does it sound natural / clean, no buzz) and ECAPA (is it still that speaker's voice); the best
     take of each line is kept; lines with a badly pronounced word are made again
  7. NO line is ever slowed down: a line that doesn't fit is generated faster by the model itself
     (up to 1.25x), then sped up (tone kept) up to 1.35x; it may also run into the silence after it
  8. lines evened out in loudness, brightness matched to the original voice, no heavy compression,
     loudness for TikTok -> out/voce.wav, out/info.json
The voice reads each line as voice_text() writes it (ș/ț, the pronunciation dictionary, names as a
Romanian reads them); the subtitles keep the real text.

Job folder: job.json + the clip's sound (spec "audio"). `voce_noua.py --setup` only installs the tools
(the worker runs it in the background when the GPU starts).
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
WIN = sys.platform.startswith("win")
BASE = Path(os.environ.get("LUCRATOR_DIR") or (HERE / "unelte_voce" if WIN else "/workspace/lucrator"))
VENV = BASE / "venv_voce"
HF_DIR = BASE / "hf"
TORCH_DIR = BASE / "torch"
SEP_MODELS = BASE / "modele_separare"
VS_URL = os.environ.get("VS_URL", "http://127.0.0.1:3900").rstrip("/")
SR = 24000           # voice work rate
MIX_SR = 44100
MAX_FIT = 1.25       # how much faster the model itself may be asked to speak
MAX_UP = 4.0         # speed-up after generation (tone kept): no real limit, a line always fits its
                     # place (only a guard against a broken take). Never slower than natural.
FIT_SPEED = 1.15     # faster than this: the PC asks the translator for a shorter line, made again
TAIL_MAX = 1.2       # a line may run this far into the silence after its slot
PUSH_MAX = 1.0       # or start this much later when the line before it is still talking
TARGET_LUFS = -15.0
SEP_ORDER = ("model_bs_roformer_ep_317_sdr_12.9755.ckpt", "mel_band_roformer_kim_ft_unwa.ckpt", None)
CTC_MODEL = "gigant/romanian-wav2vec2"     # Romanian wav2vec2, no language model: strict on pronunciation
TOOLS_VERSION = "3"                         # bump when the tools' venv needs new packages / models
LID_MODEL = "speechbrain/lang-id-voxlingua107-ecapa"   # which language a take sounds like (accent check)
# native voice -> the speaker's timbre: Seed-VC v1 (Whisper-small content: keeps the Romanian sounds),
# in its own Python (its pinned packages would clash with the judges'), installed in the background
VENV_VC = BASE / "venv_vc"
SEEDVC_DIR = BASE / "seed-vc"
SEEDVC_COMMIT = "51383efd921027683c89e5348211d93ff12ac2a8"
VC_VERSION = "1"                            # bump when the conversion tools change
VC_STEPS = 30
VC_CFG = 0.7
VC_RETRY_S = 6 * 3600                       # a failed install is tried again after this long
NATIVE_DIR = BASE / "voci_native"           # native voices made on this machine (when none is given)
# what a native voice made here says (~10 s, every Romanian sound): its sample, and its test
NATIVE_TEXT = ("Știi ce? Am stat toată noaptea și m-am gândit la ce mi-ai spus. Poate ai dreptate, "
               "dar nu pot să plec acum, când lucrurile abia încep să se schimbe.")
# when no saved native voice is given: OmniVoice's voice design (no sample, only a description), a
# few tries per gender, the best Romanian one kept (job "native_design" may name better ones)
NATIVE_DESIGN = {"m": ("male, middle-aged", "male", "male, young adult"),
                 "f": ("female, young adult", "female", "female, middle-aged")}
NATIVE_SEEDS = (11, 29)
NATIVE_VERSION = "1"                        # bump to make the native voices made here again
# the native voice must never be heard as itself: a take made from it counts only when it sounds
# clearly more like the speaker than the native voice does, and not far from the clip's own clones
NAT_SIM_GAIN = 0.25                         # measured: a good conversion gains +0.45..0.57 over the native voice
NAT_SIM_DROP = 0.20
VC_TIMEOUT_S = 240                          # one conversion; longer = the converter is stuck
VC_LOCK_STALE_S = 300                       # an install lock not refreshed this long: its install died
F0_MALE_MAX = 160.0                         # median pitch: under it a man's voice, over it a woman's
F0_CHILD_MIN = 255.0                        # a child: the women's native voice (closest timbre)

JD = OUT = WORK = None
FF = None


def say(m):
    print(m, flush=True)


_SAID = set()


def say_once(m):
    if m not in _SAID:
        _SAID.add(m)
        say(m)


def prog(p):
    print(f"PROGRES {p:.1f}", flush=True)


def sh(cmd, timeout=3600, env=None, cwd=None):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, timeout=timeout,
                       env=env, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"{str(cmd)[:100]}: " + (r.stderr or r.stdout).decode("utf-8", "replace")[-600:])
    return r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")


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


def to_wav(src, dst, sr=SR, ch=1):
    sh([FF, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vn", "-ac", str(ch), "-ar", str(sr),
        "-c:a", "pcm_s16le", str(dst)])


def load(path, sr=SR):
    import numpy as np
    import soundfile as sf
    a, r = sf.read(str(path), dtype="float32", always_2d=True)
    a = a.mean(axis=1)
    if r != sr:
        tmp = WORK / f"rs_{uuid.uuid4().hex[:8]}.wav"
        to_wav(path, tmp, sr)
        a, _ = sf.read(str(tmp), dtype="float32")
        tmp.unlink(missing_ok=True)
    return np.asarray(a, dtype="float32")


def save(path, a, sr=SR):
    import soundfile as sf
    sf.write(str(path), a, sr, subtype="PCM_16")


def env_tools():
    env = dict(os.environ, HF_HUB_ENABLE_HF_TRANSFER="0", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
               HF_HOME=str(HF_DIR), TORCH_HOME=str(TORCH_DIR))
    if FF:
        env["PATH"] = str(Path(FF).parent) + os.pathsep + env.get("PATH", "")
    return env


# ---------------------------------------------------------------- tools (once per GPU)
def venv_py():
    return VENV / ("Scripts/python.exe" if WIN else "bin/python")


ANALYZE = r'''
import json, os, sys
import numpy as np, soundfile as sf, torch, torchaudio
audio, outd, segs_json, cache, models = sys.argv[1:6]
order = json.loads(sys.argv[6])
segs = json.load(open(segs_json, encoding="utf-8"))
res = {}
if audio != "-":
    from audio_separator.separator import Separator
    err = []
    for model in order:
        try:
            s = Separator(output_dir=outd, output_format="WAV", model_file_dir=models)
            s.load_model(model_filename=model) if model else s.load_model()
            files = [f if os.path.isabs(f) else os.path.join(outd, f) for f in s.separate(audio)]
            voc = [f for f in files if "vocal" in os.path.basename(f).lower() and "instrum" not in os.path.basename(f).lower()]
            ins = [f for f in files if "instrum" in os.path.basename(f).lower() or "no_vocal" in os.path.basename(f).lower()]
            if voc:
                res["vocals"], res["bed"], res["model"] = voc[0], (ins[0] if ins else None), model
                break
        except Exception as e:
            err.append(f"{model}: {e}")
        finally:
            try:
                del s
            except Exception:
                pass
            torch.cuda.empty_cache()
    if "vocals" not in res:
        print("ANALIZA " + json.dumps({"error": " | ".join(err)[-500:]}), flush=True)
        sys.exit(0)
from speechbrain.inference.speaker import EncoderClassifier
kw = {}
try:
    from speechbrain.utils.fetching import LocalStrategy
    kw["local_strategy"] = LocalStrategy.COPY
except Exception:
    pass
dev = "cuda" if torch.cuda.is_available() else "cpu"
enc = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir=cache + "/ecapa",
                                     run_opts={"device": dev}, **kw)
embs = []
if segs:
    x, sr = sf.read(res["vocals"], dtype="float32", always_2d=True)
    t = torch.from_numpy(np.ascontiguousarray(x.mean(axis=1)))[None]
    if sr != 16000:
        t = torchaudio.functional.resample(t, sr, 16000)
    w = t[0]
    for s in segs:
        a, b = int(max(0.0, s["start"]) * 16000), int(s["end"] * 16000)
        piece = w[a:b]
        if piece.numel() < 0.8 * 16000:
            embs.append(None)
            continue
        with torch.no_grad():
            e = enc.encode_batch(piece[None].to(dev)).reshape(-1).float()
        embs.append(torch.nn.functional.normalize(e, dim=0).cpu().numpy().tolist())
res["embs"] = embs
print("ANALIZA " + json.dumps(res), flush=True)
'''

# One long-lived process per clip that judges every take. Started at once (its models load while the
# voices are being separated); the speakers' voice prints arrive later with a "CENT" line.
#   in:  "CENT\t<file.npy>"  -> "OK"
#   in:  "<speaker index or -1>\t<wav>[\t<the text the voice read>]" -> "SCOR {json}"
#        with the text also "pw": how well each of its words is pronounced (ctc_word_scores)
SCORER = r'''
import json, os, sys
import numpy as np, soundfile as sf, torch, torchaudio
cache, lang, ctc_name, app, lid_name = sys.argv[1:6]
engine = sys.argv[6] if len(sys.argv) > 6 else os.path.join(app, "voce_noua.py")
sys.path.insert(0, app)
dev = "cuda" if torch.cuda.is_available() else "cpu"
def log(m):
    print("LOG " + m.replace("\n", " ")[:300], flush=True)
enc = mos = ctc = fe = tok = lid = None
lid_idx = None
word_scores = vocab = blank = delim = None
try:
    from speechbrain.inference.speaker import EncoderClassifier
    kw = {}
    try:
        from speechbrain.utils.fetching import LocalStrategy
        kw["local_strategy"] = LocalStrategy.COPY
    except Exception:
        pass
    enc = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir=cache + "/ecapa",
                                         run_opts={"device": dev}, **kw)
except Exception as e:
    log("ecapa: %s" % e)
try:
    # which language the take sounds like: a Romanian line with an English / Russian accent gets a lower
    # Romanian probability (VoxLingua107, 107 languages)
    lid = EncoderClassifier.from_hparams(source=lid_name, savedir=cache + "/lid", run_opts={"device": dev}, **kw)
    for i, lab in dict(lid.hparams.label_encoder.ind2lab).items():
        if str(lab).split(":")[0].strip().lower() == lang.lower():
            lid_idx = int(i)
    if lid_idx is None:
        log("lid: no label for %s" % lang)
        lid = None
except Exception as e:
    lid = None
    log("lid: %s" % e)
try:
    import utmos22                      # vendored next to this script (only its weights are downloaded)
    mos = utmos22.load(dev)
except Exception as e:
    log("utmos: %s" % e)
if lang == "ro" and ctc_name:
    try:
        from transformers import Wav2Vec2CTCTokenizer, Wav2Vec2FeatureExtractor, Wav2Vec2ForCTC
        fe = Wav2Vec2FeatureExtractor.from_pretrained(ctc_name, cache_dir=cache)
        tok = Wav2Vec2CTCTokenizer.from_pretrained(ctc_name, cache_dir=cache)
        ctc = Wav2Vec2ForCTC.from_pretrained(ctc_name, cache_dir=cache, use_safetensors=True).to(dev).eval()
        if dev == "cuda":
            ctc = ctc.half()
    except Exception as e:
        ctc = None
        log("ctc: %s" % e)
if ctc is not None:
    try:
        # the letter-by-letter judge: the same code as the script that started this one (by its own
        # file: a test may run the engine under another name next to the app's voce_noua.py)
        import importlib.util
        _spec = importlib.util.spec_from_file_location("voce_engine", engine)
        _eng = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_eng)
        word_scores = _eng.ctc_word_scores
        vocab = dict(tok.get_vocab())
        blank = tok.pad_token_id if tok.pad_token_id is not None else vocab.get("<pad>", vocab.get("[PAD]", 0))
        delim = vocab.get(getattr(tok, "word_delimiter_token", None) or "|")
    except Exception as e:
        word_scores = None
        log("pron: %s" % e)
cent = None
print("GATA " + json.dumps({"ecapa": enc is not None, "utmos": mos is not None, "ctc": ctc is not None,
                            "lid": lid is not None, "pron": word_scores is not None}), flush=True)
for line in sys.stdin:
    line = line.rstrip("\n")
    if line.startswith("CENT\t"):
        try:
            cent = torch.from_numpy(np.load(line[5:])).float().to(dev)
            print("OK", flush=True)
        except Exception as e:
            print("OK " + str(e)[:100], flush=True)
        continue
    out = {}
    try:
        parts = line.split("\t")
        k, path = int(parts[0]), parts[1]
        said = parts[2] if len(parts) > 2 else ""
        x, sr = sf.read(path, dtype="float32", always_2d=True)
        t = torch.from_numpy(np.ascontiguousarray(x.mean(axis=1)))[None]
        t16 = (torchaudio.functional.resample(t, sr, 16000) if sr != 16000 else t).to(dev)
        with torch.no_grad():
            if enc is not None and cent is not None and 0 <= k < cent.shape[0]:
                e = torch.nn.functional.normalize(enc.encode_batch(t16).reshape(-1).float(), dim=0)
                out["sim"] = float(torch.dot(e, cent[k]))
            if mos is not None:
                out["utmos"] = float(mos(t16, 16000).reshape(-1)[0])
            if lid is not None:
                lp = lid.classify_batch(t16)[0].reshape(-1).float()
                out["lid"] = float(torch.softmax(lp, dim=0)[lid_idx])
            if ctc is not None:
                iv = fe(t16[0].float().cpu().numpy(), sampling_rate=16000, return_tensors="pt").input_values.to(dev)
                if dev == "cuda":
                    iv = iv.half()
                logits = ctc(iv).logits
                ids = torch.argmax(logits, dim=-1)
                out["ctc"] = tok.batch_decode(ids)[0]
                if said and word_scores is not None:
                    try:
                        pw = word_scores(logits[0].float().cpu().numpy(), said, vocab, blank, delim)
                        if pw is not None:
                            out["pw"] = pw
                    except Exception as ex:
                        out["pw_err"] = str(ex)[:120]
    except Exception as ex:
        out["err"] = str(ex)[:150]
    print("SCOR " + json.dumps(out, ensure_ascii=False), flush=True)
'''


def setup():
    """The tools' own Python (separation + the judges of each take), once per GPU; also downloads
    their models. Several jobs may ask at once: one installs, the others wait for it."""
    py = venv_py()
    mark = VENV / ".gata"

    def ready():
        try:
            return mark.read_text().strip() == "ok" + TOOLS_VERSION
        except OSError:
            return False

    if ready():
        return py
    BASE.mkdir(parents=True, exist_ok=True)
    lock = BASE / "venv_voce.lock"
    t0 = time.time()
    shown = False
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if ready():
                return py
            try:
                if time.time() - lock.stat().st_mtime > 1500:       # left behind by a killed install
                    lock.unlink()
                    continue
            except FileNotFoundError:
                continue
            if not shown:
                say("   uneltele vocii se instaleaza deja pe GPU, astept...")
                shown = True
            if time.time() - t0 > 1800:
                raise RuntimeError("instalarea uneltelor vocii dureaza prea mult")
            time.sleep(3)
    try:
        if ready():
            return py
        say("   instalez uneltele vocii pe GPU (o singura data, 3-5 minute)...")
        try:
            import torch
            import torchaudio
        except ImportError:
            raise RuntimeError("lipseste PyTorch pe masina asta")
        if not torch.cuda.is_available():
            raise RuntimeError("PyTorch nu vede placa video")
        if not py.exists():
            sh([sys.executable, "-m", "venv", "--system-site-packages", str(VENV)])
        cons = BASE / "constraints_voce.txt"
        lines = [f"torch=={torch.__version__}", f"torchaudio=={torchaudio.__version__}"]
        try:
            import torchvision
            lines.append(f"torchvision=={torchvision.__version__}")
        except Exception:  # noqa: BLE001
            pass
        cons.write_text("\n".join(lines) + "\n")
        sh([str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-c", str(cons),
            "audio-separator[gpu]", "speechbrain", "soundfile"], timeout=2400, env=env_tools())
        try:                       # the Romanian speech model; transformers usually comes with the image
            sh([str(py), "-c", "import transformers"], timeout=120, env=env_tools())
        except RuntimeError:
            sh([str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-c", str(cons),
                "transformers"], timeout=1200, env=env_tools())
        # models now, not in the middle of the first clip
        tmp = BASE / "warm"
        tmp.mkdir(exist_ok=True)
        import numpy as np
        import soundfile as sf
        sf.write(str(tmp / "liniste.wav"), np.zeros(44100 * 3, dtype="float32"), 44100)
        (tmp / "segs.json").write_text("[]")
        out = sh([str(py), "-c", ANALYZE, str(tmp / "liniste.wav"), str(tmp), str(tmp / "segs.json"),
                  str(HF_DIR), str(SEP_MODELS), json.dumps(SEP_ORDER)], timeout=1800, env=env_tools())
        if '"vocals"' not in out:
            raise RuntimeError("separarea nu merge: " + out[-300:])
        try:
            sc = Scorer(py, "ro", warm=True)
            sc.close()
        except Exception as e:  # noqa: BLE001 - the judges are optional
            say(f"   o parte din verificari nu a pornit: {str(e)[:200]}")
        shutil.rmtree(tmp, ignore_errors=True)
        mark.write_text("ok" + TOOLS_VERSION)
        say("   uneltele vocii sunt gata")
        return py
    finally:
        os.close(fd)
        try:
            lock.unlink()
        except OSError:
            pass


def analyze(py, audio, segs):
    say("   separ vocile de muzica si zgomot...")
    outd = WORK / "sep"
    outd.mkdir(exist_ok=True)
    sj = WORK / "segs.json"
    sj.write_text(json.dumps([{"start": s["start"], "end": s["end"]} for s in segs]), encoding="utf-8")
    out = sh([str(py), "-c", ANALYZE, str(audio), str(outd), str(sj), str(HF_DIR), str(SEP_MODELS),
              json.dumps(SEP_ORDER)], timeout=2400, env=env_tools())
    line = next((ln for ln in out.splitlines() if ln.startswith("ANALIZA ")), None)
    if not line:
        raise RuntimeError("analiza sunetului nu a raspuns: " + out[-300:])
    res = json.loads(line[8:])
    if res.get("error") or not res.get("vocals"):
        raise RuntimeError("separarea vocilor nu a mers: " + str(res.get("error"))[-300:])
    return res


class Scorer:
    """The judges, in their own process: ECAPA (voice like speaker k), UTMOS (natural / clean),
    the Romanian speech model (what is really pronounced). Any of them may be missing."""

    def __init__(self, py, lang, warm=False):
        runner = (WORK or BASE) / "scorer.py"
        runner.write_text(SCORER, encoding="utf-8")
        self.lock = threading.Lock()
        self.p = subprocess.Popen([str(py), str(runner), str(HF_DIR), lang, CTC_MODEL if lang == "ro" else "",
                                   str(HERE), LID_MODEL, str(Path(__file__).resolve())],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  env=env_tools(), text=True, encoding="utf-8", bufsize=1)
        self.has = {}
        self.notes = []
        self.ready = threading.Event()
        if warm:
            self._wait_ready()
            if not self.has:
                raise RuntimeError("verificarile nu pornesc")
        else:
            threading.Thread(target=self._wait_ready, daemon=True).start()

    def _wait_ready(self):
        t0 = time.time()
        while time.time() - t0 < 1200:
            ln = self.p.stdout.readline()
            if not ln:
                break
            if ln.startswith("LOG "):
                self.notes.append(ln[4:].strip())
            elif ln.startswith("GATA "):
                try:
                    self.has = json.loads(ln[5:])
                except ValueError:
                    self.has = {}
                break
        self.ready.set()

    def _ask(self, line, prefix):
        try:
            self.p.stdin.write(line + "\n")
            self.p.stdin.flush()
            while True:
                ln = self.p.stdout.readline()
                if not ln:
                    return None
                if ln.startswith(prefix):
                    return ln[len(prefix):].strip()
        except Exception:  # noqa: BLE001
            return None

    def centroids(self, path):
        self.ready.wait(1200)
        with self.lock:
            if self.has:
                self._ask(f"CENT\t{path}", "OK")

    def judge(self, k, path, said=""):
        """said: the text the voice read (for the letter-by-letter pronunciation check)."""
        self.ready.wait(1200)
        if not self.has:
            return {}
        said = " ".join(str(said or "").split())          # one line, no tabs
        with self.lock:
            r = self._ask(f"{-1 if k is None else k}\t{path}" + (f"\t{said}" if said else ""), "SCOR ")
        try:
            return json.loads(r) if r else {}
        except ValueError:
            return {}

    def close(self):
        try:
            self.p.stdin.close()
            self.p.kill()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- native voice -> the speaker's timbre
# Seed-VC v1 in its own process (its own Python, venv_vc): the models load once, then every take.
#   start: python vc_runner.py <seed-vc folder> -> "GATA {...}" or "EROARE <why>"
#   in:    "<source.wav>\t<target.wav>\t<output.wav>\t<steps>\t<cfg>" -> "VC ok <s>" / "VC err <why>"
# The output is as long as the source (24 kHz); the target's features stay cached by its path.
VC_RUNNER = r'''"""
vc_runner.py - Seed-VC v1 (commit 51383ef) as a long-lived converter: the models load ONCE, then
any number of files are converted.

    python vc_runner.py <folder_seed_vc>
        -> stdout "GATA {"device": "cuda", "sr": 24000}"  (or "EROARE <reason>", exit code 1)
    stdin, one line per file:  <source.wav>\t<target.wav>\t<output.wav>\t<diffusion_steps>\t<cfg>
        -> stdout "VC ok <seconds>"  or  "VC err <reason>"

Output: mono WAV, 16-bit, 24 kHz, exactly as long as the source (length_adjust 1.0). The target
voice's features (CAM++ style, whisper prompt, mel) stay cached by the target's path. Audio is read
and written with soundfile only; resampling with torchaudio.functional. Only the protocol lines go
to stdout (everything the models print goes to stderr). An empty line or EOF ends the process.

Models (downloaded once into <folder_seed_vc>/checkpoints): Plachta/Seed-VC
DiT_seed_v2_uvit_whisper_small_wavenet_bigvgan_pruned.pth + config_dit_mel_seed_uvit_whisper_small_wavenet.yml,
funasr/campplus, nvidia/bigvgan_v2_22khz_80band_256x, openai/whisper-small.
"""
import json
import os
import sys
import time
import types

OUT = sys.stdout
sys.stdout = sys.stderr                    # library prints / progress bars never reach the protocol
SR_OUT = 24000


def reply(msg):
    OUT.write(" ".join(str(msg).replace("\t", " ").splitlines()).strip() + "\n")
    OUT.flush()


def _stub_dac():
    """descript-audio-codec is imported only for VectorQuantize, unused with vector_quantize: false."""
    if "dac" in sys.modules:
        return
    try:
        import dac.nn.quantize  # noqa: F401  (the real package, when it is installed)
        return
    except Exception:  # noqa: BLE001
        pass

    class VectorQuantize:                  # never built by this config
        def __init__(self, *a, **k):
            raise RuntimeError("VectorQuantize (descript-audio-codec) is not installed")

    for name in ("dac", "dac.nn", "dac.nn.quantize"):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["dac"].nn = sys.modules["dac.nn"]
    sys.modules["dac.nn"].quantize = sys.modules["dac.nn.quantize"]
    sys.modules["dac.nn.quantize"].VectorQuantize = VectorQuantize


def _stub_matplotlib():
    """modules/bigvgan/utils.py imports matplotlib only for its (unused) spectrogram plots."""
    try:
        import matplotlib  # noqa: F401
        return
    except Exception:  # noqa: BLE001
        pass
    mpl = types.ModuleType("matplotlib")
    mpl.use = lambda *a, **k: None
    pylab = types.ModuleType("matplotlib.pylab")
    mpl.pylab = pylab
    sys.modules["matplotlib"] = mpl
    sys.modules["matplotlib.pylab"] = pylab


class VC:
    def __init__(self, folder):
        folder = os.path.abspath(folder)
        if not os.path.isfile(os.path.join(folder, "modules", "commons.py")):
            raise RuntimeError(f"not a seed-vc folder: {folder}")
        ck = os.path.join(folder, "checkpoints")
        os.environ.setdefault("HF_HUB_CACHE", os.path.join(ck, "hf_cache"))
        sys.path.insert(0, folder)
        _stub_dac()
        _stub_matplotlib()
        import warnings
        warnings.simplefilter("ignore")
        import torch
        import yaml
        from huggingface_hub import hf_hub_download
        from modules.commons import build_model, load_checkpoint, recursive_munch
        from modules.audio import mel_spectrogram

        self.torch = torch
        self.dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.fp16 = self.dev.type == "cuda"
        dit_path = hf_hub_download("Plachta/Seed-VC", "DiT_seed_v2_uvit_whisper_small_wavenet_bigvgan_pruned.pth",
                                   cache_dir=ck)
        cfg_path = hf_hub_download("Plachta/Seed-VC", "config_dit_mel_seed_uvit_whisper_small_wavenet.yml",
                                   cache_dir=ck)
        with open(cfg_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        mp = recursive_munch(config["model_params"])
        mp.dit_type = "DiT"
        model = build_model(mp, stage="DiT")
        model, _, _, _ = load_checkpoint(model, None, dit_path, load_only_params=True, ignore_modules=[],
                                         is_distributed=False)
        for k in model:
            model[k].eval()
            model[k].to(self.dev)
        model.cfm.estimator.setup_caches(max_batch_size=1, max_seq_length=8192)
        self.model = model

        from modules.campplus.DTDNN import CAMPPlus
        cp = hf_hub_download("funasr/campplus", "campplus_cn_common.bin", cache_dir=ck)
        self.campplus = CAMPPlus(feat_dim=80, embedding_size=192)
        self.campplus.load_state_dict(torch.load(cp, map_location="cpu"))
        self.campplus.eval().to(self.dev)

        from modules.bigvgan import bigvgan
        voc = bigvgan.BigVGAN.from_pretrained(mp.vocoder.name, use_cuda_kernel=False)
        voc.remove_weight_norm()
        self.vocoder = voc.eval().to(self.dev)

        from transformers import AutoFeatureExtractor, WhisperModel
        wname = mp.speech_tokenizer.name
        wm = WhisperModel.from_pretrained(wname, torch_dtype=torch.float16).to(self.dev)
        del wm.decoder
        self.whisper = wm
        self.wfe = AutoFeatureExtractor.from_pretrained(wname)

        sp = config["preprocess_params"]["spect_params"]
        self.sr = int(config["preprocess_params"]["sr"])            # 22050
        self.hop = int(sp["hop_length"])                             # 256
        mel_args = {"n_fft": sp["n_fft"], "win_size": sp["win_length"], "hop_size": sp["hop_length"],
                    "num_mels": sp["n_mels"], "sampling_rate": self.sr, "fmin": sp.get("fmin", 0),
                    "fmax": None if sp.get("fmax", "None") == "None" else 8000, "center": False}
        self.to_mel = lambda x: mel_spectrogram(x, **mel_args)
        self.targets = {}

    # ------------------------------------------------------------ helpers
    def read(self, path, sr):
        import numpy as np
        import soundfile as sf
        x, r = sf.read(path, dtype="float32", always_2d=True)
        if x.shape[0] == 0:
            raise ValueError(f"empty audio: {path}")
        t = self.torch.from_numpy(np.ascontiguousarray(x.mean(axis=1)))[None]
        if r != sr:
            t = self.torchaudio_resample(t, r, sr)
        return t, r, x.shape[0]

    def torchaudio_resample(self, t, a, b):
        import torchaudio.functional as AF
        return AF.resample(t, a, b)

    def semantic(self, w16):
        torch = self.torch
        inp = self.wfe([w16.squeeze(0).cpu().numpy()], return_tensors="pt", return_attention_mask=True,
                       sampling_rate=16000)
        feats = self.whisper._mask_input_features(inp.input_features, attention_mask=inp.attention_mask).to(self.dev)
        with torch.no_grad():
            out = self.whisper.encoder(feats.to(self.whisper.encoder.dtype), head_mask=None, output_attentions=False,
                                       output_hidden_states=False, return_dict=True)
        s = out.last_hidden_state.to(torch.float32)
        return s[:, : w16.size(-1) // 320 + 1]

    def semantic_long(self, w16):
        """Whisper sees 30 s at once: longer sources in 30 s windows overlapping by 5 s (as inference.py)."""
        torch = self.torch
        if w16.size(-1) <= 16000 * 30:
            return self.semantic(w16)
        ov = 5
        parts, buf, done = [], None, 0
        while done < w16.size(-1):
            if buf is None:
                chunk = w16[:, done:done + 16000 * 30]
            else:
                chunk = torch.cat([buf, w16[:, done:done + 16000 * (30 - ov)]], dim=-1)
            s = self.semantic(chunk)
            parts.append(s if done == 0 else s[:, 50 * ov:])
            buf = chunk[:, -16000 * ov:]
            done += 30 * 16000 if done == 0 else chunk.size(-1) - 16000 * ov
        return torch.cat(parts, dim=1)

    def target(self, path):
        """The target voice's features, computed once per file (path + size + mtime)."""
        st = os.stat(path)
        key = (os.path.abspath(path), st.st_size, st.st_mtime_ns)
        if key in self.targets:
            return self.targets[key]
        torch = self.torch
        ref, _, _ = self.read(path, self.sr)
        ref = ref[:, : self.sr * 25].to(self.dev)                       # at most 25 s, as inference.py
        w16 = self.torchaudio_resample(ref, self.sr, 16000)
        with torch.no_grad():
            s_ori = self.semantic(w16)
            mel2 = self.to_mel(ref.float())
            feat2 = __import__("torchaudio").compliance.kaldi.fbank(w16, num_mel_bins=80, dither=0,
                                                                    sample_frequency=16000)
            feat2 = feat2 - feat2.mean(dim=0, keepdim=True)
            style2 = self.campplus(feat2.unsqueeze(0))
            prompt, *_ = self.model.length_regulator(s_ori, ylens=torch.LongTensor([mel2.size(2)]).to(self.dev),
                                                     n_quantizers=3, f0=None)
        self.targets = {k: v for k, v in self.targets.items() if k[0] != key[0]}   # an older version of it
        self.targets[key] = (mel2, style2, prompt)
        return self.targets[key]

    # ------------------------------------------------------------ one conversion
    def convert(self, src, tgt, dst, steps, cfg):
        import numpy as np
        import soundfile as sf
        torch = self.torch
        mel2, style2, prompt = self.target(tgt)
        source, src_sr, n_src = self.read(src, self.sr)
        source = source.to(self.dev)
        n_out = int(round(n_src * SR_OUT / src_sr))
        max_ctx = self.sr // self.hop * 30
        ov_frames = 16
        ov_wave = ov_frames * self.hop
        with torch.no_grad():
            w16 = self.torchaudio_resample(source, self.sr, 16000)
            s_alt = self.semantic_long(w16)
            mel = self.to_mel(source.float())
            cond, *_ = self.model.length_regulator(s_alt, ylens=torch.LongTensor([int(mel.size(2) * 1.0)]).to(self.dev),
                                                   n_quantizers=3, f0=None)
            max_src = max_ctx - mel2.size(2)
            done, chunks, prev = 0, [], None
            while done < cond.size(1):
                cc = cond[:, done:done + max_src]
                last = done + max_src >= cond.size(1)
                cat = torch.cat([prompt, cc], dim=1)
                with torch.autocast(device_type=self.dev.type, dtype=torch.float16 if self.fp16 else torch.float32):
                    out = self.model.cfm.inference(cat, torch.LongTensor([cat.size(1)]).to(self.dev), mel2, style2,
                                                   None, steps, inference_cfg_rate=cfg)
                    out = out[:, :, mel2.size(-1):]
                wav = self.vocoder(out.float()).squeeze()[None, :]
                if done == 0:
                    if last:
                        chunks.append(wav[0].cpu().numpy())
                        break
                    chunks.append(wav[0, :-ov_wave].cpu().numpy())
                    prev = wav[0, -ov_wave:]
                    done += out.size(2) - ov_frames
                else:
                    cur = wav[0].cpu().numpy() if last else wav[0, :-ov_wave].cpu().numpy()
                    chunks.append(_crossfade(prev.cpu().numpy(), cur, ov_wave))
                    if last:
                        break
                    prev = wav[0, -ov_wave:]
                    done += out.size(2) - ov_frames
        y = torch.from_numpy(np.concatenate(chunks)).float()[None]
        y = self.torchaudio_resample(y, self.sr, SR_OUT)[0].numpy()
        if len(y) < n_out:                                              # exactly the source's length
            y = np.concatenate([y, np.zeros(n_out - len(y), dtype=y.dtype)])
        y = np.clip(y[:n_out], -1.0, 1.0)
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        sf.write(dst, y, SR_OUT, subtype="PCM_16")


def _crossfade(a, b, n):
    import numpy as np
    b = b.copy()
    fo = np.cos(np.linspace(0, np.pi / 2, n)) ** 2
    fi = np.cos(np.linspace(np.pi / 2, 0, n)) ** 2
    m = min(n, len(b))
    b[:m] = b[:m] * fi[:m] + (a[-n:] * fo)[:m]
    return b


def main():
    if len(sys.argv) < 2:
        reply("EROARE lipseste folderul seed-vc (python vc_runner.py <folder_seed_vc>)")
        sys.exit(2)
    try:
        vc = VC(sys.argv[1])
    except BaseException as e:  # noqa: BLE001
        reply(f"EROARE {type(e).__name__}: {str(e)[:300]}")
        sys.exit(1)
    reply("GATA " + json.dumps({"device": vc.dev.type, "sr": SR_OUT}))
    for line in sys.stdin:
        line = line.rstrip("\r\n")
        if not line.strip():
            break
        t0 = time.time()
        try:
            parts = line.split("\t")
            if len(parts) != 5:
                raise ValueError(f"expected 5 tab-separated fields, got {len(parts)}")
            src, tgt, dst, steps, cfg = parts
            vc.convert(src, tgt, dst, int(steps), float(cfg))
            reply(f"VC ok {time.time() - t0:.2f}")
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, KeyboardInterrupt):
                break
            try:
                if vc.dev.type == "cuda":
                    vc.torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            reply(f"VC err {type(e).__name__}: {str(e)[:300]}")


if __name__ == "__main__":
    main()
'''


def vc_py():
    return VENV_VC / ("Scripts/python.exe" if WIN else "bin/python")


class VC:
    """The converter: a native Romanian take -> the speaker's timbre (its clean clip sample).
    ok False once it failed to start or died; last_error says why."""

    def __init__(self):
        import queue
        runner = (WORK or BASE) / "vc_runner.py"
        runner.write_text(VC_RUNNER, encoding="utf-8")
        self.ok = False
        self.last_error = ""
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.q = queue.Queue()
        self.log = open((WORK or BASE) / "vc.log", "ab")
        self.p = subprocess.Popen([str(vc_py()), str(runner), str(SEEDVC_DIR)], cwd=str(SEEDVC_DIR),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                  env=env_tools(), text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for ln in self.p.stdout:
            ln = ln.strip()
            if not self.ready.is_set():
                if ln.startswith("GATA"):
                    self.ok = True
                    self.ready.set()
                elif ln.startswith("EROARE"):
                    self.last_error = ln[6:].strip() or "nu porneste"
                    self.ready.set()
                continue
            if ln.startswith("VC "):
                self.q.put(ln[3:])
        if not self.last_error:
            self.last_error = "procesul conversiei s-a oprit"
        self.ok = False
        self.ready.set()
        self.q.put(None)

    def wait(self, timeout=900):
        self.ready.wait(timeout)
        if not self.ready.is_set():
            self.last_error = "modelele conversiei nu s-au incarcat"
            self.close()
        return self.ok

    def convert(self, src, ref, dst):
        import queue
        if not self.wait():
            return False
        with self.lock:
            try:
                while not self.q.empty():
                    self.q.get_nowait()
                self.p.stdin.write(f"{src}\t{ref}\t{dst}\t{VC_STEPS}\t{VC_CFG}\n")
                self.p.stdin.flush()
                r = self.q.get(timeout=VC_TIMEOUT_S)
            except queue.Empty:
                self.last_error = f"o conversie a durat peste {VC_TIMEOUT_S} s"
                self.close()
                return False
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)[:200]
                self.close()
                return False
        if r is None:
            return False
        if r.startswith("ok") and Path(dst).exists():
            return True
        self.last_error = r[3:].strip() if r.startswith("err") else r
        return False

    def close(self):
        self.ok = False
        try:
            self.p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.p.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.log.close()
        except Exception:  # noqa: BLE001
            pass


def setup_vc(wait=True):
    """Seed-VC's own Python + code + models, once per machine (the worker installs it in the
    background with --setup). -> True ready, None being installed by someone else (wait=False),
    False not possible here (tried again after VC_RETRY_S)."""
    mark, failed, lock = VENV_VC / ".gata", BASE / "venv_vc.esuat", BASE / "venv_vc.lock"

    def ready():
        try:
            return mark.read_text().strip() == "ok" + VC_VERSION
        except OSError:
            return False

    if ready():
        return True
    try:
        if time.time() - failed.stat().st_mtime < VC_RETRY_S:
            return False
    except OSError:
        pass
    BASE.mkdir(parents=True, exist_ok=True)
    t0 = shown = time.time()
    token = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, token.encode())
            break
        except FileExistsError:
            if ready():
                return True
            try:
                # the installer refreshes its lock every minute: an old one was left by an install that died
                if time.time() - lock.stat().st_mtime > VC_LOCK_STALE_S:
                    lock.unlink()
                    continue
            except FileNotFoundError:
                continue
            if not wait or time.time() - t0 > 3000:
                return None
            if time.time() - shown > 60:              # a sign of life for the watchdogs of the clip
                shown = time.time()
                say(f"    astept instalarea conversiei vocii native ({int((shown - t0) // 60)} min)")
            time.sleep(5)
    stop = threading.Event()

    def heartbeat():
        n = 0
        while not stop.wait(60):
            n += 1
            try:
                os.utime(lock, None)
            except OSError:
                pass
            say(f"    instalez conversia vocii native... {n} min")
    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        if ready():
            return True
        say("   instalez conversia vocii native (Seed-VC; o singura data, cateva minute)...")
        try:
            import torch
            import torchaudio
        except ImportError:
            raise RuntimeError("lipseste PyTorch pe masina asta")
        if not torch.cuda.is_available():
            raise RuntimeError("PyTorch nu vede placa video")
        env = env_tools()
        if not (SEEDVC_DIR / "modules" / "commons.py").exists():
            import zipfile
            z = BASE / "seed-vc.zip"
            with urllib.request.urlopen(f"https://codeload.github.com/Plachtaa/seed-vc/zip/{SEEDVC_COMMIT}",
                                        timeout=60) as r, open(z, "wb") as f:
                t1 = time.time()
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
                    if time.time() - t1 > 900:
                        raise RuntimeError("descarcarea Seed-VC dureaza prea mult")
            tmp = BASE / "seed-vc-tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            with zipfile.ZipFile(z) as zf:
                zf.extractall(tmp)
            top = next(p for p in tmp.iterdir() if p.is_dir())
            shutil.rmtree(SEEDVC_DIR, ignore_errors=True)
            shutil.move(str(top), str(SEEDVC_DIR))
            shutil.rmtree(tmp, ignore_errors=True)
            z.unlink(missing_ok=True)
        py = vc_py()
        if not py.exists():
            sh([sys.executable, "-m", "venv", "--system-site-packages", str(VENV_VC)])
        cons = BASE / "constraints_vc.txt"
        pins = [f"torch=={torch.__version__}", f"torchaudio=={torchaudio.__version__}"]
        try:
            import torchvision
            pins.append(f"torchvision=={torchvision.__version__}")
        except Exception:  # noqa: BLE001
            pass
        cons.write_text("\n".join(pins) + "\n")
        pip = [str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-c", str(cons)]
        try:
            sh(pip + ["transformers==4.46.3", "huggingface_hub<1.0", "librosa==0.10.2", "scipy", "munch", "einops",
                      "pyyaml", "soundfile", "numpy<2"], timeout=2400, env=env)
        except RuntimeError:                      # the exact versions are not available here: close ones
            sh(pip + ["transformers>=4.44,<4.50", "huggingface_hub<1.0", "librosa>=0.10,<0.11", "scipy", "munch",
                      "einops", "pyyaml", "soundfile"], timeout=2400, env=env)
        # the models now (downloaded on the first load), with one real conversion
        import numpy as np
        import soundfile as sf
        tmp = BASE / f"warm_vc_{os.getpid()}"
        tmp.mkdir(exist_ok=True)
        t = np.arange(SR * 3) / SR
        for name, f in (("sursa.wav", 130.0), ("tinta.wav", 210.0)):
            x = 0.3 * np.sin(2 * np.pi * f * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))
            sf.write(str(tmp / name), x.astype("float32"), SR, subtype="PCM_16")
        vc = VC()
        try:
            if not vc.wait(2400):
                raise RuntimeError(vc.last_error or "conversia nu porneste")
            if not vc.convert(tmp / "sursa.wav", tmp / "tinta.wav", tmp / "iesire.wav"):
                raise RuntimeError(vc.last_error or "conversia de proba nu a mers")
        finally:
            vc.close()
        shutil.rmtree(tmp, ignore_errors=True)
        mark.write_text("ok" + VC_VERSION)
        failed.unlink(missing_ok=True)
        say("   conversia vocii native e gata")
        return True
    except Exception as e:  # noqa: BLE001 - the native voice is optional: the clip goes on without it
        try:
            failed.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{str(e)[:500]}", encoding="utf-8")
        except OSError:
            pass
        say(f"   conversia vocii native nu se poate instala: {str(e)[:200]}")
        return False
    finally:
        stop.set()
        os.close(fd)
        try:
            if lock.read_text() == token:             # only our own lock (a stale one may be someone else's now)
                lock.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- speakers
def cluster(embs, n_fixed=0, thr=0.30, max_spk=6):
    """Average-linkage grouping of line embeddings (unit vectors). Returns a label per item."""
    import numpy as np
    X = np.asarray(embs, dtype="float32")
    n = len(X)
    if n == 0:
        return []
    S = X @ X.T
    np.fill_diagonal(S, -9)
    members = {i: [i] for i in range(n)}
    alive = list(range(n))
    while len(alive) > 1:
        sub = S[np.ix_(alive, alive)]
        i, j = np.unravel_index(int(np.argmax(sub)), sub.shape)
        best = float(sub[i, j])
        a, b = alive[i], alive[j]
        if n_fixed:
            if len(alive) <= n_fixed:
                break
        elif best < thr and len(alive) <= max_spk:
            break
        na, nb = len(members[a]), len(members[b])
        S[a, :] = (na * S[a, :] + nb * S[b, :]) / (na + nb)
        S[:, a] = S[a, :]
        S[a, a] = -9
        members[a] += members.pop(b)
        alive.remove(b)
    labels = [0] * n
    for lab, root in enumerate(sorted(alive, key=lambda r: min(members[r]))):
        for m in members[root]:
            labels[m] = lab
    return labels


def speakers(segs, embs, job):
    """Speaker key per line. VoiceStudio's labels unless it found a single speaker (often its
    fallback when its own speaker detection isn't available): then the lines are grouped by voice."""
    import numpy as np
    given = [s.get("speaker_id") or "vorbitor" for s in segs]
    n_fixed = int(job.get("num_speakers") or 0)
    if not job.get("rediarize") or n_fixed == 1 or len(set(given)) > 1:
        return given, False
    idx = [i for i, e in enumerate(embs) if e is not None]
    if len(idx) < 4:
        return given, False
    labels = cluster([embs[i] for i in idx], n_fixed=n_fixed)
    # tiny groups (< 3 s of speech) are folded into the closest big one
    dur = {}
    for i, lab in zip(idx, labels):
        dur[lab] = dur.get(lab, 0.0) + segs[i]["end"] - segs[i]["start"]
    E = np.asarray([embs[i] for i in idx], dtype="float32")
    cent = {lab: E[[k for k, l2 in enumerate(labels) if l2 == lab]].mean(axis=0) for lab in set(labels)}
    # a real second voice speaks for a while: a group of 1 line or under 8 s is the same voice
    # sounding different (a shout, a whisper, music under it) and goes to the closest big group
    count = {lab: labels.count(lab) for lab in set(labels)}
    big = [lab for lab in cent if dur[lab] >= 8.0 and count[lab] >= 2] or [max(dur, key=dur.get)]
    if not n_fixed:
        for k, lab in enumerate(labels):
            if lab not in big:
                labels[k] = max(big, key=lambda b: float(E[k] @ cent[b]))
    names = {lab: f"V{r + 1}" for r, lab in enumerate(sorted(set(labels), key=lambda l2: labels.index(l2)))}
    out = [None] * len(segs)
    for i, lab in zip(idx, labels):
        out[i] = names[lab]
    for i in range(len(segs)):                 # short lines: the voice of the closest line in time
        if out[i] is None:
            near = min(idx, key=lambda j: abs(segs[j]["start"] - segs[i]["start"]))
            out[i] = out[near]
    if len(set(out)) <= 1:
        return given, False
    return out, True


# ---------------------------------------------------------------- references
def references(vocals, segs, spk, embs):
    """Per speaker: its clearest lines (loud enough, clearly that voice) joined into 6-15 s.
    Per line: its own clip, when it is long and clean enough (keeps the line's emotion)."""
    import numpy as np
    a = load(vocals)
    keys = sorted(set(spk), key=spk.index)
    cent = {}
    for k in keys:
        vs = [np.asarray(embs[i]) for i in range(len(segs)) if spk[i] == k and embs[i] is not None]
        if vs:
            c = np.mean(vs, axis=0)
            cent[k] = c / (np.linalg.norm(c) + 1e-9)
    gap = np.zeros(int(SR * 0.25), dtype="float32")
    refs, own = {}, {}
    for k in keys:
        cands = []
        for i, s in enumerate(segs):
            if spk[i] != k:
                continue
            i0, i1 = int(max(0.0, s["start"] - 0.05) * SR), int((s["end"] + 0.05) * SR)
            piece = a[i0:i1]
            dur = len(piece) / SR
            if dur < 1.0:
                continue
            rms = float(np.sqrt(np.mean(piece ** 2)) + 1e-9)
            pure = float(np.dot(embs[i], cent[k])) if embs[i] is not None and k in cent else 0.0
            cands.append({"i": i, "dur": dur, "rms": rms, "pure": pure, "piece": piece,
                          "text": (s.get("text_original") or s.get("text") or "").strip()})
        if not cands:
            continue
        loud = sorted(c["rms"] for c in cands)
        floor = loud[len(loud) // 4] * 0.5
        good = [c for c in cands if c["rms"] >= floor]
        pure_floor = sorted(c["pure"] for c in good)[len(good) // 3] if len(good) >= 3 else -1
        best = sorted([c for c in good if c["pure"] >= pure_floor and c["dur"] <= 15.0],
                      key=lambda c: (-c["pure"], -c["dur"])) or sorted(good, key=lambda c: -c["dur"])
        parts, texts, tot = [], [], 0.0
        for c in best:
            if tot >= 6.0 or (tot and tot + c["dur"] > 15.0):
                continue
            parts += [c["piece"], gap]
            texts.append(c["text"])
            tot += c["dur"]
        if not parts:
            c = best[0]
            parts, texts, tot = [c["piece"][: int(15 * SR)]], [c["text"]], min(c["dur"], 15.0)
        ref = np.concatenate(parts)
        ref = ref / (np.max(np.abs(ref)) + 1e-6) * 0.9
        path = WORK / f"ref_{k}.wav"
        save(path, ref)
        refs[k] = (path, " ".join(t for t in texts if t), tot)
        for c in cands:
            if 1.5 <= c["dur"] <= 12.0 and c["rms"] >= floor and c["pure"] >= 0.30 and c["text"]:
                p = c["piece"] / (np.max(np.abs(c["piece"])) + 1e-6) * 0.9
                op = WORK / f"own_{c['i']:04d}.wav"
                save(op, p)
                own[c["i"]] = (op, c["text"])
    return refs, own, cent


# ---------------------------------------------------------------- VoiceStudio on this machine
def _multipart(fields, files):
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode("utf-8")
    for k, (name, data, ctype) in files.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{name}\"\r\n"
                 f"Content-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    hdr = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    key = os.environ.get("OMNIVOICE_API_KEY", "")
    if key:
        hdr["Authorization"] = f"Bearer {key}"
    return body, hdr


_REF_BYTES = {}


def vs_generate(job, text, ref, ref_text, dst, seed, cfg, duration=None, instruct=None):
    # "raw": the model's own output. VoiceStudio's default "broadcast" preset adds two compressors,
    # a treble boost and a limiter: harsher, buzzier, noisier voice
    # ref None: no sample, the voice is designed from `instruct` alone
    fields = {"text": text, "language": job["language"], "ref_text": ref_text or "",
              "num_step": str(job["steps"]), "guidance_scale": f"{cfg:.2f}", "speed": str(job["speed"]),
              "seed": str(int(seed)), "effect_preset": "raw"}
    if duration:
        fields["duration"] = f"{duration:.2f}"
    instruct = job.get("instruct") if instruct is None else instruct
    if instruct:
        fields["instruct"] = instruct
    files = {}
    if ref is not None:
        if str(ref) not in _REF_BYTES:
            _REF_BYTES[str(ref)] = Path(ref).read_bytes()
        files["ref_audio"] = ("ref.wav", _REF_BYTES[str(ref)], "audio/wav")
    body, hdr = _multipart(fields, files)
    req = urllib.request.Request(VS_URL + "/generate", data=body, method="POST", headers=hdr)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                Path(dst).write_bytes(r.read())
                aid = r.headers.get("X-Audio-Id")
            if aid:
                _forget(aid)
            return True
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                say(f"   VoiceStudio nu a generat o varianta: {str(e)[:150]}")
                return False
            time.sleep(4)


def _forget(aid):
    """The takes are ours now: out of VoiceStudio's history (it would fill up)."""
    try:
        _, hdr = _multipart({}, {})
        hdr.pop("Content-Type", None)
        urllib.request.urlopen(urllib.request.Request(f"{VS_URL}/history/{aid}", method="DELETE", headers=hdr),
                               timeout=20).read()
    except Exception:  # noqa: BLE001
        pass


def vs_profile(name, dst):
    """A voice saved in this machine's VoiceStudio (by name): its sample -> dst (wav, SR) and its
    text. None when there is no such voice (the PC copies the user's voices here before the job)."""
    _, hdr = _multipart({}, {})
    hdr.pop("Content-Type", None)
    try:
        with urllib.request.urlopen(urllib.request.Request(VS_URL + "/profiles", headers=hdr), timeout=30) as r:
            profs = json.loads(r.read().decode("utf-8", "replace"))
        p = next((p for p in profs if str(p.get("name") or "").strip().lower() == name.strip().lower()), None)
        if not p:
            return None
        raw = WORK / f"profil_{uuid.uuid4().hex[:8]}"
        with urllib.request.urlopen(urllib.request.Request(f"{VS_URL}/profiles/{p['id']}/audio", headers=hdr),
                                    timeout=120) as r:
            raw.write_bytes(r.read())
        to_wav(raw, dst)
        raw.unlink(missing_ok=True)
        return Path(dst), " ".join(str(p.get("ref_text") or "").split())
    except Exception as e:  # noqa: BLE001
        say(f"   vocea nativa '{name}': nu o pot lua din VoiceStudio ({str(e)[:120]})")
        return None


def vs_hear(path, lang):
    body, hdr = _multipart({"model": "whisper-1", "language": lang, "response_format": "json"},
                           {"file": ("take.wav", Path(path).read_bytes(), "audio/wav")})
    req = urllib.request.Request(VS_URL + "/v1/audio/transcriptions", data=body, method="POST", headers=hdr)
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return str(json.loads(r.read().decode("utf-8", "replace")).get("text") or "")
        except Exception:  # noqa: BLE001
            time.sleep(2)
    return None


def norm(t):
    t = unicodedata.normalize("NFKD", (t or "").lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return " ".join("".join(ch if ch.isalnum() else " " for ch in t).split())


RO_KEEP = set("abcdefghijklmnopqrstuvwxyzăîșț")


def norm_ro(t):
    """For the Romanian speech model: its letters (diacritics kept: ă is not a), â = î (same sound)."""
    t = unicodedata.normalize("NFC", (t or "").lower())
    t = t.replace("ş", "ș").replace("ţ", "ț").replace("â", "î")
    return " ".join("".join(ch if ch in RO_KEEP else " " for ch in t).split())


def _lev(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def cer(ref, hyp):
    a, b = norm(ref), norm(hyp)
    return _lev(a, b) / len(a) if a else 0.0


_RO_U = ["zero", "unu", "doi", "trei", "patru", "cinci", "șase", "șapte", "opt", "nouă"]
_RO_T = ["zece", "unsprezece", "doisprezece", "treisprezece", "paisprezece", "cincisprezece", "șaisprezece",
         "șaptesprezece", "optsprezece", "nouăsprezece"]
_RO_D = {2: "douăzeci", 3: "treizeci", 4: "patruzeci", 5: "cincizeci", 6: "șaizeci", 7: "șaptezeci",
         8: "optzeci", 9: "nouăzeci"}


def ro_words(n):
    """A number in Romanian words (0..999 999), as it is usually read. Only used to compare what Whisper
    heard (it writes digits) with the text (written in words)."""
    if n < 10:
        return _RO_U[n]
    if n < 20:
        return _RO_T[n - 10]
    if n < 100:
        d, u = divmod(n, 10)
        return _RO_D[d] + ("" if u == 0 else " și " + _RO_U[u])
    if n < 1000:
        h, r = divmod(n, 100)
        hs = "o sută" if h == 1 else ("două" if h == 2 else _RO_U[h]) + " sute"
        return hs + ("" if r == 0 else " " + ro_words(r))
    if n < 1000000:
        th, r = divmod(n, 1000)
        if th == 1:
            ts = "o mie"
        else:
            w = ro_words(th)
            if w.endswith("doisprezece"):
                w = w[:-len("doisprezece")] + "douăsprezece"
            elif w.endswith("doi"):
                w = w[:-3] + "două"
            ts = w + (" de mii" if th % 100 >= 20 or th % 100 == 0 else " mii")
        return ts + ("" if r == 0 else " " + ro_words(r))
    return str(n)


def spoken_ro(t):
    """Whisper's text with its digits and % read out in Romanian."""
    import re
    t = re.sub(r"(\d)[.,](\d{3})\b", r"\1\2", t or "")
    t = re.sub(r"(\d+)\s*%", lambda m: m.group(1) + " la sută", t)
    return re.sub(r"\d+", lambda m: ro_words(int(m.group())) if len(m.group()) <= 6 else m.group(), t)


# numbers as the VOICE says them (spoken_ro above only reads Whisper's digits back for comparing).
# Numbers whose words do not depend on what they count (3, 7, 20, 300...) are always read out;
# 1, 2, 12 and those ending in 1 / 2 only when the noun after them is known ("o oră", "două ore",
# "un an", "doi ani"), after a label ("pasul 1" -> "unu", "ora 2" -> "două"), before a month
# ("1 mai" -> "întâi mai"); otherwise they stay written as they were. Money, decimals, clock times,
# "a 2-a", ranges ("2-3 ore"), negative numbers.
_MASC = set("""
an ani om oameni copil copii frate frați băiat băieți ban bani dolar dolari leu lei euro cent cenți metru metri
kilometru kilometri centimetru centimetri milimetru milimetri kilogram gram minut secund câine câini pește pești
prieten prieteni bărbat bărbați polițist polițiști soldat soldați oaspete oaspeți pas pași dinte dinți pantof
pantofi ochi băiețel băieței fiu fii nepot nepoți elev elevi student studenți medic medici șef șefi frați pumni pumn
glonț gloanțe metrou etaj etaje bilet bilete telefon telefoane apartament
""".split())
_FEM_SING = set("noapte carte parte floare femeie cheie lume moarte vale minte sare baie oaie mie zi".split())
_FEM_PLUR = set("""
luni săptămâni nopți cărți mâini ori părți flori vești femei mii mașini bucăți surori pisici lecții întrebări
străzi țări uși rochii familii țigări pâini fiice nepoate prietene camere zile ore fete case
""".split())
_NOT_NOUN = set("""
e este sunt era erau fost fi a al ai au are am aveam avea va vor voi vom ar și sau ori dar iar că ca să nu mai
la de pe în din dintre după până peste sub lângă fără prin pentru cu care ce cine cum când unde dacă decât
""".split())
_LABEL = set("""
pasul numărul nr camera ziua capitolul pagina etapa runda nivelul episodul sezonul partea clasa linia poarta
sala apartamentul blocul scara etajul locul echipa punctul varianta versiunea anul jocul proba cartea filmul
""".split())
_HOUR = {"ora", "orele"}
_MONTHS = set("ianuarie februarie martie aprilie mai iunie iulie august septembrie octombrie noiembrie decembrie".split())
_MULT = {"mie": True, "mii": True, "milion": False, "milioane": True, "miliard": False, "miliarde": True}
_CURRENCY = {"$": ("dolar", "dolari", False), "€": ("euro", "euro", False), "£": ("liră", "lire", True)}
_ORD_M = ["", "primul", "al doilea", "al treilea", "al patrulea", "al cincilea", "al șaselea", "al șaptelea",
          "al optulea", "al nouălea", "al zecelea"]
_ORD_F = ["", "prima", "a doua", "a treia", "a patra", "a cincea", "a șasea", "a șaptea", "a opta", "a noua",
          "a zecea"]


def _de(n):
    """Romanian puts "de" between a number and its noun from 20 on ("20 de ani", "105 ani")."""
    return n % 100 >= 20 or (n >= 100 and n % 100 == 0)


def _gendered(n):
    """The number's words change with the gender of what it counts (1, 2, 12, 21, 32, 101...)."""
    r = n % 100
    return n == 1 or r == 12 or (r % 10 == 2 and r != 12) or (r % 10 == 1 and r != 11 and n > 20)


def num_ro(n, fem=False, before_noun=False):
    """A number in words with the gender of what it counts: 1 -> un/o (unu alone), 2 -> doi/două,
    12 -> doisprezece/douăsprezece, 21 -> douăzeci și unu/una."""
    if n == 1 and before_noun:
        return "o" if fem else "un"
    w = ro_words(n)
    if fem:
        if w.endswith("doisprezece"):
            w = w[:-len("doisprezece")] + "douăsprezece"
        elif w.endswith("doi"):
            w = w[:-3] + "două"
        elif w.endswith("unu") and n > 20:
            w = w[:-3] + "una"
    return w


def _noun_gender(word, n):
    """"f" / "m" for the noun counted by n, None when not sure (then the digits stay)."""
    w = (word or "").lower()
    if not w:
        return None
    if w in _MASC:
        return "m"
    if n == 1:                                       # singular
        if w.endswith(("ă", "a")) or w in _FEM_SING:
            return "f"
        if w[-1] not in "eiăaâî":                    # ends in a consonant or -u: un an, un minut, un leu
            return "m"
        return None
    if w.endswith(("e", "uri")) or w in _FEM_PLUR:   # plural: feminine and neuter count alike
        return "f"
    return None


def _amount(s):
    """'1.000.000' / '1,000' / '2,5' / '3.50' -> (integer part, decimal digits or ''), as written."""
    import re
    m = re.fullmatch(r"(\d{1,3}(?:[.,]\d{3})+)(?:[.,](\d{1,2}))?", s)    # with a currency: thousands
    if m:
        return int(re.sub(r"[.,]", "", m.group(1))), m.group(2) or ""
    m = re.fullmatch(r"(\d+)(?:[.,](\d{1,2}))?", s)
    if m:
        return int(m.group(1)), m.group(2) or ""
    return None, ""


def _frac(d):
    return " ".join(_RO_U[int(c)] for c in d) if d.startswith("0") else num_ro(int(d))


def read_numbers_ro(t):
    """The digits, money, decimals and clock times of a text written out as a Romanian voice
    should say them (what is not sure stays as written)."""
    import re
    if not t or not any(ch.isdigit() for ch in t):
        return t or ""

    def ordinal(m):
        n = int(m.group(2))
        if not 1 <= n <= 10:
            return m.group(0)
        return (_ORD_F if m.group(1).lower() == "a" else _ORD_M)[n]
    t = re.sub(r"\b(al|a)\s+(\d{1,2})-(lea|a)\b", ordinal, t, flags=re.IGNORECASE)

    def money(m):
        sym = m.group(1) or m.group(5)
        digits = m.group(2) or m.group(4)
        mult = (m.group(3) or "").lower()
        one, many, fem = _CURRENCY[sym]
        n, dec = _amount(digits)
        if n is None:
            return m.group(0)
        if mult:                                      # "$2,5 milioane" -> "două virgulă cinci milioane de dolari"
            head = num_ro(n, _MULT[mult]) + (f" virgulă {_frac(dec)}" if dec else "")
            if n == 1 and not dec:
                head = "o" if _MULT[mult] else "un"
            return f"{head} {mult} de {many}"
        if n > 999999:
            return f"{n} de {many}"
        if dec:
            return f"{num_ro(n, fem)} virgulă {_frac(dec)} {many}"
        if n == 1:
            return f"{num_ro(1, fem, True)} {one}"
        return f"{num_ro(n, fem)} {'de ' if _de(n) else ''}{many}"
    num = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
    mult = r"(?:\s+(mii|mie|milion|milioane|miliard|miliarde)\b)?"
    # the amount whole: not cut off before a letter or another digit ("$15k" stays as written)
    t = re.sub(rf"([$€£])\s?({num}){mult}(?![^\W_])|(?<![\w.,])({num})\s?([$€£])", money, t)
    t = re.sub(r"(?<![\w.,])(\d{1,3}(?:\.\d{3})+)(?![\d.,])", lambda m: m.group(1).replace(".", ""), t)  # 1.000.000
    t = re.sub(r"(?<![\w.,])(\d{1,3}(?:,\d{3}){2,})(?![\d.,])", lambda m: m.group(1).replace(",", ""), t)  # 1,000,000
    t = re.sub(r"(?<![\w.,])(\d{1,3}(?:\.\d{3})+),(\d{1,2})(?!\d)", lambda m: m.group(1).replace(".", "") + ","
               + m.group(2), t)                                                                       # 1.000,50
    t = re.sub(r"(?<![\w.,])(\d{1,3}) (\d{3})(?![\d.,])", r"\1\2", t)                                 # 10 000

    def clock(m):
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 24 or mi > 59:
            return m.group(0)
        hw = "unu" if h == 1 else num_ro(h, fem=True)
        return hw if mi == 0 else f"{hw} și {num_ro(mi)}"
    t = re.sub(r"\b(\d{1,2}):(\d{2})\b", clock, t)

    def decimal(m):
        return f"{num_ro(int(m.group(1)))} virgulă {_frac(m.group(2))}"
    t = re.sub(r"(?<![\w.,])(\d{1,6})[.,](\d{1,2})(?![\d.,])", decimal, t)
    t = re.sub(r"(?<![\w.,])(\d{1,6}),(\d{3})(?![\d.,])", decimal, t)        # "2,500" in Romanian: a decimal
    t = re.sub(r"(\d+)\s*%", lambda m: m.group(1) + " la sută", t)
    t = re.sub(r"(?<![\w\d-])-(\d)", r"minus \1", t)                          # -5 (not a range, not COVID-19)
    src = t

    def integer(m):
        s = m.group(0)
        if len(s) > 1 and s.startswith("0"):                  # a code / phone: digit by digit
            return " ".join(_RO_U[int(c)] for c in s)
        n = int(s)
        if n > 999999:
            return s
        if not _gendered(n):
            return ro_words(n)
        before = re.findall(r"([^\W\d_]+)[\s.]*$", src[:m.start()])
        prev = before[0].lower() if before else ""
        if prev in _HOUR:                                     # "ora 2" -> "ora două", "ora 1" -> "ora unu"
            return "unu" if n == 1 else num_ro(n, fem=True)
        if prev in _LABEL:                                    # "pasul 1", "camera 2": the plain number
            return ro_words(n)
        nxt = re.match(r"(?:\s*[-–]\s*\d+)?\s+(?:de\s+)?([^\W\d_]+)", src[m.end():])
        noun = nxt.group(1).lower() if nxt else ""
        if noun in _MONTHS:                                   # "1 mai" -> "întâi mai", "2 iunie" -> "doi iunie"
            return "întâi" if n == 1 else ro_words(n)
        if not noun or noun in _NOT_NOUN:                     # counting without a noun: "Am 2." -> "doi"
            if n == 1 and noun in ("dintre", "din"):
                return s                                      # "1 dintre ei": unul / una, not sure
            return ro_words(n)
        g = _noun_gender(noun, n)
        if g is None:
            return s
        return num_ro(n, g == "f", before_noun=True)
    return re.sub(r"(?<![^\W_])\d+(?![^\W_])", integer, t)     # whole numbers not glued to letters ("mp3", "15k")


def cer_ro(ref, hyp):
    a, b = norm_ro(ref), norm_ro((hyp or "").replace("[UNK]", " ").replace("[PAD]", " "))
    return _lev(a, b) / len(a) if a else 0.0


# ---------------------------------------------------------------- words the take does not say
# number words (no diacritics, as norm() leaves them): Whisper writes numbers in digits, the text in
# words, so a number "missing" next to digits in what was heard is not missing
_NUM = set("""
zero unu una doi doua trei patru cinci sase sapte opt noua zece unsprezece doisprezece douasprezece
treisprezece paisprezece cincisprezece saisprezece saptesprezece optsprezece nouasprezece douazeci
treizeci patruzeci cincizeci saizeci saptezeci optzeci nouazeci suta sute mie mii milion milioane
miliard miliarde si de la lei euro dolari procent
un une deux trois quatre cinq six sept huit neuf dix onze douze treize quatorze quinze seize vingt
trente quarante cinquante soixante cent cents mille million millions milliard et virgule pour
one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen
seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand
million billion and percent
eins zwei drei vier funf sechs sieben acht neun zehn elf zwolf zwanzig dreissig vierzig funfzig
hundert tausend und
uno dos tres cuatro cinco seis siete ocho nueve diez veinte treinta cuarenta cincuenta cien ciento
mil y due tre quattro cinque sette otto nove dieci venti trenta cento mille
""".split())


def missing_words(text, heard, lang=None, known_names=()):
    """Runs of words of the text that the take does not say. Compared letter by letter (spaces
    ignored), so a name Whisper writes joined or split ("Cho Cheng Yu" / "Kochengyu") or a word
    spelled a little differently still counts as said. Only real gaps count: 2+ words of 3+ letters
    in a row (Whisper often skips "de", "a", "si" on its own); number words are not a gap when
    Whisper heard digits. -> the missing runs (text words); [] when nothing is missing or nothing
    was heard."""
    import difflib
    if heard is None:
        return []
    t = norm(text).split()
    raw = spoken_ro(heard) if lang == "ro" else heard
    if not t:
        return []
    digits = any(ch.isdigit() for ch in heard or "")
    names = _name_words(text) | {x for n in known_names for x in norm(n).split()}
    ts = "".join(t)
    owner = [i for i, w in enumerate(t) for _ in w]            # letter -> its word
    hs = "".join(norm(raw).split())
    said = [0] * len(t)
    for b in difflib.SequenceMatcher(None, ts, hs, autojunk=False).get_matching_blocks():
        if b.size >= 3:                                        # 1-2 letters in common: chance
            for p in range(b.a, b.a + b.size):
                said[owner[p]] += 1
    gone = [said[i] < 0.5 * len(w) for i, w in enumerate(t)]
    runs, cur = [], []
    for i, w in enumerate(t + [""]):
        if i < len(t) and gone[i]:
            cur.append(w)
            continue
        # names do not count: Whisper writes a foreign name as it sounds to it ("Xiao Yue" -> "si au ei")
        real = [x for x in cur if len(x) >= 3 and not (digits and x in _NUM) and x not in names]
        if len(real) >= 2:
            runs.append(" ".join(cur))
        cur = []
    return runs


def _name_words(text, words=None):
    """The words of the text that are names (as norm() writes them, or as `words` cuts a word):
    capitalised, and not just because a sentence starts there (or next to another capitalised
    word: "Sou Fen")."""
    import re
    words = words or (lambda w: norm(w).split())
    toks = []                                   # (word as written, starts a sentence)
    start = True
    for m in re.finditer(r"[^\W_]+|[.!?…]", text or ""):
        w = m.group()
        if w in ".!?…":
            start = True
            continue
        toks.append((w, start))
        start = False
    caps = [w[:1].isupper() for w, _ in toks]
    out = set()
    for i, (w, st) in enumerate(toks):
        if not caps[i]:
            continue
        if not st or (i + 1 < len(toks) and caps[i + 1] and not toks[i + 1][1]):
            for x in words(w):
                out.add(x)
    return out


# ---------------------------------------------------------------- what the voice reads (point 4)
_CEDILLA = str.maketrans({"ş": "ș", "Ş": "Ș", "ţ": "ț", "Ţ": "Ț", "ã": "ă", "Ã": "Ă"})
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_SYMBOLS_RO = (("&", " și "), ("%", " la sută"), ("€", " euro"), ("$", " dolari"), ("+", " plus "))


def clean_lexicon(rows, lang="ro"):
    """[(written, said), ...] from the job: pairs only, both non-empty, at most 400, no line breaks;
    written the way the text is (composed letters; for Romanian ș/ț with a comma, so a dictionary typed
    on the old keyboard with ş/ţ still matches)."""
    out = []
    for r in rows or []:
        try:
            a, b = (" ".join((unicodedata.normalize("NFC", str(x)).translate(_CEDILLA) if lang == "ro"
                              else unicodedata.normalize("NFC", str(x))).split()) for x in r[:2])
        except (TypeError, ValueError):
            continue
        if a and b and len(a) <= 60 and len(b) <= 80 and a.lower() != b.lower():
            out.append((a, b))
    return out[:400]


def lexicon_aid(written, said):
    """A dictionary entry that only helps the voice say the same word ("fiica => fiiica", "copilul =>
    copi-lul": the same letters once hyphens / doubled letters are set aside), as opposed to a real
    respelling ("Jack => Gec"). The judge hears an aided word against its real spelling."""
    import re

    def squash(w):
        w = unicodedata.normalize("NFC", w.lower()).translate(_CEDILLA)
        return re.sub(r"(.)\1+", r"\1", "".join(ch for ch in w if ch.isalpha()))
    return squash(written) == squash(said)


def apply_lexicon(text, lexicon):
    """Whole-word, case-insensitive respelling (one pass, longest first, so a respelling is never
    respelled again); a word written with a capital keeps it ("Jack" -> "Gec", "JACK" -> "GEC")."""
    import re
    if not text or not lexicon:
        return text or ""
    best = {}
    for a, b in lexicon:
        best.setdefault(a.lower(), b)             # the first entry for a word wins (the user's own first)
    keys = sorted(best, key=len, reverse=True)

    def edge(ch):
        return ch.isalnum() or ch == "_"
    alts = [(r"(?<![\w])" if edge(k[0]) else "") + re.escape(k) + (r"(?![\w])" if edge(k[-1]) else "")
            for k in keys]
    pat = re.compile("|".join(alts), re.IGNORECASE)

    def rep(m):
        w = m.group(0)
        b = best.get(w.lower())
        if b is None:
            return w
        if w.isupper() and len(w) > 1:
            return b.upper()
        if w[:1].isupper():
            return b[:1].upper() + b[1:]
        return b
    return pat.sub(rep, text)


def voice_text(text, lang, lexicon=()):
    """The text as the voice should read it (the subtitles keep the real one): composed letters,
    ș/ț with a comma (not the old cedilla forms), the dictionary's respellings (names, words the
    model says wrong), and for Romanian digits / symbols read out in words."""
    t = unicodedata.normalize("NFC", text or "").translate(_INVISIBLE)
    if lang == "ro":
        t = t.translate(_CEDILLA)
    t = apply_lexicon(t, lexicon)
    if lang == "ro":
        t = read_numbers_ro(t)                    # numbers, money, times as a Romanian says them
        import re
        for a, b in _SYMBOLS_RO:                  # what is left: a symbol on its own ("Tom & Jerry")
            t = re.sub(r"(?<![^\W_])" + re.escape(a) + r"(?![^\W_])", f" {b.strip()} ", t)
    return " ".join(t.split())


# ---------------------------------------------------------------- pronunciation, sound by sound (point 5)
# The Romanian speech model (no language model) is asked how likely each letter of the text is
# where it should be heard: the text is aligned on its output (CTC forced alignment) and every
# letter gets the model's probability at its place. A letter said right ~1.0; said as another
# sound (ă as a, a dark English l, a dropped î) low. Words with letters a Romanian reads
# differently (k, q, w, y) and names written the foreign way are not judged.
# calibrated on 72 native and 16 accented takes (8 foreign voices) of OmniVoice saying the same Romanian
# lines: a line is redone at 2+ badly said words or overall sounds under PRON_GOOD (native 4% / accented
# 88% of lines); "perfect" = PRON_EXCELLENT with no bad word (native 74% / accented 6%)
PRON_EXCELLENT = 0.98      # line: mean probability of its letters for "perfect"
PRON_GOOD = 0.97           # ... for "good enough" (with at most one doubtful word)
WORD_BAD_MIN = 0.05        # a word with a letter this unlikely: that sound is missing / another one
WORD_BAD_MEAN = 0.45       # ... or a word this unclear overall
WORD_WEAK_MIN = 0.35
WORD_BAD_MIN_LEN = 2         # shorter words count in the mean but are never "bad"
INTERJECTIONS = set("""
ah aha ahh aoleu ăă ăăă eh ehe hei hm hmm hmmm îhî îî mda mhm oh of oho pfu pff uau uf uh uhm
vai ha haha hah hihi hă brr pst șșt sst
""".split())


def _interjection(w):
    """Sounds rather than words: a listed one, one letter held ("mmm", "aaa"), a laugh ("hahaha"),
    or a word drawn out ("nuuu", "daaa": a run of 3 of one letter; not "iii", copiii is a word)."""
    import re
    return (w in INTERJECTIONS or bool(re.fullmatch(r"(.)\1+", w))
            or bool(re.fullmatch(r"(h[aeiouăî])\1+h?", w))
            or bool(re.search(r"([^i])\1\1", w)))


_RO_LETTERS = set("abcdefghijlmnoprstuvxzăâîșț")
_RO_CANON = {"â": "î", "ş": "ș", "ţ": "ț"}


def ro_ctc_words(text):
    """The words of a text in the speech model's letters (lowercase, ș/ț with comma, other signs as
    spaces). The scorer and the judge both cut words this way, so their word lists match."""
    t = unicodedata.normalize("NFC", (text or "").lower()).translate(_CEDILLA)
    return "".join(ch if ch.isalpha() else " " for ch in t).split()


def ctc_force_align(lp, tokens, blank):
    """Viterbi CTC alignment. lp: T x V log-probabilities (numpy), tokens: the text as token ids
    (no blanks). -> for every frame the index of the token it belongs to, or -1 (blank); None when
    the text cannot fit in these frames."""
    import numpy as np
    T, L = int(lp.shape[0]), len(tokens)
    if L == 0 or T == 0:
        return None
    ext = np.full(2 * L + 1, blank, dtype=np.int64)
    ext[1::2] = tokens
    S = len(ext)
    neg = -1e30
    skip = np.zeros(S, dtype=bool)
    if S > 2:
        skip[2:] = (ext[2:] != blank) & (ext[2:] != ext[:-2])
    emit = lp[:, ext].astype(np.float64)
    dp = np.full(S, neg)
    dp[0] = emit[0, 0]
    dp[1] = emit[0, 1]
    back = np.zeros((T, S), dtype=np.int8)
    for t in range(1, T):
        p1 = np.empty(S)
        p1[0] = neg
        p1[1:] = dp[:-1]
        p2 = np.full(S, neg)
        p2[2:] = dp[:-2]
        p2[~skip] = neg
        cand = np.stack([dp, p1, p2])
        arg = np.argmax(cand, axis=0)
        dp = cand[arg, np.arange(S)] + emit[t]
        back[t] = arg
    ends = [S - 1, S - 2] if S >= 2 else [S - 1]
    s = max(ends, key=lambda e: dp[e])
    if dp[s] < -1e29:
        return None
    path = np.empty(T, dtype=np.int64)
    for t in range(T - 1, -1, -1):
        path[t] = s
        s -= int(back[t, s])
    if path[0] > 1:
        return None
    return [int((x - 1) // 2) if x % 2 else -1 for x in path]


# How a native Romanian really speaks, so the judge does not call it wrong (measured on native takes):
#   "hyphen": "dă-mi", "s-a", "într-o" are said as one word (no pause between the pieces)
#   "elide":  a word-initial "î" after a word ending in a vowel is swallowed ("putea îneca" -> "puteaneca")
#   "voice":  a word-final consonant may take its voiced / voiceless pair ("scris chiar" -> "scriz")
#   "double": of a doubled vowel ("ideea", "fiica", "copiii") the clearer one counts
#   "fin_i":  a word-final "i" after a consonant is not a syllable ("ochi", "pomi", "dă-mi")
PRON_RULES = ("elide", "voice")      # measured: native false alarms 11% -> 4%, as many caught ("double"
                                     # would save 1 line in 72 but hide "fiica" said "fica")
_VOWELS = set("aeiouăî")
_VOICE_PAIRS = {"s": "z", "z": "s", "t": "d", "d": "t", "c": "g", "g": "c", "p": "b", "b": "p", "f": "v", "v": "f",
                "ș": "j", "j": "ș"}


def word_joins(text):
    """For each word of ro_ctc_words(text): True when a hyphen joins it to the word before ("dă-mi")."""
    t = unicodedata.normalize("NFC", (text or "").lower()).translate(_CEDILLA)
    out = []
    for chunk in t.split():
        placed = False
        for si, seg in enumerate(chunk.split("-")):
            for k, w in enumerate("".join(ch if ch.isalpha() else " " for ch in seg).split()):
                out.append(bool(si > 0 and k == 0 and placed))
                placed = True
    return out


def ctc_word_scores(logits, text, vocab, blank, delim=None, rules=None):
    """How well each word of the text is pronounced, from the speech model's output (T x V logits,
    numpy) on the take. -> [[min letter prob, mean letter prob, extra sounds], ...] for the words of
    ro_ctc_words(text) (None for a word the model has no letters for), or None when the text does
    not fit at all. rules: which of PRON_RULES' allowances for natural speech apply (default: those)."""
    import numpy as np
    rules = set(PRON_RULES if rules is None else rules)
    words = ro_ctc_words(text)
    if not words:
        return None
    joins = word_joins(text) if "hyphen" in rules else [False] * len(words)
    if len(joins) != len(words):
        joins = [False] * len(words)
    x = logits.astype(np.float64)
    x -= x.max(axis=1, keepdims=True)
    P = np.exp(x)
    P /= P.sum(axis=1, keepdims=True)
    canon = {}                       # letters that are the same sound (â/î; old ş/ţ): one column
    for tok, i in vocab.items():
        c = _RO_CANON.get(tok, tok)
        canon.setdefault(c, []).append(i)
    col = {}
    for c, ids in canon.items():
        if len(ids) > 1:
            P[:, ids[0]] = P[:, ids].sum(axis=1)
            P[:, ids[1:]] = 0.0      # one column per sound: the other one must never look like an extra sound
        col[c] = ids[0]
    tokens, owner, letters, judged = [], [], [], []   # owner: word of each token (-1 = the word delimiter)
    for wi, w in enumerate(words):
        ids, chs = [], []
        for ch in w:
            ch = _RO_CANON.get(ch, ch)
            if ch == "y" and "y" not in col:
                ch = "i"
            ids.append(col.get(ch))
            chs.append(ch)
        if None in ids:
            keep = [(i, ch) for i, ch in zip(ids, chs) if i is not None]
            owner_w = -2 - wi        # letters the model lacks: aligned, never judged
        else:
            keep = list(zip(ids, chs))
            owner_w = wi
        if tokens and delim is not None and not joins[wi]:
            tokens.append(delim)
            owner.append(-1)
            letters.append("|")
            judged.append(False)
        for n, (i, ch) in enumerate(keep):
            ok = True
            if "elide" in rules and n == 0 and ch == "î" and wi > 0 and words[wi - 1][-1:] in _VOWELS:
                ok = False
            if "fin_i" in rules and n == len(keep) - 1 and n > 0 and ch == "i" and keep[n - 1][1] not in _VOWELS:
                ok = False
            tokens.append(i)
            owner.append(owner_w)
            letters.append(ch)
            judged.append(ok)
    if not tokens:
        return None
    lp = np.log(np.maximum(P, 1e-12))
    path = ctc_force_align(lp, tokens, blank)
    if path is None:
        return None
    best = [0.0] * len(tokens)
    for t, j in enumerate(path):
        if j >= 0:
            p = float(P[t, tokens[j]])
            if "voice" in rules and letters[j] in _VOICE_PAIRS and _VOICE_PAIRS[letters[j]] in col and \
                    (j + 1 >= len(tokens) or owner[j + 1] != owner[j]):
                p = max(p, float(P[t, col[_VOICE_PAIRS[letters[j]]]]))       # word-final: its pair counts too
            best[j] = max(best[j], p)
    if "double" in rules:            # a doubled vowel: the clearer of the two counts for both
        for j in range(1, len(tokens)):
            if letters[j] in _VOWELS and letters[j] == letters[j - 1] and owner[j] == owner[j - 1]:
                best[j] = best[j - 1] = max(best[j], best[j - 1])
    # sounds the text does not have: a confident letter on a frame the alignment gives to "nothing"
    extra = [0] * len(words)
    nb = np.array([i for i in range(P.shape[1]) if i != blank and i != delim])
    prev_j = -1
    run_tok = None
    for t, j in enumerate(path):
        if j >= 0:
            prev_j, run_tok = j, None
            continue
        k = int(nb[np.argmax(P[t, nb])])
        if P[t, k] < 0.6:
            run_tok = None
            continue
        nxt = next((jj for jj in path[t + 1:] if jj >= 0), -1)
        near = {tokens[prev_j] if prev_j >= 0 else None, tokens[nxt] if nxt >= 0 else None}
        if k in near or k == run_tok:
            continue
        run_tok = k
        wi = owner[prev_j] if prev_j >= 0 and owner[prev_j] >= 0 else (owner[nxt] if nxt >= 0 else -1)
        if wi >= 0:
            extra[wi] += 1
    out = []
    for wi in range(len(words)):
        ps = [best[j] for j in range(len(tokens)) if owner[j] == wi and judged[j]]
        out.append([round(min(ps), 4), round(sum(ps) / len(ps), 4), extra[wi]] if ps else None)
    return out


def pron_eval(say, pw, names=()):
    """The judge's verdict on a take from its word scores: (pronunciation 0..1 over the words that
    count, [badly said words], [doubtful words]). Names, interjections and words with foreign
    letters do not count; one-letter words count in the mean but are never "bad"."""
    words = ro_ctc_words(say)
    if not pw or len(pw) != len(words):
        return None, [], []
    skip = set()
    for n in names or ():
        skip.update(ro_ctc_words(n))
    skip |= _name_words(say, ro_ctc_words)
    tot = n = 0.0
    bad, weak = [], []
    for w, s in zip(words, pw):
        if s is None or w in skip or _interjection(w) or not set(w) <= _RO_LETTERS:
            continue
        wmin, wmean, extra = s
        tot += wmean * len(w)
        n += len(w)
        if len(w) < WORD_BAD_MIN_LEN:
            continue
        if wmin < WORD_BAD_MIN or wmean - 0.25 * extra < WORD_BAD_MEAN:
            bad.append(w)
        elif wmin < WORD_WEAK_MIN or extra:
            weak.append(w)
    return (round(tot / n, 4) if n else None), bad, weak


def source_words(heard, original, text):
    """Words of the ORIGINAL line (its source language) that the take says although the
    translation does not have them: the voice copied the reference ("You're not drinking any",
    "Big,") instead of speaking the translation. Names, being in both, are not counted."""
    if not heard or not original:
        return []
    src = {w for w in norm(original).split() if len(w) >= 3}
    tgt = set(norm(text).split())
    return [w for w in norm(heard).split() if len(w) >= 3 and w in src and w not in tgt]


def sentences(text):
    """The text cut into its sentences (each keeps its punctuation); a piece of under 3 words stays
    with its neighbour, so no part is a lone word."""
    import re
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", (text or "").strip()) if p.strip()]
    out = []
    for p in parts:
        if out and (len(norm(p).split()) < 3 or len(norm(out[-1]).split()) < 3):
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return out


# ---------------------------------------------------------------- native voice (point 1)
def f0_median(a, sr=SR, fmin=60.0, fmax=500.0):
    """Median pitch (Hz) of the voiced parts of a voice (YIN): men ~90-150, women ~170-250,
    children higher. None when too little of it is voiced."""
    import numpy as np
    a = np.asarray(a, dtype="float64")
    w, tmax, tmin, hop = int(sr * 0.03), int(sr / fmin), int(sr / fmax), int(sr * 0.02)
    n = (len(a) - w - tmax) // hop
    if n <= 0:
        return None
    fr = a[np.arange(n)[:, None] * hop + np.arange(w + tmax)[None, :]]
    e = np.sqrt(np.mean(fr[:, :w] ** 2, axis=1))
    fr = fr[e > max(e.max() * 0.1, 1e-4)][:3000]
    if len(fr) < 10:
        return None
    nfft = 1 << int(np.ceil(np.log2(w + tmax + 1)))
    r = np.fft.irfft(np.fft.rfft(fr, nfft) * np.conj(np.fft.rfft(fr[:, :w], nfft)), nfft)[:, :tmax + 1]
    sq = np.concatenate([np.zeros((len(fr), 1)), np.cumsum(fr ** 2, axis=1)], axis=1)
    taus = np.arange(tmax + 1)
    d = sq[:, w][:, None] + (sq[:, w + taus] - sq[:, taus]) - 2.0 * r      # YIN difference function
    d[:, 0] = 0.0
    cmnd = np.ones_like(d)
    cmnd[:, 1:] = d[:, 1:] * taus[1:] / np.maximum(np.cumsum(d[:, 1:], axis=1), 1e-12)
    f0s = []
    for row in cmnd:
        below = np.nonzero(row[tmin:tmax] < 0.15)[0]
        if not len(below):
            continue
        t = tmin + int(below[0])
        while t + 1 < tmax and row[t + 1] < row[t]:
            t += 1
        f0s.append(sr / t)
    return float(np.median(f0s)) if len(f0s) >= 10 else None


def speaker_pitch(vocals, segs, spk, keys):
    """Per speaker: (median pitch, which native voice fits it: "m" or "f"), from its lines in the
    separated voice. A child gets the women's voice; an unknown pitch, None."""
    import numpy as np
    out = {}
    for key in keys:
        parts = [vocals[int(s["start"] * SR):int(s["end"] * SR)] for k, s in enumerate(segs) if spk[k] == key]
        parts = [p for p in parts if len(p) > SR // 4]
        f0 = f0_median(np.concatenate(parts)[: SR * 40]) if parts else None
        out[key] = (f0, None if f0 is None else ("m" if f0 < F0_MALE_MAX else "f"))
    return out


def designed_native(job, scorer, gen):
    """A native Romanian voice made here by OmniVoice's voice design (no sample), when the user gave
    none for this gender: a few tries, each judged (pronunciation sound by sound, sounds Romanian,
    natural, right pitch for the gender); the best is kept in NATIVE_DIR for the next clips.
    -> (wav, text, label) or None."""
    NATIVE_DIR.mkdir(parents=True, exist_ok=True)
    wav, meta = NATIVE_DIR / f"{gen}.wav", NATIVE_DIR / f"{gen}.json"
    try:
        m = json.loads(meta.read_text(encoding="utf-8"))
        if m.get("v") == NATIVE_VERSION and wav.exists():
            return wav, m["text"], m["label"]
    except (OSError, ValueError, KeyError):
        pass
    if scorer is None or not scorer.has.get("pron"):
        return None                       # unjudged it could be anything: not used
    tries = [(d.get("instruct") or "", int(d.get("seed") or 0))
             for d in (job.get("native_design") or {}).get(gen) or [] if isinstance(d, dict)]
    tries += [(ins, seed) for ins in NATIVE_DESIGN[gen] for seed in NATIVE_SEEDS]
    best = None
    say(f"   fac o voce nativa romaneasca de {'barbat' if gen == 'm' else 'femeie'} (o singura data)...")
    for n, (ins, seed) in enumerate(tries[:8]):
        dst = WORK / f"nativ_proba_{gen}_{n}.wav"
        if not vs_generate(job, NATIVE_TEXT, None, "", dst, seed=seed, cfg=2.0, instruct=ins):
            continue
        a = trim_silence(load(dst))
        save(dst, a)
        j = scorer.judge(None, dst, NATIVE_TEXT)
        pron, bad, _ = pron_eval(NATIVE_TEXT, j.get("pw"))
        f0 = f0_median(a)
        right = f0 is not None and ((f0 < F0_MALE_MAX) if gen == "m" else (f0 >= F0_MALE_MAX))
        if pron is None or pron < PRON_GOOD or len(bad) > 1 or not right \
                or (j.get("lid") is not None and j["lid"] < 0.8):
            continue
        q = 3.0 * pron - 0.3 * len(bad) + 0.5 * (j.get("utmos") or 3.0) + (j.get("lid") or 0.8)
        if best is None or q > best[0]:
            best = (q, dst, ins, seed, pron, j.get("lid"), j.get("utmos"), f0)
    if best is None:
        return None
    _, dst, ins, seed, pron, lid, mos, f0 = best
    label = f"facuta aici ({ins or 'auto'}, seed {seed})"
    tmp = NATIVE_DIR / f"{gen}.{uuid.uuid4().hex[:6]}.wav"
    shutil.copyfile(dst, tmp)
    os.replace(tmp, wav)
    meta.write_text(json.dumps({"v": NATIVE_VERSION, "text": NATIVE_TEXT, "label": label, "instruct": ins,
                                "seed": seed, "pron": pron, "lid": lid, "utmos": mos, "f0": round(f0)},
                               ensure_ascii=False), encoding="utf-8")
    return wav, NATIVE_TEXT, label


def native_bank(job, scorer, need):
    """The native Romanian voices for the genders in `need`: {gender: (wav, text, label)}. First the
    user's saved voices named in job["native_names"] ({"m": name, "f": name}), else voices made here.
    A gender still missing borrows the other one (the conversion gives it the speaker's timbre)."""
    bank = {}
    names = job.get("native_names") or {}
    for gen in ("m", "f"):
        name = str(names.get(gen) or "").strip()
        if name and gen in need:
            got = vs_profile(name, WORK / f"nativ_banca_{gen}.wav")
            if got and got[1]:
                bank[gen] = (got[0], got[1], name)
            elif got:
                say(f"   vocea nativa '{name}' nu are textul mostrei; nu o folosesc")
    for gen in need:
        if gen not in bank:
            made = designed_native(job, scorer, gen)
            if made:
                bank[gen] = made
    for gen in need:
        if gen not in bank and bank:
            other = next(iter(bank))
            bank[gen] = bank[other]
            say(f"   nu am voce nativa de {'barbat' if gen == 'm' else 'femeie'}; folosesc vocea {bank[other][2]} "
                "(conversia ii da oricum vocea personajului)")
    return bank


def start_native(job, scorer, vocals, segs, spk, keys, refs, ckeys):
    """Everything the native voice needs for this clip: the converter (Seed-VC) loading, each
    speaker's native voice (by its pitch) and its clean sample as the conversion target.
    -> (vc, native {key: (wav, text)}, target {key: wav}, base_sim {key: sim}, info) or Nones."""
    none = (None, {}, {}, {}, {})
    if scorer is None or not scorer.has.get("ecapa") or not scorer.has.get("pron"):
        say("   vocea nativa: fara verificarea vocii si a pronuntiei nu o folosesc")
        return none
    ok = setup_vc(wait=WIN)                       # on a GPU the worker installs it in the background
    if ok is None:
        say("   vocea nativa: conversia se instaleaza inca pe GPU; clipul asta merge fara ea")
        return none
    if not ok:
        try:
            why = (BASE / "venv_vc.esuat").read_text(encoding="utf-8").split("\t", 1)[-1][:150]
        except OSError:
            why = "?"
        say_once(f"   vocea nativa nu merge pe masina asta ({why}); clipul merge fara ea")
        return none
    try:
        vc = VC()                                 # its models load while the native voices are found
    except Exception as e:  # noqa: BLE001
        say(f"   vocea nativa: conversia nu porneste ({str(e)[:150]})")
        return none
    try:
        pitch = speaker_pitch(vocals, segs, spk, [k for k in keys if k in refs])
        want = {key: (g or "m") for key, (_, g) in pitch.items()}
        bank = native_bank(job, scorer, set(want.values()))
        if not bank:
            vc.close()
            say("   vocea nativa: nu am gasit si nu am putut face o voce nativa romaneasca; clipul merge fara ea")
            return none
        if not vc.wait():
            say(f"   vocea nativa: conversia nu porneste ({vc.last_error[:150]}); clipul merge fara ea")
            vc.close()
            return none
        native, target, base, info = {}, {}, {}, {}
        for key in keys:
            if key not in refs or key not in want:
                continue
            wav, text, label = bank.get(want[key]) or next(iter(bank.values()))
            native[key], target[key] = (wav, text), refs[key][0]
            # how much the native voice itself already sounds like the speaker: a converted take has
            # to beat this clearly, or it is the native voice being heard
            j = scorer.judge(ckeys.index(key) if key in ckeys else None, wav)
            base[key] = j.get("sim")
            f0 = pitch[key][0]
            info[key] = {"gen": want[key], "f0": round(f0) if f0 else None, "copil": bool(f0 and f0 > F0_CHILD_MIN),
                         "voce": label, "sim_nativ": round(base[key], 3) if base[key] is not None else None}
    except Exception:
        vc.close()
        raise
    say("   vocea nativa: " + ", ".join(f"{k} -> {v['voce']} ({'barbat' if v['gen'] == 'm' else 'femeie'}"
                                         + (f", {v['f0']} Hz" if v["f0"] else "") + ")" for k, v in info.items()))
    return vc, native, target, base, info


# ---------------------------------------------------------------- takes
# takes made from an accent-free source win unless clearly worse: "ro" = the speaker's own best
# Romanian lines as the sample; "nat" = a native Romanian voice converted to the speaker's timbre;
# "nr" = cloned from a sample of such converted native lines
KIND_BONUS = {"ro": 0.12, "nr": 0.15, "nat": 0.10}
ACCENT_FREE = ("nr", "ro", "nat")


def pron_ok(t):
    """Pronounced well enough, sound by sound (or not measured): at most one doubtful word and
    clear sounds overall."""
    return t.get("pron") is None or (t["pron"] >= PRON_GOOD and len(t.get("bad") or []) <= 1)


def said_right(t, strict=True):
    """Pronunciation of a take, judged sound by sound when that was measured (strict), otherwise by
    the letters the speech model heard."""
    if strict and t.get("pron") is not None:
        return t["pron"] >= PRON_EXCELLENT and not t.get("bad")
    return t.get("ctc") is None or t["ctc"] <= 0.10


def score(t, room, typical_sim, strict=True):
    """Higher is better: says the text (Whisper), pronounced right sound by sound (Romanian model),
    natural and clean (UTMOS), the speaker's voice (ECAPA, without rewarding a copy of the original's
    noise), little speed-up needed."""
    s = 0.0
    pron = t.get("pron") if strict else None
    if t.get("sim") is not None:
        # a copy of the original voice also copies its accent: the likeness counts, but less than before
        s += 0.6 * t["sim"]
        if typical_sim is not None and t["sim"] < typical_sim - 0.12:
            s -= 1.5 * (typical_sim - 0.12 - t["sim"])        # drifted away from the speaker
    if t.get("lid") is not None:
        # sounds like another language = foreign accent (less sure on very short lines); the
        # sound-by-sound check says more, so this weighs less when there is one
        s -= (0.5 if pron is not None else 0.8) * min(1.0, t["len"] / 2.5) * (1.0 - t["lid"])
    s += KIND_BONUS.get(t.get("kind"), 0.0)
    if t.get("wcer") is not None:
        s -= 2.0 * min(1.0, t["wcer"])
    if pron is not None:
        s -= 5.0 * (1.0 - pron)                              # native ~0.99 -> -0.05, accented ~0.93 -> -0.35
        s -= 0.35 * min(3, len(t.get("bad") or []))           # every badly said word
    elif t.get("ctc") is not None:
        s -= 1.5 * min(1.0, t["ctc"])
    if t.get("utmos") is not None:
        s += 0.3 * (t["utmos"] - 3.0)
    ratio = t["len"] / room
    # a take that needs a big speed-up sounds rushed: the shorter one wins when the rest is close
    return s - 0.3 * max(0.0, ratio - 1.0) - 1.0 * max(0.0, ratio - 1.35)


def excellent(t, room, typical_sim, strict=True):
    return ((t.get("wcer") is None or t["wcer"] <= 0.05)
            and said_right(t, strict)
            and (t.get("lid") is None or t["lid"] >= 0.6)
            and (t.get("utmos") is None or t["utmos"] >= 3.2)
            and t["len"] <= room * 1.05
            and (t.get("sim") is None or typical_sim is None or t["sim"] >= typical_sim))


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else None


def trim_silence(a, sr=SR, keep=0.04):
    """The take without the silence before and after the voice (a little of it kept)."""
    import numpy as np
    if len(a) < sr // 10:
        return a
    hop = int(sr * 0.01)
    n = len(a) // hop
    e = np.sqrt(np.mean(a[: n * hop].reshape(n, hop) ** 2, axis=1))
    on = np.nonzero(e > max(e.max() * 0.03, 1e-4))[0]
    if not len(on):
        return a
    i0 = max(0, on[0] * hop - int(sr * keep))
    i1 = min(len(a), (on[-1] + 1) * hop + int(sr * keep))
    return a[i0:i1]


class Lines:
    """All takes of all lines, the judging, and the choice."""

    def __init__(self, job, segs, spk, room, scorer, ckeys, vc=None, vc_target=None, base_sim=None):
        self.job, self.segs, self.spk, self.room = job, segs, spk, room
        self.scorer, self.ckeys = scorer, ckeys
        self.takes = {k: [] for k in range(len(segs))}
        self.natural = {}               # (line, ref kind) -> natural length of its first take
        self.n_gen = 0
        # every line from an accent-free sample whenever one of its takes is good enough: the
        # original voice of a foreign speaker always carries some of its accent
        self.prefer_ro = bool(job.get("mostra_romana", True) or job.get("native_voice"))
        self.strict = bool(job.get("pron_strict", True))     # pronunciation judged sound by sound
        # the clip's names (as subtitled and as read): never judged letter by letter
        self.names = list(job.get("names") or []) + [b for _, b in clean_lexicon(job.get("name_say"), job["lang"])]
        self.vc, self.vc_target = vc, dict(vc_target or {})  # native voice -> the speaker's timbre
        self.base_sim = dict(base_sim or {})       # how much the native voice itself sounds like the speaker
        self.vc_fail = 0

    def typical(self, key, kinds=None):
        """The speaker's usual likeness (median), over the takes that may be used (a take still
        sounding like the native voice does not count)."""
        orig = self.orig_sim(key)
        return median(t.get("sim") for k, ts in self.takes.items() if self.spk[k] == key for t in ts
                      if (kinds is None or t["kind"] in kinds) and self.timbre_ok(k, t, orig))

    def orig_sim(self, key):
        """Median likeness of the takes cloned from the speaker's own (clip) voice."""
        return median(x.get("sim") for kk, ts in self.takes.items() if self.spk[kk] == key
                      for x in ts if x["kind"] in ORIGINAL_KINDS)

    def timbre_ok(self, k, t, orig=False):
        """A take made from the native voice ("nat", "nr") must sound like the speaker, never like
        the native voice: clearly closer to the speaker than the native voice itself, and not far
        from the takes cloned from the clip. Unmeasured: not trusted. A take kept from the first
        run (second run of a clip) was already judged then."""
        if t["kind"] not in ("nat", "nr") or t.get("kept"):
            return True
        if t.get("sim") is None:
            return False
        key = self.spk[k]
        base = self.base_sim.get(key)
        if base is not None and t["sim"] < base + NAT_SIM_GAIN:
            return False
        orig = self.orig_sim(key) if orig is False else orig
        return orig is None or t["sim"] >= orig - NAT_SIM_DROP

    def gen(self, k, ref, kind, cfg, tag, split=False):
        """One take of line k. split=True: each sentence made on its own and joined (a model that
        skips a sentence of a long line says a short one whole). kind "nat": made with a native
        Romanian voice (ref), then converted to the speaker's timbre."""
        import numpy as np
        s = self.segs[k]
        say = s.get("say") or s["text"]
        heard_as = s.get("judge") or say               # what the judge expects (an aid's real word)
        key = self.spk[k]
        if kind == "nat" and (self.vc is None or not self.vc.ok or key not in self.vc_target):
            return None
        dst = WORK / f"take_{k:04d}_{kind}{'_bucati' if split else ''}_{tag}.wav"
        src = WORK / f"nativ_{k:04d}_{tag}.wav" if kind == "nat" else dst
        seed = 7919 * (k + 1) + sum(map(ord, kind)) * 31 + tag
        nat = self.natural.get((k, kind))
        parts = sentences(say) if split else None
        if split:
            if len(parts) < 2:
                return None
            gap = np.zeros(int(SR * 0.12), dtype="float32")
            pieces = []
            for n, p in enumerate(parts):
                pd = WORK / f"bucata_{k:04d}_{tag}_{n}.wav"
                if not vs_generate(self.job, p, ref[0], ref[1], pd, seed=seed + 101 * (n + 1), cfg=cfg):
                    return None
                self.n_gen += 1
                pieces += [trim_silence(load(pd)), gap]
                pd.unlink(missing_ok=True)
            save(src, np.concatenate(pieces[:-1]))
        else:
            target = None
            if nat is not None and nat > self.room[k] * 1.02:
                target = max(self.room[k], nat / MAX_FIT)     # only ever asked to be shorter, never longer
            if not vs_generate(self.job, say, ref[0], ref[1], src, seed=seed, cfg=cfg, duration=target):
                return None
            self.n_gen += 1
        if kind == "nat":
            ok = self.vc.convert(src, self.vc_target[key], dst)
            src.unlink(missing_ok=True)
            if not ok:
                self.vc_fail += 1
                if self.vc_fail >= 3 or not self.vc.ok:
                    say_once(f"   conversia vocii native nu merge ({self.vc.last_error[:120]}); continui fara ea")
                    self.vc.ok = False
                return None
        length = len(load(dst)) / SR
        t = {"path": str(dst), "kind": kind, "cfg": cfg, "len": length}
        if split:
            t["split"] = len(parts)
        j = (self.scorer.judge(self.ckeys.index(key) if key in self.ckeys else None, dst, heard_as)
             if self.scorer else {})
        t["sim"], t["utmos"], t["lid"] = j.get("sim"), j.get("utmos"), j.get("lid")
        if j.get("ctc") is not None:
            t["ctc_text"] = j["ctc"]
            t["ctc"] = cer_ro(heard_as, j["ctc"])
        if j.get("pw") is not None:
            t["pw"] = j["pw"]
            t["pron"], t["bad"], t["weak"] = pron_eval(heard_as, j["pw"], self.names)
        self.listen(k, t)
        if nat is None and not split and not t.get("miss"):
            self.natural[(k, kind)] = length          # a take that skipped words is no measure of the line
        self.takes[k].append(t)
        return t

    def listen(self, k, t):
        """What Whisper hears in the take, how far it is from the text, and which words it does not
        say. Words "missing" are checked a second time (the take with silence around it: Whisper
        sometimes drops the first or last words of a tight clip), so a good take is not thrown away.
        The text is compared both as written and as the voice read it (a name respelled for the
        voice, "Gec", may be written either way by Whisper)."""
        import numpy as np
        text, lang = self.segs[k]["text"], self.job["lang"]
        say = self.segs[k].get("say") or text
        names = self.names

        def hear(path):
            h = vs_hear(path, lang)
            if h is None:
                return None, None, []
            hh = spoken_ro(h) if lang == "ro" else h
            # the read-out text against what was heard, numbers read the same way on both sides
            w = min(cer(text, hh), cer(say, read_numbers_ro(h) if lang == "ro" else h)) if say != text                 else cer(text, hh)
            miss = missing_words(text, h, lang, names)
            if miss and say != text:
                m2 = missing_words(say, h, lang, names)
                if sum(len(x.split()) for x in m2) < sum(len(x.split()) for x in miss):
                    miss = m2
            return h, w, miss

        heard, wcer, miss = hear(t["path"])
        if miss:
            pad = np.zeros(int(SR * 0.5), dtype="float32")
            p2 = WORK / f"ascult_{uuid.uuid4().hex[:8]}.wav"
            save(p2, np.concatenate([pad, load(t["path"]), pad]))
            h2, w2, m2 = hear(p2)
            p2.unlink(missing_ok=True)
            if h2 is not None and sum(len(x.split()) for x in m2) < sum(len(x.split()) for x in miss):
                heard, wcer, miss = h2, w2, m2
        if miss and t.get("ctc_text"):
            # second opinion: the Romanian speech model (letters only, no guessing from context).
            # Words it hears were said: only what BOTH miss is missing (Whisper sometimes drops or
            # rewrites words that were spoken clearly)
            ctc_heard = t["ctc_text"].replace("[UNK]", " ").replace("[PAD]", " ")
            other = {w for src in {text, say} for run in missing_words(src, ctc_heard, None, names)
                     for w in run.split()}
            miss = [run for run in miss if other & set(run.split())]
        t["heard"], t["wcer"], t["miss"] = heard, wcer, miss
        t["leak"] = source_words(heard, self.segs[k].get("text_original") or "", text)

    def best(self, k):
        """The best take that says the whole text (and no words of the original language);
        only when none does, the best of the rest."""
        takes = [t for t in self.takes[k] if self.timbre_ok(k, t)]   # never the native voice as itself
        if not takes:
            return None
        typ = self.typical(self.spk[k])
        pool = [t for t in takes if not t.get("miss") and not t.get("leak")] or takes
        if self.prefer_ro:
            ro = [t for t in pool if t["kind"] in ACCENT_FREE and self.good_enough(k, t)]
            if ro:
                pool = ro
        return max(pool, key=lambda t: score(t, self.room[k], typ, self.strict))

    def good_enough(self, k, t):
        """Says the text (Whisper), in the right language, nothing skipped or copied; with the
        sound-by-sound check also pronounced well enough (at most one doubtful word)."""
        words = len(norm(self.segs[k]["text"]).split())
        return bool(not t.get("miss") and not t.get("leak") and self.timbre_ok(k, t)
                    and (t.get("wcer") is None or t["wcer"] <= (0.30 if words <= 2 else 0.15))
                    and (t.get("lid") is None or t["lid"] >= 0.6 or words < 3)
                    and (not self.strict or t.get("pron") is None
                         or (t["pron"] >= PRON_GOOD and len(t.get("bad") or []) <= 1)))

    def done(self, k):
        b = self.best(k)
        return (b is not None and not b.get("miss") and not b.get("leak")
                and excellent(b, self.room[k], self.typical(self.spk[k]), self.strict))

    def needs_fix(self, k):
        """Still wrong: skips words, Whisper hears something else, it sounds like another language,
        or (sound-by-sound check) a word is pronounced wrong."""
        b = self.best(k)
        if b is None:
            return True
        words = len(norm(self.segs[k]["text"]).split())
        # longer lines: what missing_words finds (names and digits excused; a plain error rate would
        # count every foreign name Whisper spells its own way); 1-2 words: the error rate itself
        return bool(b.get("miss") or b.get("leak")
                    or (b.get("wcer") is not None and b["wcer"] > (0.30 if words <= 2 else 0.50))
                    or (b.get("lid") is not None and b["lid"] < 0.6 and words >= 3)
                    or (self.strict and not pron_ok(b)))

    def run(self, k, plan):
        """plan: [(ref, kind, cfg), ...] (a step may be a function giving it, chosen only when its
        turn comes); stops as soon as a take is excellent."""
        for i, step in enumerate(plan):
            if self.done(k):
                break
            ref, kind, cfg = step() if callable(step) else step
            self.gen(k, ref, kind, cfg, len(self.takes[k]) + i)


# how strict the takes for the accent-free sample are: first the strict choice; when it finds too
# little for a speaker (a strong foreign accent makes every take score badly on pronunciation, which
# is exactly when the sample is needed most), the best of what there is
RO_REF_TIERS = (
    dict(lo=1.2, hi=10.0, wcer=0.05, ctc=0.12, pron=0.98, bad=0, sim=0.05, min_s=4.0),
    dict(lo=1.0, hi=12.0, wcer=0.10, ctc=0.26, pron=0.97, bad=1, sim=0.10, min_s=3.0),
    # last resort, as before the sound-by-sound check: a strong accent fails every take on it, and
    # that speaker needs the sample most (its best-pronounced takes still come first)
    dict(lo=1.0, hi=12.0, wcer=0.10, ctc=0.26, pron=None, bad=None, sim=0.10, min_s=3.0),
)
ORIGINAL_KINDS = ("mostra", "replica")      # takes cloned from the clip's own (foreign) voice


def _ro_candidates(lines, key, tier, typ, kinds=ORIGINAL_KINDS):
    out = []
    for k, ts in lines.takes.items():
        if lines.spk[k] != key:
            continue
        for t in ts:
            if (not tier["lo"] <= t["len"] <= tier["hi"] or t["kind"] not in kinds or t.get("miss")
                    or t.get("split") or t.get("leak") or not lines.timbre_ok(k, t)):
                continue                   # the sample's text must be exactly what it says
            if t.get("wcer") is not None and t["wcer"] > tier["wcer"]:
                continue
            pron = t.get("pron") if lines.strict else None
            if pron is not None and tier["pron"] is not None:   # sound by sound, when it was measured
                if pron < tier["pron"] or len(t.get("bad") or []) > tier["bad"]:
                    continue
            elif t.get("ctc") is not None and t["ctc"] > tier["ctc"]:
                continue
            if t.get("sim") is not None and typ is not None and t["sim"] < typ - tier["sim"]:
                continue
            if t.get("lid") is not None and t["lid"] < 0.3:
                continue                                   # too much of the original's accent
            # the clearest Romanian first: pronunciation weighs most
            said = (4.0 * (1.0 - pron) + 0.5 * len(t.get("bad") or [])) if pron is not None \
                else 4.0 * (t.get("ctc") or 0.0)
            q = ((t.get("utmos") or 3.0) + 2.0 * (t.get("sim") or 0.0) - said
                 - 3.0 * (t.get("wcer") or 0.0) + 2.0 * (t["lid"] if t.get("lid") is not None else 0.5))
            out.append((q, k, t))
    out.sort(key=lambda x: -x[0])
    return out


def romanian_refs(lines, keys, min_s=4.0, kinds=ORIGINAL_KINDS, tag="ro"):
    """Per speaker: its best Romanian lines so far (clear, well pronounced, natural, its voice)
    joined into one reference of 6-12 s, with the text the voice read. The strictly chosen lines
    first; when they make less than 6 s, the best of the rest fill it up (a 4 s sample clones worse).
    kinds: which takes may go in ("mostra"/"replica" for the speaker's own Romanian sample; "nat",
    the native voice converted to the speaker, for the native sample "nr")."""
    import numpy as np
    out = {}
    gap = np.zeros(int(SR * 0.3), dtype="float32")
    for key in keys:
        # converted native takes are compared among themselves (a clone of the clip is always closer)
        typ = lines.typical(key, None if tuple(kinds) == tuple(ORIGINAL_KINDS) else kinds)
        ordered = [(n, q, k, t) for n, tier in enumerate(RO_REF_TIERS)
                   for q, k, t in _ro_candidates(lines, key, tier, typ, kinds)]
        parts, texts, tot, used, worst = [], [], 0.0, set(), 0
        for n, q, k, t in ordered:
            if k in used or tot >= 6.0 or (tot and tot + t["len"] > 12.0):
                continue
            parts += [load(t["path"]), gap]
            texts.append((lines.segs[k].get("say") or lines.segs[k]["text"]).strip())
            used.add(k)
            tot += t["len"]
            worst = max(worst, n)
        need = min_s if worst == 0 else min(min_s, RO_REF_TIERS[worst]["min_s"])
        if tot < need:
            continue
        ref = np.concatenate(parts)
        ref = ref / (np.max(np.abs(ref)) + 1e-6) * 0.9
        path = WORK / f"ref_{tag}_{key}.wav"
        save(path, ref)
        out[key] = (path, " ".join(texts), tot)
    return out


# ---------------------------------------------------------------- timeline + mix
_RB = {}


def has_rubberband():
    """ffmpeg with Rubber Band (the GPU's own ffmpeg build and the full Windows builds have it):
    a much more natural speed-up of a voice than atempo, above all past ~1.2x."""
    if "ok" not in _RB:
        try:
            _RB["ok"] = " rubberband " in sh([FF, "-hide_banner", "-filters"], timeout=60)
        except Exception:  # noqa: BLE001
            _RB["ok"] = False
    return _RB["ok"]


def tempo_filter(tempo):
    if has_rubberband():
        # settings for speech: soft onsets, formants kept (the voice keeps its timbre), best quality
        return (f"rubberband=tempo={tempo:.4f}:transients=mixed:detector=soft:formant=preserved"
                ":pitchq=quality")
    out, t = [], tempo
    while t > 2.0:                     # atempo takes at most 2.0 per stage on old ffmpeg
        out.append("atempo=2.0")
        t /= 2.0
    out.append(f"atempo={t:.4f}")
    return ",".join(out)


def line_fx(a, tempo, treble_db):
    """Speed-up (tone kept) and/or a gentle treble cut on one line, through ffmpeg."""
    fx = []
    if tempo > 1.0005:
        fx.append(tempo_filter(tempo))
    if treble_db > 0.2:
        fx.append(f"treble=g={-treble_db:.1f}:f=5000:width_type=o:w=1")
    if not fx:
        return a
    src, dst = WORK / f"t_{uuid.uuid4().hex[:8]}.wav", WORK / f"u_{uuid.uuid4().hex[:8]}.wav"
    save(src, a)
    sh([FF, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-af", ",".join(fx),
        "-ar", str(SR), "-ac", "1", str(dst)])
    out = load(dst)
    src.unlink(missing_ok=True)
    dst.unlink(missing_ok=True)
    return out


def active_rms(a):
    import numpy as np
    if len(a) < 400:
        return float(np.sqrt(np.mean(a ** 2)) + 1e-9)
    fr = a[: len(a) // 400 * 400].reshape(-1, 400)
    e = np.sqrt(np.mean(fr ** 2, axis=1))
    loud = e[e > e.max() * 0.1]
    return float(np.sqrt(np.mean(loud ** 2)) + 1e-9) if len(loud) else float(e.mean() + 1e-9)


def hf_db(a, sr=SR):
    """Energy above 4 kHz relative to all (dB): how bright / hissy a voice is."""
    import numpy as np
    if len(a) < sr // 2:
        return None
    spec = np.abs(np.fft.rfft(a * np.hanning(len(a)))) ** 2
    f = np.fft.rfftfreq(len(a), 1 / sr)
    tot = spec[f > 60].sum()
    return float(10 * np.log10(spec[f > 4000].sum() / tot + 1e-12)) if tot > 0 else None


def rooms(segs, total):
    """Time each line may use: its slot, plus the silence after it (up to TAIL_MAX)."""
    out = []
    for i, s in enumerate(segs):
        nxt = segs[i + 1]["start"] if i + 1 < len(segs) else total
        end = max(s["end"], min(nxt, s["end"] + TAIL_MAX))
        out.append(max(0.3, end - s["start"]))
    return out


def loudness(path):
    """Integrated loudness (LUFS) of a file, by ffmpeg's loudnorm analysis."""
    out = sh([FF, "-hide_banner", "-nostats", "-i", str(path), "-af", "loudnorm=print_format=json",
              "-f", "null", "-"])
    try:
        js = out[out.rindex("{"): out.rindex("}") + 1]
        v = float(json.loads(js)["input_i"])
        return v if v > -70 else None
    except (ValueError, KeyError):
        return None


def master(vw, bed, final, total_s):
    """Voice (+ the original music when wanted) -> TikTok loudness. No compressor: only a gentle
    rumble cut, a light de-esser, one gain for the whole clip and a peak limiter."""
    pre = WORK / "premix.wav"
    last = None
    for voice_chain in ("highpass=f=75:p=2,highpass=f=75:p=2,deesser=i=0.15",
                        "highpass=f=75:p=2,highpass=f=75:p=2"):        # an old ffmpeg may lack deesser
        try:
            if bed:
                # amix halves both inputs; volume=2 puts them back (no "normalize" option on old ffmpeg)
                filt = (f"[0:a]{voice_chain},aresample={MIX_SR},pan=stereo|c0=c0|c1=c0[v];"
                        f"[1:a]aresample={MIX_SR},aformat=channel_layouts=stereo[b];"
                        f"[v][b]amix=inputs=2:duration=longest,volume=2,atrim=0:{total_s:.3f}[o]")
                sh([FF, "-y", "-hide_banner", "-loglevel", "error", "-i", str(vw), "-i", str(bed),
                    "-filter_complex", filt, "-map", "[o]", "-ar", str(MIX_SR), "-c:a", "pcm_f32le", str(pre)])
            else:
                sh([FF, "-y", "-hide_banner", "-loglevel", "error", "-i", str(vw), "-af",
                    f"{voice_chain},aresample={MIX_SR},pan=stereo|c0=c0|c1=c0", "-ar", str(MIX_SR),
                    "-c:a", "pcm_f32le", str(pre)])
            last = None
            break
        except RuntimeError as e:
            last = e
    if last:
        raise last
    lufs = loudness(pre)
    gain = max(-20.0, min(20.0, TARGET_LUFS - lufs)) if lufs is not None else 0.0
    for lim in ("alimiter=limit=0.84:attack=3:release=60:level=false", "alimiter=limit=0.84"):
        try:
            sh([FF, "-y", "-hide_banner", "-loglevel", "error", "-i", str(pre), "-af", f"volume={gain:.2f}dB,{lim}",
                "-ar", str(MIX_SR), "-c:a", "pcm_s16le", str(final)])
            break
        except RuntimeError:
            if lim == "alimiter=limit=0.84":
                raise
    pre.unlink(missing_ok=True)


# ---------------------------------------------------------------- main
def main(jd):
    global JD, OUT, WORK, FF
    import numpy as np
    JD = Path(jd)
    OUT, WORK = JD / "out", JD / "work"
    OUT.mkdir(exist_ok=True)
    WORK.mkdir(exist_ok=True)
    HF_DIR.mkdir(parents=True, exist_ok=True)
    TORCH_DIR.mkdir(parents=True, exist_ok=True)
    FF = ffmpeg()
    job = json.loads((JD / "job.json").read_text(encoding="utf-8"))
    n_takes = max(1, min(5, int(job.get("takes") or 3)))
    cfgs = [float(x) for x in (job.get("cfgs") or [2.0, 2.6])]
    segs = []
    for s in job["segments"]:
        s = dict(s, start=float(s["start"]), end=float(s["end"]))
        if (s.get("text") or "").strip() and s["end"] > s["start"]:
            segs.append(s)
    segs.sort(key=lambda s: s["start"])
    # what the voice reads (point 4): ș/ț, the pronunciation dictionary, names as a Romanian reads
    # them; "text" stays the real line (subtitles, Whisper's check, the translator)
    lexicon = clean_lexicon(job.get("lexicon"), job["lang"])
    for s in segs:
        s["say"] = voice_text(s["text"], job["lang"], lexicon)
        # what the judge expects to hear: the respellings (names...) but not the aids ("fiiica" is "fiica")
        s["judge"] = voice_text(s["text"], job["lang"], [(a, b) for a, b in lexicon if not lexicon_aid(a, b)])
    respelled = sum(1 for s in segs if s["say"] != " ".join(s["text"].split()))
    orig = WORK / "orig.wav"
    state_f = WORK / "stare.json"
    only = {str(x) for x in job.get("only") or []}
    state = None
    if only and state_f.exists():
        # second run: only these lines again (new, shorter text); the rest and the analysis are kept
        state = json.loads(state_f.read_text(encoding="utf-8"))
        if [str(s.get("id")) for s in segs] != state["ids"]:
            state = None
    if state is None:
        only = set()
        to_wav(JD / job["audio"], orig, MIX_SR, 2)
    total = len(load(orig, MIX_SR)) / MIX_SR
    prog(2)

    py = setup()
    scorer = None
    try:
        scorer = Scorer(py, job["lang"])          # its models load while the voices are separated
    except Exception as e:  # noqa: BLE001
        say(f"   verificarile nu pornesc ({str(e)[:150]}); aleg doar dupa Whisper")
    prog(8)
    if state:
        an, spk, regrouped = state["an"], state["spk"], state["regrouped"]
        refs = {k: (Path(v[0]), v[1], v[2]) for k, v in state["refs"].items()}
        own = {int(i): (Path(v[0]), v[1]) for i, v in state["own"].items()}
        ro_refs = {k: (Path(v[0]), v[1], v[2]) for k, v in state.get("ro_refs", {}).items()}
        nr_refs = {k: (Path(v[0]), v[1], v[2]) for k, v in state.get("nr_refs", {}).items()}
        cent = {k: np.asarray(v, dtype="float32") for k, v in state["cent"].items()}
        keys = sorted(set(spk), key=spk.index)
        say(f"   refac {len(only)} replici cu textul nou (restul raman cum erau)")
    else:
        an = analyze(py, orig, segs)
        prog(15)
        spk, regrouped = speakers(segs, an["embs"], job)
        keys = sorted(set(spk), key=spk.index)
        if regrouped:
            say(f"   VoiceStudio a gasit un singur vorbitor; dupa voce sunt {len(keys)}")
        refs, own, cent = references(an["vocals"], segs, spk, an["embs"])
        if not refs:
            raise RuntimeError("nu am gasit replici destul de lungi pentru mostra vocii")
        ro_refs, nr_refs = {}, {}
        say("   mostre: " + ", ".join(f"{k} {refs[k][2]:.0f}s" for k in keys if k in refs)
            + f"; {len(own)} replici au si mostra lor (emotia replicii)")
        if respelled:
            say(f"   {respelled} replici citite de voce cu scrierea pentru pronuntie (subtitrarile raman corecte)")
    fallback = next(iter(refs))
    ckeys = [k for k in keys if k in cent]
    if scorer:
        cf = WORK / "centroizi.npy"
        np.save(cf, np.asarray([cent[k] for k in ckeys] or [np.zeros(192)], dtype="float32"))
        scorer.centroids(cf)
        if scorer.has:
            on = [n for n, ok in (("vocea (ECAPA)", scorer.has.get("ecapa")), ("naturalete (UTMOS)", scorer.has.get("utmos")),
                                  ("pronuntie (model romanesc)", scorer.has.get("ctc")),
                                  ("accent (ce limba se aude)", scorer.has.get("lid"))) if ok]
            say("   verific fiecare varianta cu Whisper" + (", " + ", ".join(on) if on else ""))
        else:
            say("   verificarile suplimentare nu au pornit" + (f" ({scorer.notes[-1][:120]})" if scorer.notes else "")
                + "; aleg doar dupa Whisper")
            scorer.close()
            scorer = None
    prog(20)

    room = rooms(segs, total)
    # point 1: a native Romanian voice says the line, Seed-VC gives it the speaker's timbre
    vc, native, vc_target, base_sim, native_info = None, {}, {}, {}, {}
    if job.get("native_voice") and job["lang"] == "ro":
        try:
            vc, native, vc_target, base_sim, native_info = start_native(job, scorer, load(an["vocals"]), segs, spk,
                                                                        keys, refs, ckeys)
        except Exception as e:  # noqa: BLE001 - the native voice is optional: the clip goes on without it
            say(f"   vocea nativa nu porneste ({str(e)[:150]}); clipul merge fara ea")
    if state:              # the first run's measure of the native voice, when it is not measured again now
        for k, v in (state.get("base_sim") or {}).items():
            if base_sim.get(k) is None and v is not None:
                base_sim[k] = v
    lines = Lines(job, segs, spk, room, scorer, ckeys, vc=vc, vc_target=vc_target, base_sim=base_sim)
    todo = [k for k, s in enumerate(segs) if not state or str(s.get("id")) in only]
    if state:                                     # the lines not redone keep their chosen take
        for k, t in enumerate(state["chosen"]):
            if k not in todo and t:
                lines.takes[k] = [dict(t, kept=True)]
    n1 = min(2, n_takes)
    accent_free = job.get("mostra_romana", True) or bool(native)
    n2 = max(0, n_takes - 1) if accent_free else max(0, n_takes - n1)

    def nat_ok(key):
        return key in native and vc is not None and vc.ok

    def free_sample(k, key, own=True):
        """The accent-free sample line k is made from: the native one ("nr"), unless the line borrows
        another speaker's voice or its takes from the native voice keep failing the timbre check;
        then the speaker's own Romanian one ("ro"); None when there is neither."""
        if own and key in nr_refs:
            tried = [t for t in lines.takes[k] if t["kind"] in ("nat", "nr")]
            if len(tried) < 2 or any(lines.timbre_ok(k, t) for t in tried):
                return nr_refs[key][:2], "nr"
        if key in ro_refs:
            return ro_refs[key][:2], "ro"
        return None

    t0 = time.time()
    try:
        # pass 1: the clip's own voice (its clearest lines; and the line's own clip, for its emotion);
        # with the native voice, its second take is the native voice converted to the speaker
        if not state:
            say(f"   {len(segs)} replici; pasul 1: pana la {n1} variante din vocea originala"
                + (" si din vocea nativa" if native and n1 > 1 else "") + "...")
        for n, k in enumerate(todo):
            key = spk[k] if spk[k] in refs else fallback
            plan = [(refs[key][:2], "mostra", cfgs[0])]
            if n1 > 1:
                if nat_ok(spk[k]):
                    plan.append((native[spk[k]], "nat", cfgs[0]))
                else:
                    plan.append((own[k], "replica", cfgs[0]) if k in own
                                else (refs[key][:2], "mostra", cfgs[1 % len(cfgs)]))
            lines.run(k, plan)
            prog(20 + (35 if n2 else 70) * (n + 1) / max(1, len(todo)))
            if n == 4:
                left = (time.time() - t0) / (n + 1) * (len(todo) - n - 1) * (1.8 if n2 else 1.0)
                say(f"   {n + 1}/{len(todo)} replici facute (mai dureaza ~{max(1, round(left / 60))} min)")
        # pass 2: the same voice already speaking Romanian (much less accent), for the lines not perfect yet
        if n2:
            if job.get("mostra_romana", True) and (not ro_refs or not state):
                ro_refs = romanian_refs(lines, keys)
            if native and (not nr_refs or not state):
                # the native voice's best converted lines: a sample that speaks Romanian like a
                # Romanian, with the speaker's timbre (cloned from in pass 2, before the "ro" one)
                nr_refs = romanian_refs(lines, keys, kinds=("nat",), tag="nr")
            redo = [k for k in todo if not lines.done(k)]
            if not state and nr_refs:
                rest = len(todo) - len(redo)
                say("   mostra nativa (vocea nativa cu timbrul personajului): "
                    + ", ".join(f"{k} {v[2]:.0f}s" for k, v in nr_refs.items())
                    + (" + mostra romaneasca: " + ", ".join(f"{k} {v[2]:.0f}s" for k, v in ro_refs.items()
                                                            if k not in nr_refs)
                       if any(k not in nr_refs for k in ro_refs) else "")
                    + f"; pasul 2: refac {len(redo)} replici cu ea"
                    + (f" (celelalte {rest} au iesit deja foarte bine)" if rest else ""))
            elif not state:
                if ro_refs:
                    rest = len(todo) - len(redo)
                    say("   mostra romaneasca: " + ", ".join(f"{k} {v[2]:.0f}s" for k, v in ro_refs.items())
                        + f"; pasul 2: refac {len(redo)} replici cu ea"
                        + (f" (celelalte {rest} au iesit deja foarte bine)" if rest else ""))
                elif job.get("mostra_romana", True):
                    say(f"   prea putine replici curate pentru mostra romaneasca; pasul 2: {len(redo)} replici "
                        "mai primesc variante din vocea originala")
            for n, k in enumerate(redo):
                key = spk[k] if spk[k] in refs else fallback

                def step(i, k=k, key=key):          # the sample chosen when the take is made
                    got = free_sample(k, key)
                    return ((got[0], got[1], cfgs[i % len(cfgs)]) if got
                            else (refs[key][:2], "mostra", cfgs[(i + 1) % len(cfgs)]))
                lines.run(k, [lambda i=i, step=step: step(i) for i in range(n2)])
                prog(55 + 35 * (n + 1) / max(1, len(redo)))
            # the lines already good from the original voice get one take from the accent-free sample
            # too: the original voice of a foreign speaker carries its accent, which the judges hear
            # only partly; that take wins when it is as good
            for k in todo:
                key = spk[k] if spk[k] in refs else fallback
                for i in range(2):          # up to 2 tries until one from the sample is good enough
                    if any(t["kind"] in ACCENT_FREE and lines.good_enough(k, t) for t in lines.takes[k]):
                        break
                    got = free_sample(k, key)
                    if not got:
                        break
                    lines.gen(k, got[0], got[1], cfgs[i % len(cfgs)], 60 + len(lines.takes[k]) + i)
        # pass 3: lines that still skip words, are heard as something else or sound foreign get
        # other ways, until they come out whole: more takes from the accent-free sample, then the
        # line made sentence by sentence. What is still wrong goes back to the translator (simpler words).
        bad = [k for k in todo if lines.needs_fix(k)]

        def only_bad_words(k):
            """Says the text, understood, sounds Romanian: only a word pronounced wrong."""
            b = lines.best(k)
            words = len(norm(segs[k]["text"]).split())
            return bool(b and not pron_ok(b) and not b.get("miss") and not b.get("leak")
                        and not (b.get("wcer") is not None and b["wcer"] > (0.30 if words <= 2 else 0.50))
                        and not (b.get("lid") is not None and b["lid"] < 0.6 and words >= 3))
        # skipped words first, then lines not understood / sounding foreign, a badly said word last
        bad.sort(key=lambda k: (0 if (lines.best(k) or {}).get("miss") else 2 if only_bad_words(k) else 1, k))
        if bad:
            budget = max(12, int(0.6 * len(todo)))          # extra generations for the whole clip
            say(f"   pasul 3: {len(bad)} replici sar cuvinte sau nu se inteleg bine; le refac altfel...")
            fixed = 0
            spoken = {}
            for i, s in enumerate(segs):
                spoken[spk[i]] = spoken.get(spk[i], 0.0) + s["end"] - s["start"]
            main = max((x for x in spoken if x in refs), key=lambda x: spoken[x], default=fallback)
            for n, k in enumerate(bad):
                key = spk[k] if spk[k] in refs else fallback
                b = lines.best(k) or {}
                own_voice = True
                if key != main and ((b.get("lid") is not None and b["lid"] < 0.5)
                                    or (b.get("wcer") is not None and b["wcer"] > 0.5)):
                    key = main      # its own sample gives another language / nonsense: the main voice's
                    own_voice = False      # (its "ro" / clip sample: a native one is judged on its own speaker)
                words_only = only_bad_words(k)
                short = len(norm(segs[k]["text"]).split()) <= 2
                steps = [(None, None, cfgs[i % len(cfgs)], False) for i in range(2 if words_only else 4 if short else 2)]
                if len(sentences(segs[k]["text"])) >= 2 and not words_only:
                    steps += [(None, None, cfgs[0], True), (None, None, cfgs[1 % len(cfgs)], True)]
                if lines.strict and not pron_ok(b) and nat_ok(spk[k]):
                    # a word pronounced wrong: first the native voice itself says the line (converted)
                    steps.insert(0, (native[spk[k]], "nat", cfgs[0], False))
                for i, (r, kd, cfg, split) in enumerate(steps):
                    if budget <= 0 or not lines.needs_fix(k):
                        break
                    if r is None:                   # the sample chosen when the take is made
                        r, kd = free_sample(k, key, own_voice) or (refs[key][:2], "mostra")
                    before = lines.n_gen
                    lines.gen(k, r, kd, cfg, 100 + len(lines.takes[k]) + i, split=split)
                    budget -= max(1, lines.n_gen - before)
                if not lines.needs_fix(k):
                    fixed += 1
                prog(90 + 7 * (n + 1) / len(bad))
            left = len(bad) - fixed
            say(f"   pasul 3: {fixed} reparate" + (f", {left} tot nu ies bine" if left else "")
                + ("" if budget > 0 else " (limita de variante in plus pe clip atinsa)"))
        # a line left with no usable take (its takes from the native voice did not sound like the
        # speaker, and its own failed to generate): one more from the speaker's Romanian sample or voice
        for k in todo:
            key = spk[k] if spk[k] in refs else fallback
            tries = ([(ro_refs[key][:2], "ro")] if key in ro_refs else []) + [(refs[key][:2], "mostra")] * 2
            for i, (r, kd) in enumerate(tries):
                if lines.best(k) is not None:
                    break
                lines.gen(k, r, kd, cfgs[i % len(cfgs)], 200 + len(lines.takes[k]) + i)
    finally:
        if scorer:
            scorer.close()
        if vc:
            vc.close()

    chosen = []
    for k in range(len(segs)):
        b = lines.best(k)
        if b is None and state and k not in todo and state["chosen"][k]:
            b = state["chosen"][k]                # a line not redone keeps what it had
        if b is None:
            raise RuntimeError(f"replica {k + 1}: VoiceStudio nu a generat nimic")
        chosen.append(b)
    state_f.write_text(json.dumps({
        "ids": [str(s.get("id")) for s in segs], "an": an, "spk": spk, "regrouped": regrouped,
        "refs": {k: [str(v[0]), v[1], v[2]] for k, v in refs.items()},
        "own": {str(i): [str(v[0]), v[1]] for i, v in own.items()},
        "ro_refs": {k: [str(v[0]), v[1], v[2]] for k, v in ro_refs.items()},
        "nr_refs": {k: [str(v[0]), v[1], v[2]] for k, v in nr_refs.items()},
        "base_sim": base_sim,
        "cent": {k: [float(x) for x in v] for k, v in cent.items()},
        "chosen": chosen}, ensure_ascii=False), encoding="utf-8")

    # lines evened out (no compressor), brightness matched to the original voice of each speaker
    vocals = load(an["vocals"])
    ref_hf = {}
    for key in keys:
        parts = [vocals[int(s["start"] * SR):int(s["end"] * SR)] for k, s in enumerate(segs) if spk[k] == key]
        parts = [p for p in parts if len(p) > SR // 2]
        if parts:
            ref_hf[key] = hf_db(np.concatenate(parts)[: SR * 60])
    clips = [load(t["path"]) for t in chosen]
    lvl = {}
    for k, a in enumerate(clips):
        lvl.setdefault(spk[k], []).append(active_rms(a))
    target_lvl = {key: median(v) for key, v in lvl.items()}
    treble = {}
    for key in keys:
        hs = [hf_db(a) for k, a in enumerate(clips) if spk[k] == key]
        h, r = median(hs), ref_hf.get(key)
        treble[key] = max(0.0, min(4.0, h - r - 2.0)) if h is not None and r is not None else 0.0

    # timeline: never slower; faster (tone kept) only as much as needed
    voice = np.zeros(int((total + 30) * SR), dtype="float32")
    placed, info, prev_end, speedups = [], [], 0.0, []
    for k, (s, best) in enumerate(zip(segs, chosen)):
        a = clips[k]
        g = target_lvl[spk[k]] / active_rms(a)
        a = a * max(0.5, min(2.0, g))
        st = s["start"]
        length = len(a) / SR
        if prev_end > st and prev_end - st <= PUSH_MAX:
            # wait for the line before to finish, unless that squeezes this one past 1.5x
            if length / max(0.3, room[k] - (prev_end - st)) <= max(1.5, length / room[k]):
                st = prev_end
        avail = max(0.3, room[k] - (st - s["start"]))
        f = min(MAX_UP, length / avail) if length > avail * 1.02 else 1.0
        if f > 1.0:
            speedups.append(f)
        a = line_fx(a, f, treble[spk[k]])
        i = int(st * SR)
        voice[i:i + len(a)] += a[: max(0, len(voice) - i)]
        en = st + len(a) / SR
        prev_end = max(prev_end, en)
        placed.append({"id": s.get("id"), "start": round(st, 3), "end": round(en, 3), "text": s["text"]})
        nat = best["len"] if best.get("split") else (lines.natural.get((k, best["kind"])) or best["len"])
        info.append({"id": s.get("id"), "speaker": spk[k], "text": s["text"], "kind": best["kind"],
                     "cfg": best.get("cfg"), "takes": len(lines.takes[k]), "speed": round(f, 3),
                     "natural": round(nat, 3), "slot": round(s["end"] - s["start"], 3), "room": round(room[k], 3),
                     "over": round(max(0.0, len(a) / SR - avail), 3),
                     "wcer": best.get("wcer"), "ctc": best.get("ctc"), "utmos": best.get("utmos"),
                     "sim": best.get("sim"), "heard": best.get("heard"), "ctc_text": best.get("ctc_text"),
                     "lid": best.get("lid"), "miss": best.get("miss") or [], "split": best.get("split"),
                     "leak": best.get("leak") or [],
                     # what the voice read, and how well each sound came out (sound by sound)
                     "say": s["say"] if s["say"] != s["text"] else None,
                     "pron": best.get("pron"), "bad": best.get("bad") or [], "weak": best.get("weak") or [],
                     "pw": best.get("pw"),
                     "all": [{kk: t.get(kk) for kk in ("kind", "cfg", "len", "wcer", "ctc", "utmos", "sim", "lid",
                                                       "miss", "split", "pron", "bad")}
                             for t in lines.takes[k]]})
    end = max(int(total * SR), max((int(p["end"] * SR) for p in placed), default=0))
    voice = voice[:end]

    # level next to the music: as loud as the original voices were
    if job.get("bg", True) and an.get("bed") and Path(an["bed"]).exists():
        mo = np.zeros(len(vocals), dtype=bool)
        mn = np.zeros(len(voice), dtype=bool)
        for s, p in zip(segs, placed):
            mo[int(s["start"] * SR):int(s["end"] * SR)] = True
            mn[int(p["start"] * SR):int(p["end"] * SR)] = True
        ro_ = float(np.sqrt(np.mean(vocals[mo[:len(vocals)]] ** 2))) if mo.any() else 0.1
        rn = float(np.sqrt(np.mean(voice[mn] ** 2))) if mn.any() else 0.1
        voice = voice * min(4.0, max(0.25, ro_ / max(rn, 1e-6)))
        bed = an["bed"]
    else:
        bed = None
    peak = float(np.max(np.abs(voice))) if len(voice) else 1.0
    vw = WORK / "voce_linii.wav"
    import soundfile as sf
    sf.write(str(vw), voice / max(1.0, peak), SR, subtype="FLOAT")
    master(vw, bed, OUT / "voce.wav", max(total, end / SR))

    def avg(key):
        v = [x[key] for x in info if x.get(key) is not None]
        return round(sum(v) / len(v), 4) if v else None
    from collections import Counter
    unclear = sum(1 for x in info if (x.get("wcer") or 0) > 0.25 or (x.get("ctc") or 0) > 0.30)
    missing = sum(1 for x in info if x.get("miss"))
    bad_words = Counter(w for x in info for w in x.get("bad") or [])
    stats = {"lines": len(segs), "speakers": len(keys), "regrouped": regrouped,
             "cer": avg("wcer"), "ctc": avg("ctc"), "utmos": avg("utmos"), "sim": avg("sim"), "lid": avg("lid"),
             "pron": avg("pron"), "bad_lines": sum(1 for x in info if x.get("bad")),
             "bad_words": dict(bad_words.most_common(40)), "respelled": respelled,
             "unclear": unclear, "missing": missing, "takes": lines.n_gen, "sped_up": len(speedups),
             "rubberband": has_rubberband(),
             "max_speed": round(max(speedups), 2) if speedups else 1.0,
             "ro_ref": sorted(ro_refs), "from_ro": sum(1 for x in info if x["kind"] == "ro"),
             "nr_ref": sorted(nr_refs), "from_nr": sum(1 for x in info if x["kind"] == "nr"),
             "from_nat": sum(1 for x in info if x["kind"] == "nat"), "native": native_info,
             "own_refs": sum(1 for x in info if x["kind"] == "replica"),
             "treble_cut": {k: round(v, 1) for k, v in treble.items()},
             "minutes": round((time.time() - t0) / 60, 1)}
    (OUT / "info.json").write_text(json.dumps({"segments": placed, "lines": info, "stats": stats},
                                              ensure_ascii=False), encoding="utf-8")
    parts = [f"Whisper aude corect {100 - (stats['cer'] or 0) * 100:.0f}% din text"]
    if stats["pron"] is not None and lines.strict:
        # sound by sound: the main measure of the accent now (the language guess was too lenient)
        parts.append(f"pronuntie pe sunete {stats['pron'] * 100:.0f}%")
    else:
        if stats["ctc"] is not None:
            parts.append(f"pronuntie {100 - stats['ctc'] * 100:.0f}%")
        if stats["lid"] is not None:
            parts.append(f"suna romaneste {stats['lid'] * 100:.0f}%")
    if stats["utmos"] is not None:
        parts.append(f"naturalete {stats['utmos']:.1f}/5")
    if stats["from_nr"] or stats["from_nat"]:
        parts.append(f"{stats['from_nr'] + stats['from_nat']} replici din vocea nativa")
    if stats["from_ro"]:
        parts.append(f"{stats['from_ro']} replici din mostra romaneasca")
    still = [w for w, _ in bad_words.most_common(8)]
    say("   gata: " + ", ".join(parts) + (f"; {unclear} replici tot neclare" if unclear else "")
        + (f"; {missing} replici tot sar cuvinte" if missing else "")
        + (f"; cuvinte tot pronuntate gresit: {', '.join(still)}" if still and lines.strict else "")
        + f"; {len(speedups)} replici accelerate (max {stats['max_speed']}x), niciuna incetinita")
    prog(100)
    print("REZULTAT " + json.dumps({"ok": True, "stats": stats}), flush=True)


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--setup":
            FF = ffmpeg()
            setup()
            setup_vc()                       # the native voice's converter (optional: never fails the setup)
        else:
            main(sys.argv[1])
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        print("EROARE " + (str(e) or e.__class__.__name__)[:600], flush=True)
        sys.exit(1)
