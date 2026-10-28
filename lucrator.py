"""
lucrator.py - ruleaza pe GPU-ul inchiriat (RunPod sau Vast.ai), langa VoiceStudio, pe portul 3901.
Face video-ul final al unui clip dublat (stergere subtitrari AI, Real-ESRGAN, 9:16,
subtitrari, watermark, export) cu acelasi cod ca pe PC (randare.py + sterge_ai.py).

Pornit de mica bucla din LUCRATOR_BOOT (vezi dubleaza.py). PC-ul trimite codul si
fisierele, porneste lucrul, urmareste progresul si descarca rezultatul.
Toate cererile cer antetul X-Key (aceeasi cheie ca VoiceStudio pe pod-ul asta).
"""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

KEY = os.environ.get("LUCRATOR_KEY", "")
BASE = Path(os.environ.get("LUCRATOR_DIR", "/workspace/lucrator"))
APP = BASE / "app"
JOBS = BASE / "jobs"
PORT = int(os.environ.get("LUCRATOR_PORT", "3901"))
SELF = Path(__file__).resolve()
for d in (APP, JOBS):
    d.mkdir(parents=True, exist_ok=True)

RENDER_JOB = r'''
import json, shutil, sys, traceback
from pathlib import Path
jd = Path(sys.argv[1])
sys.path.insert(0, str(Path(__file__).resolve().parent))
import randare
spec = json.loads((jd / "job.json").read_text(encoding="utf-8"))
c = spec["c"]
video, audio, segs = jd / spec["video"], jd / spec["audio"], spec["segments"]
work = jd / "work"
work.mkdir(exist_ok=True)
out = jd / "out.mp4"
logo = jd / "watermark.png"
randare.LOGO_FILE = logo
def prog(p):
    print(f"PROGRES {p:.1f}", flush=True)
try:
    import subprocess as _sp
    _ff = randare.find_ffmpeg()[0]
    _v = _sp.run([_ff, "-hide_banner", "-version"], capture_output=True).stdout.decode(errors="replace").split("\n")[0]
    print(f"ffmpeg folosit: {_ff} | {_v[:80]}", flush=True)
except Exception as _e:
    print(f"ffmpeg: {_e}", flush=True)
try:
    extra = {}
    if spec.get("fit"):
        target = randare.probe(video)["dur"]
        f = randare.fit_voice(audio, target, c, work / "voce_potrivita.wav")
        audio = work / "voce_potrivita.wav"
        segs = randare.fit_segments(segs, f["parts"], f["factor"])
        c["taie_pauze"] = False
        extra["voce"] = {k: f.get(k) for k in ("before", "after", "factor", "voice_len")}
        if c.get("salveaza_voce"):
            randare.encode_audio(audio, jd / "voce.mp3", c.get("bitrate", "256"))
    info = randare.render(video, audio, segs, out, c, progress=prog, work_dir=work)
    info.update(extra)
    shutil.rmtree(work, ignore_errors=True)
    print("REZULTAT " + json.dumps(info), flush=True)
except Exception as e:
    print("EROARE " + (str(e) or e.__class__.__name__), flush=True)
    traceback.print_exc()
    sys.exit(1)
'''
(APP / "render_job.py").write_text(RENDER_JOB, encoding="utf-8")

FF_BASE = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
# newest first; release branches are built against older NVENC headers, so they still encode on the
# GPU when the host's NVIDIA driver is too old for master's NVENC API
FF_BUILDS = ["ffmpeg-master-latest-linux64-gpl.tar.xz",
             "ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz"]
FF_READY = threading.Event()
FF_NOTE = {"msg": ""}


FF_DIAG = {}      # why NVENC does / does not work here (shown in /ping as "ffmpeg_diag")


def _nvenc_test(ff):
    try:
        r = subprocess.run([str(ff), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
                           capture_output=True, timeout=60)
        lines = [ln.strip() for ln in r.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
        # the first lines say why (driver / API version / missing library), the last only "could not open"
        why = " / ".join(lines[:3] + ([lines[-1]] if len(lines) > 3 else []))
        return r.returncode == 0, why[:400]
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def _sh(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
        return r.stdout.decode("utf-8", "replace").strip()
    except Exception as e:  # noqa: BLE001
        return f"? ({e})"


def _gpu_diag():
    """What the container gives ffmpeg for NVENC: driver, libnvidia-encode, capabilities."""
    d = {}
    d["driver"] = (_sh("nvidia-smi --query-gpu=driver_version --format=csv,noheader").splitlines() or [""])[0]
    d["cuda_driver"] = next((ln.split("CUDA Version:")[1].split("|")[0].strip()
                             for ln in _sh("nvidia-smi").splitlines() if "CUDA Version:" in ln), "")
    libs = [ln.split("=>")[-1].strip() for ln in _sh("ldconfig -p").splitlines() if "libnvidia-encode" in ln]
    if not libs:
        libs = [str(p) for d_ in ("/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/local/nvidia/lib64")
                for p in Path(d_).glob("libnvidia-encode.so*")] if sys.platform.startswith("linux") else []
    d["libnvidia_encode"] = libs[:3]
    d["capabilities"] = os.environ.get("NVIDIA_DRIVER_CAPABILITIES", "")
    return d


def _diag_text():
    d = FF_DIAG
    lib = d.get("libnvidia_encode")
    return (f"driver {d.get('driver') or '?'} (CUDA {d.get('cuda_driver') or '?'}), "
            f"libnvidia-encode {'da' if lib else 'LIPSESTE'}, "
            f"NVIDIA_DRIVER_CAPABILITIES={d.get('capabilities') or '-'}")


def _fetch_ff(name, dest):
    import tarfile
    import urllib.request
    tmp = BASE / "ffmpeg.tar.xz"
    urllib.request.urlretrieve(FF_BASE + name, tmp)
    unpack = BASE / "ff_unpack"
    shutil.rmtree(unpack, ignore_errors=True)
    with tarfile.open(tmp) as t:
        t.extractall(unpack)
    top = next(p for p in unpack.iterdir() if p.is_dir())
    shutil.rmtree(dest, ignore_errors=True)
    shutil.move(str(top), str(dest))
    shutil.rmtree(unpack, ignore_errors=True)
    tmp.unlink(missing_ok=True)
    for b in ("ffmpeg", "ffprobe"):
        os.chmod(dest / "bin" / b, 0o755)


def ensure_ffmpeg():
    """A recent static ffmpeg in app/ffmpeg (the image has ffmpeg 4.x: no -fps_mode, old filters),
    preferably one whose NVENC works with this host's driver (video encoded on the GPU, not the CPU)."""
    dest = APP / "ffmpeg"
    why = ""
    try:
        try:
            FF_DIAG.update(_gpu_diag())
        except Exception as e:  # noqa: BLE001
            FF_DIAG["err"] = str(e)[:200]
        if (dest / "bin" / "ffmpeg").is_file():
            ok, why = _nvenc_test(dest / "bin" / "ffmpeg")
            FF_DIAG["app_ffmpeg"] = "NVENC ok" if ok else why
            if ok:
                (APP / "ffmpeg_nvenc.txt").unlink(missing_ok=True)
                FF_NOTE["msg"] = "ffmpeg ok (NVENC)"
                return
        first = None
        for name in FF_BUILDS:
            cand = BASE / ("ff_" + name.split("-")[1])
            FF_NOTE["msg"] = f"descarc {name}"
            try:
                if not (cand / "bin" / "ffmpeg").is_file():
                    _fetch_ff(name, cand)
            except Exception as e:  # noqa: BLE001 - that branch may be gone: next one
                why = f"{name}: {e}"
                continue
            first = first or cand
            ok, why = _nvenc_test(cand / "bin" / "ffmpeg")
            FF_DIAG[name.split("-")[1]] = "NVENC ok" if ok else why
            if ok:
                shutil.rmtree(dest, ignore_errors=True)
                shutil.copytree(cand, dest)
                (APP / "ffmpeg_nvenc.txt").unlink(missing_ok=True)
                FF_NOTE["msg"] = f"ffmpeg ok (NVENC, {name.split('-')[1]})"
                return
        if first and not (dest / "bin" / "ffmpeg").is_file():
            shutil.copytree(first, dest)
        # the image's own (old) ffmpeg talks to older drivers: Real-ESRGAN's output can still be
        # encoded on the GPU with it (sterge_ai.py reads this file)
        sysff = shutil.which("ffmpeg")
        if sysff:
            ok, why2 = _nvenc_test(sysff)
            FF_DIAG["system_ffmpeg"] = sysff + ": " + ("NVENC ok" if ok else why2)
            if ok:
                (APP / "ffmpeg_nvenc.txt").write_text(sysff, encoding="utf-8")
                FF_NOTE["msg"] = (f"ffmpeg ok; NVENC doar cu ffmpeg-ul vechi din imagine (marire + export final): "
                                  f"{why} | {_diag_text()}")
                return
        (APP / "ffmpeg_nvenc.txt").unlink(missing_ok=True)
        FF_NOTE["msg"] = f"ffmpeg ok, dar fara NVENC (codare pe procesor): {why} | {_diag_text()}"
    except Exception as e:  # noqa: BLE001 - the image's own ffmpeg is still there
        FF_NOTE["msg"] = f"ffmpeg nou nu s-a putut descarca ({e}); folosesc cel din imagine"
    finally:
        FF_READY.set()


threading.Thread(target=ensure_ffmpeg, daemon=True).start()


def prepare_voice_tools():
    """The new dub voice (voce_noua.py) needs its own tools (voice separation + voice measuring):
    installed in the background as soon as the PC has sent the script, while the first clip is
    still being transcribed and translated."""
    for _ in range(360):
        if (APP / "voce_noua.py").exists() and (APP / "utmos22.py").exists():
            time.sleep(5)                       # the PC may still be sending the other files
            break
        time.sleep(10)
    else:
        return
    FF_READY.wait(900)
    try:
        log = open(BASE / "voce_unelte.log", "ab")
        subprocess.run([sys.executable, str(APP / "voce_noua.py"), "--setup"], stdout=log, stderr=log,
                       cwd=str(APP), timeout=3600, env=dict(os.environ, PYTHONUNBUFFERED="1"))
    except Exception:  # noqa: BLE001
        pass


threading.Thread(target=prepare_voice_tools, daemon=True).start()

JOB = {}          # job id -> {"state", "percent", "lines", "result", "error", "proc"}
LOCK = threading.Lock()
GPU_SLOT = threading.Semaphore(1)   # one video job at a time: two Real-ESRGAN runs next to
                                    # VoiceStudio do not fit in the card's memory
POSIX = os.name == "posix"


def _kill_tree(proc):
    """The render job and everything it started (sterge_ai.py, ffmpeg): nothing keeps the GPU busy."""
    if proc is None:
        return
    try:
        if POSIX:
            os.killpg(proc.pid, signal.SIGKILL)     # started with start_new_session: its own group
        else:
            proc.kill()
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


# ---- watchdog: a pod nobody talks to any more removes itself (no forgotten GPU billed for hours)
HB_IDLE_MIN = float(os.environ.get("LUCRATOR_IDLE_MIN", "25") or 25)
HB = {"last": time.time(), "seen": False}     # last authenticated request; seen = a /hb ever came
WATCH = {"state": "pornit", "log": []}


def _touch(hb=False):
    HB["last"] = time.time()
    if hb:
        HB["seen"] = True


def _wlog(line):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + line
    WATCH["log"] = (WATCH["log"] + [line])[-10:]
    try:
        with open(BASE / "watchdog.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _jobs_active():
    with LOCK:
        return any(j.get("state") == "running" for j in JOB.values())


VAST_API = "https://console.vast.ai/api/v0"


def _vast_destroy(iid, key):
    """Vast.ai: the instance destroys itself with its own per-instance key (Vast injects
    CONTAINER_ID + CONTAINER_API_KEY; docs: `vastai destroy instance $CONTAINER_ID --api-key
    $CONTAINER_API_KEY` = DELETE /api/v0/instances/<id>/). -> (ok, text)"""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"{VAST_API}/instances/{iid}/", data=b"{}", method="DELETE",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", "replace")[:300]
            try:
                ok = json.loads(body or "{}").get("success", True) is not False
            except ValueError:
                ok = True
            return ok, f"HTTP {r.status}: {body}"
    except urllib.error.HTTPError as e:
        return e.code == 404, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:300]


def _remove_self():
    """Delete this rented machine (RunPod pod or Vast.ai instance). True when the request went out."""
    pod = os.environ.get("RUNPOD_POD_ID", "")
    if pod:
        for cmd in (["runpodctl", "remove", "pod", pod], ["runpodctl", "stop", "pod", pod]):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=120)
                out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()[-300:]
                _wlog(f"{' '.join(cmd)} -> cod {r.returncode}: {out}")
                if r.returncode == 0:
                    WATCH["state"] = f"{cmd[1]} pod trimis"
                    return True
            except Exception as e:  # noqa: BLE001
                _wlog(f"{' '.join(cmd)} -> {e}")
        return False
    iid, key = os.environ.get("CONTAINER_ID", ""), os.environ.get("CONTAINER_API_KEY", "")
    if iid and key:
        ok, text = _vast_destroy(iid, key)
        _wlog(f"Vast.ai destroy instance {iid} -> {text}")
        if ok:
            WATCH["state"] = "stergere instanta Vast.ai trimisa"
        return ok
    return False


def _where():
    if os.environ.get("RUNPOD_POD_ID"):
        return "pod-ul " + os.environ["RUNPOD_POD_ID"]
    if os.environ.get("CONTAINER_ID") and os.environ.get("CONTAINER_API_KEY"):
        return "instanta Vast.ai " + os.environ["CONTAINER_ID"]
    return ""


def watchdog():
    pod = _where()
    if not pod:
        WATCH["state"] = "oprit (fara RUNPOD_POD_ID / CONTAINER_ID+CONTAINER_API_KEY)"
        return
    if HB_IDLE_MIN <= 0:
        WATCH["state"] = "oprit (LUCRATOR_IDLE_MIN=0)"
        return
    while True:
        time.sleep(30)
        idle = time.time() - HB["last"]
        if not HB["seen"]:
            # armed only by the first /hb: a PC whose app does not send heartbeats yet never
            # loses a pod it still uses (for the voice on VoiceStudio, say)
            WATCH["state"] = "asteapta primul /hb"
            continue
        if _jobs_active():
            WATCH["state"] = "lucreaza"
            continue
        if idle < HB_IDLE_MIN * 60:
            WATCH["state"] = f"activ (se inchide dupa {HB_IDLE_MIN:g} min fara contact)"
            continue
        WATCH["state"] = "sterg masina"
        _wlog(f"niciun contact de {idle / 60:.0f} min si nicio lucrare: sterg {pod}")
        if not _remove_self():
            WATCH["state"] = "nu am putut sterge masina (vezi watchdog.log); reincerc"
        # whatever happened: wait a full idle period again before the next try
        HB["last"] = time.time()


threading.Thread(target=watchdog, daemon=True).start()


def version():
    h = hashlib.md5()
    h.update(SELF.read_bytes())
    return h.hexdigest()[:12]


def app_files():
    res = {}
    for p in APP.rglob("*"):
        rel = str(p.relative_to(APP)).replace("\\", "/")
        if (p.is_file() and p.name != "render_job.py" and not rel.startswith(("ffmpeg/", "__pycache__/"))
                and p.stat().st_size < 20 << 20):
            res[str(p.relative_to(APP)).replace("\\", "/")] = hashlib.md5(p.read_bytes()).hexdigest()[:12]
    return res


def run_job(jid):
    with LOCK:
        j = JOB.get(jid)          # this run's own record (a new /run of the same id gets a new one)
        if j is None:
            return
    try:
        _run_job_slot(jid, j)
    except Exception as e:  # noqa: BLE001 - never leave a job "running" forever (the pod would never stop)
        with LOCK:
            if j.get("state") == "running":
                j["state"] = "cancelled" if j.get("cancel") else "error"
                j["error"] = j.get("error") or f"lucratorul: {str(e)[:300] or e.__class__.__name__}"
            j["proc"] = None


def _run_job_slot(jid, j):
    try:
        spec = json.loads((JOBS / jid / "job.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        spec = {}
    if spec.get("no_slot"):
        # the dub voice: VoiceStudio does the GPU work, it must not wait behind a video render
        _run_job(jid, j)
        return
    with LOCK:
        j["lines"] = ["astept randul la placa video (alt clip se randeaza acum)"]
    with GPU_SLOT:
        with LOCK:
            if j.get("cancel") or JOB.get(jid) is not j:
                if j.get("cancel"):
                    j["state"] = "cancelled"
                return
            j["lines"] = []
        _run_job(jid, j)


# A job that stops talking (no line, no progress) or runs absurdly long is killed: a hung ffmpeg / CUDA call
# would otherwise hold the GPU slot - and keep the machine "busy", so billing - forever. (The PC gives up on
# it earlier and sends /cancel; this is for when the PC is gone or can't reach this machine.)
JOB_STALL_S = float(os.environ.get("LUCRATOR_JOB_STALL_S", "2700") or 2700)     # 45 min without any output
JOB_MAX_S = float(os.environ.get("LUCRATOR_JOB_MAX_S", "14400") or 14400)       # 4 h, whatever it says


def _guard_job(j, proc, t0):
    while proc.poll() is None:
        time.sleep(10)
        with LOCK:
            if j.get("cancel") or j.get("proc") is not proc:
                return
            idle = time.time() - float(j.get("moved") or t0)
        why = None
        if idle > JOB_STALL_S:
            why = f"blocat: nicio iesire de {idle / 60:.0f} min, oprit de lucrator"
        elif time.time() - t0 > JOB_MAX_S:
            why = f"oprit de lucrator dupa {JOB_MAX_S / 3600:g} ore"
        if why:
            with LOCK:
                j["killed"] = why
            _wlog(f"o lucrare {why}")
            _kill_tree(proc)
            return


def _run_job(jid, j):
    jd = JOBS / jid
    FF_READY.wait(600)
    try:
        spec = json.loads((jd / "job.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        spec = {}
    script = spec.get("script")
    if script:
        # an experiment / tool sent by the PC (app/<script>), run on this job folder
        cmd = [sys.executable, str(APP / H._safe(script)), str(jd)]
    else:
        cmd = [sys.executable, str(APP / "render_job.py"), str(jd)]
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    t0 = time.time()
    with LOCK:
        if j.get("cancel"):
            j["state"] = "cancelled"
            return
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=str(APP),
                                **({"start_new_session": True} if POSIX else {}))
        j["proc"] = proc
        j["moved"] = t0
    threading.Thread(target=_guard_job, args=(j, proc, t0), daemon=True).start()
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").rstrip()
        with LOCK:
            j["moved"] = time.time()
            if line.startswith("PROGRES "):
                try:
                    j["percent"] = float(line.split()[1])
                except ValueError:
                    pass
            elif line.startswith("REZULTAT "):
                try:
                    j["result"] = json.loads(line[9:])
                except ValueError:
                    j["error"] = "rezultatul lucrarii a iesit stricat"
            elif line.startswith("EROARE "):
                j["error"] = line[7:]
            else:
                j["lines"] = (j["lines"] + [line])[-30:]
    proc.wait()
    with LOCK:
        j["proc"] = None
        if j.get("cancel"):
            j["state"] = "cancelled"
        elif proc.returncode == 0 and j.get("result") is not None:
            j["state"] = "done"
            j["files"] = {p.name: p.stat().st_size for p in jd.iterdir()
                          if p.is_file() and p.name in ("out.mp4", "out.srt", "voce.mp3")}
            if (jd / "out").is_dir():
                j["files"].update({"out/" + p.name: p.stat().st_size for p in (jd / "out").iterdir() if p.is_file()})
        else:
            j["state"] = "error"
            j["error"] = (j.get("killed") or j.get("error")
                          or (" | ".join(j["lines"][-4:]) or f"cod {proc.returncode}"))


# ---------------------------------------------------------------- local LLM (Ollama) on this GPU
# The PC can have the translation made here too (no model on the PC): Ollama is installed on
# first use, its model pulled once, and /llm/chat forwards a chat request to it.
OLLAMA_DIR = BASE / "ollama"
OLLAMA_URL = "http://127.0.0.1:11434"
LLM = {"state": "none", "msg": "", "model": None}     # none | preparing | ready | error
LLM_LOCK = threading.Lock()


def _ollama_bin():
    for c in (shutil.which("ollama"), str(OLLAMA_DIR / "bin" / "ollama"), "/usr/local/bin/ollama"):
        if c and os.path.isfile(c):
            return c
    return None


def _ollama_up():
    import urllib.request
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _llm_prepare(model):
    try:
        exe = _ollama_bin()
        if not exe:
            LLM["msg"] = "instalez Ollama pe GPU..."
            OLLAMA_DIR.mkdir(parents=True, exist_ok=True)
            r = subprocess.run("curl -fsSL https://ollama.com/install.sh | sh", shell=True,
                               capture_output=True, timeout=1200)
            exe = _ollama_bin()
            if not exe:          # no root / no installer: the plain archive next to the worker
                for url, flag in (("https://ollama.com/download/ollama-linux-amd64.tgz", "xzf"),
                                  ("https://ollama.com/download/ollama-linux-amd64.tar.zst", "--zstd -xf")):
                    subprocess.run(f"curl -fsSL {url} -o /tmp/ollama.arc && tar {flag} /tmp/ollama.arc -C {OLLAMA_DIR}",
                                   shell=True, capture_output=True, timeout=1200)
                    exe = _ollama_bin()
                    if exe:
                        break
            if not exe:
                raise RuntimeError("Ollama nu s-a putut instala pe GPU: " + r.stderr.decode("utf-8", "replace")[-300:])
        if not _ollama_up():
            LLM["msg"] = "pornesc Ollama pe GPU..."
            env = dict(os.environ, OLLAMA_HOST="127.0.0.1:11434", OLLAMA_MODELS=str(OLLAMA_DIR / "models"),
                       OLLAMA_KEEP_ALIVE="20m", OLLAMA_MAX_LOADED_MODELS="1", OLLAMA_NUM_PARALLEL="2")
            OLLAMA_DIR.mkdir(parents=True, exist_ok=True)
            log = open(OLLAMA_DIR / "serve.log", "ab")
            subprocess.Popen([exe, "serve"], env=env, stdout=log, stderr=log, start_new_session=True)
            for _ in range(60):
                if _ollama_up():
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Ollama nu porneste pe GPU")
        LLM["msg"] = f"descarc modelul {model} pe GPU (o singura data)..."
        r = subprocess.run([exe, "pull", model], capture_output=True, timeout=3600,
                           env=dict(os.environ, OLLAMA_HOST="127.0.0.1:11434"))
        if r.returncode != 0:
            raise RuntimeError(f"modelul {model} nu s-a descarcat: " + r.stderr.decode("utf-8", "replace")[-300:])
        LLM.update(state="ready", msg="gata", model=model)
    except Exception as e:  # noqa: BLE001
        LLM.update(state="error", msg=str(e)[:400])


def llm_prepare(model):
    with LLM_LOCK:
        if LLM["state"] == "preparing" or (LLM["state"] == "ready" and LLM["model"] == model and _ollama_up()):
            return
        LLM.update(state="preparing", msg="pregatesc modelul de traducere pe GPU...", model=model)
    threading.Thread(target=_llm_prepare, args=(model,), daemon=True).start()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth(self):
        if not KEY or self.headers.get("X-Key") != KEY:
            self._json({"err": "cheie gresita"}, 403)
            return False
        _touch()
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _parts(self):
        u = urlparse(self.path)
        return [p for p in u.path.split("/") if p], parse_qs(u.query)

    @staticmethod
    def _safe(name):
        name = name.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise ValueError("nume gresit")
        return name

    def do_GET(self):
        if not self._auth():
            return
        parts, q = self._parts()
        if parts == ["llm", "st"]:
            if LLM["state"] == "ready" and not _ollama_up():
                LLM["state"] = "none"            # Ollama died: prepared again on the next request
            return self._json(dict(LLM))
        if parts == ["hb"]:
            _touch(hb=True)
            return self._json({"ok": 1, "watchdog": WATCH["state"], "idle_min": HB_IDLE_MIN,
                               "ffmpeg": FF_NOTE["msg"]})
        if parts == ["ping"]:
            gpu = ""
            try:
                gpu = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                                     capture_output=True, timeout=10).stdout.decode().strip().splitlines()[0]
            except Exception:
                pass
            with LOCK:
                busy = sum(1 for j in JOB.values() if j["state"] == "running")
            return self._json({"ver": version(), "files": app_files(), "busy": busy, "gpu": gpu,
                               "ffmpeg": FF_NOTE["msg"], "ffmpeg_diag": dict(FF_DIAG),
                               "hb_age": round(time.time() - HB["last"], 1), "watchdog": WATCH["state"],
                               "free_gb": round(shutil.disk_usage(str(BASE)).free / 2 ** 30, 1)})
        if len(parts) == 2 and parts[0] == "st":
            with LOCK:
                j = JOB.get(parts[1])
                if not j:
                    return self._json({"state": "unknown"}, 404)
                return self._json({k: v for k, v in j.items() if k != "proc"})
        if len(parts) >= 3 and parts[0] == "dl":
            try:
                p = JOBS / parts[1] / self._safe("/".join(parts[2:]))
            except ValueError:
                return self._json({"err": "nume"}, 400)
            if not p.is_file():
                return self._json({"err": "nu exista"}, 404)
            off = int((q.get("off") or ["0"])[0])
            size = p.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(max(0, size - off)))
            self.end_headers()
            with open(p, "rb") as f:
                f.seek(off)
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    self.wfile.write(b)
            return
        self._json({"err": "?"}, 404)

    def do_PUT(self):
        if not self._auth():
            return
        parts, q = self._parts()
        try:
            if len(parts) >= 2 and parts[0] == "app":
                dest = APP / self._safe("/".join(parts[1:]))
            elif len(parts) >= 3 and parts[0] == "up":
                dest = JOBS / self._safe(parts[1]) / self._safe("/".join(parts[2:]))
            else:
                return self._json({"err": "?"}, 404)
        except ValueError:
            return self._json({"err": "nume"}, 400)
        off = int((q.get("off") or ["0"])[0])
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = self._body()
        mode = "r+b" if off and dest.exists() else "wb"
        with open(dest, mode) as f:
            f.seek(off)
            f.write(data)
            f.truncate()
        self._json({"ok": 1, "size": dest.stat().st_size})

    def do_POST(self):
        if not self._auth():
            return
        parts, _ = self._parts()
        if parts == ["boot"]:
            code = self._body()
            if b"ThreadingHTTPServer" not in code:
                return self._json({"err": "cod gresit"}, 400)
            SELF.write_bytes(code)
            self._json({"ok": 1})
            self.wfile.flush()
            threading.Timer(0.5, lambda: os.execv(sys.executable, [sys.executable, str(SELF)])).start()
            return
        if len(parts) == 2 and parts[0] == "run":
            jid = self._safe(parts[1])
            spec = json.loads(self._body() or b"{}")
            jd = JOBS / jid
            with LOCK:
                if JOB.get(jid, {}).get("state") == "running":
                    return self._json({"err": "deja porneste"}, 409)
                # job.json only now: a job that is already running keeps reading its own
                jd.mkdir(parents=True, exist_ok=True)
                (jd / "job.json").write_text(json.dumps(spec), encoding="utf-8")
                JOB[jid] = {"state": "running", "percent": 0.0, "lines": [], "result": None, "error": None,
                            "proc": None, "started": time.time()}
            threading.Thread(target=run_job, args=(jid,), daemon=True).start()
            return self._json({"ok": 1})
        if parts == ["llm", "prep"]:
            req = json.loads(self._body() or b"{}")
            llm_prepare(str(req.get("model") or "qwen3:8b"))
            return self._json(dict(LLM))
        if parts == ["llm", "chat"]:
            import urllib.error
            import urllib.request
            body = self._body()
            r = urllib.request.Request(OLLAMA_URL + "/api/chat", data=body, method="POST",
                                       headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(r, timeout=1800) as resp:
                    out = resp.read()
                    code = resp.status
            except urllib.error.HTTPError as e:
                out, code = e.read(), e.code
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"Ollama pe GPU: {e}"}, 500)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if parts == ["vs"]:
            # a VoiceStudio call from this machine itself (loopback): some admin routes, like the
            # one-click install of an extra voice engine, only accept "desktop" (local) callers
            import urllib.error
            import urllib.request
            req = json.loads(self._body() or b"{}")
            body = json.dumps(req.get("json")).encode() if req.get("json") is not None else None
            r = urllib.request.Request("http://127.0.0.1:3900" + req.get("path", "/health"), data=body,
                                       method=req.get("method", "GET"),
                                       headers={"Content-Type": "application/json",
                                                "Authorization": f"Bearer {os.environ.get('OMNIVOICE_API_KEY', KEY)}"})
            try:
                with urllib.request.urlopen(r, timeout=120) as resp:
                    return self._json({"status": resp.status, "body": resp.read().decode("utf-8", "replace")})
            except urllib.error.HTTPError as e:
                return self._json({"status": e.code, "body": e.read().decode("utf-8", "replace")[:4000]})
            except Exception as e:  # noqa: BLE001
                return self._json({"status": 0, "body": str(e)})
        if len(parts) == 2 and parts[0] == "cancel":
            with LOCK:
                j = JOB.get(parts[1])
                if j and j.get("state") == "running":
                    j["cancel"] = True
                    if j.get("proc"):
                        _kill_tree(j["proc"])
                    else:
                        j["state"] = "cancelled"      # still waiting for the GPU: it never starts
            return self._json({"ok": 1})
        if len(parts) == 2 and parts[0] == "del":
            jid = self._safe(parts[1])
            with LOCK:
                j = JOB.pop(jid, None)
                if j:
                    j["cancel"] = True
                    _kill_tree(j.get("proc"))
            shutil.rmtree(JOBS / jid, ignore_errors=True)
            return self._json({"ok": 1})
        self._json({"err": "?"}, 404)


if __name__ == "__main__":
    # old half jobs from a previous run are of no use
    for d in JOBS.iterdir():
        if d.is_dir() and time.time() - d.stat().st_mtime > 3600:
            shutil.rmtree(d, ignore_errors=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    srv.daemon_threads = True
    srv.serve_forever()
