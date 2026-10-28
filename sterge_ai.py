"""
Stergere AI a subtitrarilor arse in clip (STTN), doar in banda aleasa de tine.

Reteaua si greutatile vin din proiectul video-subtitle-remover
(https://github.com/YaoFANGUK/video-subtitle-remover, licenta Apache-2.0),
modelele "sttn-det" (umple exact textul gasit in banda de detectorul PP-OCRv4, orice culoare/font)
si "sttn-auto" (curata toata banda). Aici e doar partea de inferenta; banda o stii deja.

Rulat ca proces separat de aplicatie (ca placa video sa fie eliberata la final):
    python sterge_ai.py status
    python sterge_ai.py install
    python sterge_ai.py run <intrare.mp4> <iesire.mp4> <y_px> <h_px> [ffmpeg] [litere|banda]
Pe stdout scrie linii "PROGRES <0-100>" si "INFO <text>".
"""
import json
import math
import warnings
import os
import subprocess
import sys
from pathlib import Path

warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent
MODEL_DIR = HERE / "modele" / "sttn"
DET_SIZE = 66252587
TORCH_INDEX = "https://download.pytorch.org/whl/cu126"

IN_W, IN_H = 640, 120          # model input (16:3)
NEIGHBOR = 5                   # neighbour stride
REF_LEN = 10                   # reference frame step
CHUNK = 60                     # frames loaded at once
FEATHER = 6                    # px of soft edge above/below the band


LOG = HERE / "sterge_ai.log"


def _say(kind, text):
    print(f"{kind} {text}", flush=True)
    if kind != "PROGRES":
        note(f"{kind} {text}")


def note(line):
    """Kept in sterge_ai.log next to the app, so a failure can be read afterwards."""
    try:
        import time
        if LOG.exists() and LOG.stat().st_size > 400_000:
            LOG.write_text(LOG.read_text(encoding="utf-8", errors="replace")[-100_000:], encoding="utf-8")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except Exception:
        pass


def _no_window():
    return {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}


# ---------------------------------------------------------------- model files
# auto: cleans the whole band by itself (good on thin subtitles)
# det:  fills in exactly the pixels of a mask (the letters + their outline), for bold captions
_RAW = "https://github.com/YaoFANGUK/video-subtitle-remover/raw/main/backend/models/"
MODELS = {
    "auto": (MODEL_DIR / "infer_model.pth", 66271967, _RAW + "sttn-auto/infer_model.pth"),
    "det": (MODEL_DIR / "det.pth", DET_SIZE, _RAW + "sttn-det/sttn.pth"),
    # Real-ESRGAN (BSD-3-Clause), compact nets: rapid = animevideov3 (16 conv), general = general-x4v3 (32 conv)
    "esr_rapid": (MODEL_DIR / "realesr-animevideov3.pth", 2504012,
                  "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-animevideov3.pth"),
    "esr_general": (MODEL_DIR / "realesr-general-x4v3.pth", 4885111,
                    "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth"),
}


def model_ready(kind="auto") -> bool:
    path, size, _ = MODELS[kind]
    return path.exists() and path.stat().st_size == size


def assemble_model(kind="auto"):
    """The models ship split in pieces (infer_model.pth.00, .01 ...) next to the app."""
    if model_ready(kind):
        return True
    path, size, _ = MODELS[kind]
    parts = sorted(MODEL_DIR.glob(path.name + ".[0-9][0-9]"))
    if not parts:
        return False
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as out:
        for p in parts:
            out.write(p.read_bytes())
    if tmp.stat().st_size != size:
        tmp.unlink()
        return False
    tmp.replace(path)
    return True


def download_model(kind="auto"):
    if assemble_model(kind):
        return True
    import urllib.request
    path, size, url = MODELS[kind]
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    _say("INFO", f"descarc modelul STTN {kind} ({size >> 20} MB)...")
    with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
        got = 0
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            got += len(b)
            _say("PROGRES", f"{min(99, got * 100 // size)}")
    if tmp.stat().st_size != size:
        tmp.unlink()
        raise RuntimeError("modelul descarcat e incomplet")
    tmp.replace(path)
    return True


# ---------------------------------------------------------------- status / install
def status() -> dict:
    st = {"torch": False, "cuda": False, "gpu": "", "model": all(model_ready(k) or assemble_model(k) for k in MODELS),
          "numpy": False, "python": sys.version.split()[0]}
    try:
        import numpy  # noqa: F401
        st["numpy"] = True
    except Exception:
        pass
    try:
        import torch
        st["torch"] = torch.__version__
        st["cuda"] = bool(torch.cuda.is_available())
        if st["cuda"]:
            st["gpu"] = torch.cuda.get_device_name(0)
    except Exception as e:
        st["torch_err"] = str(e)[:200]
    return st


def _pip(args):
    cmd = [sys.executable, "-m", "pip", *args]
    _say("INFO", " ".join(cmd[2:])[:160])
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **_no_window())
    for line in p.stdout:
        t = line.decode("utf-8", "replace").strip()
        if t and ("Downloading" in t or "Installing" in t or "Successfully" in t or "ERROR" in t):
            _say("INFO", t[:200])
    p.wait()
    return p.returncode


def install():
    st = status()
    if not st["numpy"]:
        _pip(["install", "--quiet", "numpy"])
    if not st["torch"] or not st["cuda"]:
        if st["torch"] and not st["cuda"]:
            _say("INFO", f"torch {st['torch']} e fara placa video, il inlocuiesc cu varianta CUDA")
            _pip(["uninstall", "-y", "torch"])
        _say("INFO", "instalez PyTorch cu CUDA (~2.5 GB, dureaza cateva minute)...")
        code = _pip(["install", "torch", "--index-url", TORCH_INDEX])
        if code != 0:
            raise RuntimeError("pip nu a putut instala torch (vezi mesajele de mai sus)")
    for k in MODELS:
        download_model(k)
    # fresh process: the torch just installed is not importable in this one on Windows
    out = subprocess.run([sys.executable, str(Path(__file__).resolve()), "status"],
                         capture_output=True, **_no_window())
    try:
        st = json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except Exception:
        st = {}
    print("STATUS " + json.dumps(st), flush=True)
    if not st.get("cuda"):
        _say("INFO", "atentie: torch nu vede placa video, o sa mearga foarte incet pe procesor")


# ---------------------------------------------------------------- network (STTN, inference only)
def build_net(patchsize=((80, 15), (32, 6), (10, 5), (5, 3))):
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    class Deconv(nn.Module):
        def __init__(self, cin, cout, kernel_size=3, padding=0):
            super().__init__()
            self.conv = nn.Conv2d(cin, cout, kernel_size=kernel_size, stride=1, padding=padding)

        def forward(self, x):
            return self.conv(F.interpolate(x, scale_factor=2, mode="bilinear", align_corners=True))

    class MultiHeadedAttention(nn.Module):
        def __init__(self, patchsize, d_model):
            super().__init__()
            self.patchsize = patchsize
            self.query_embedding = nn.Conv2d(d_model, d_model, kernel_size=1, padding=0)
            self.value_embedding = nn.Conv2d(d_model, d_model, kernel_size=1, padding=0)
            self.key_embedding = nn.Conv2d(d_model, d_model, kernel_size=1, padding=0)
            self.output_linear = nn.Sequential(nn.Conv2d(d_model, d_model, kernel_size=3, padding=1),
                                               nn.LeakyReLU(0.2, inplace=True))

        def forward(self, x, b, c):
            bt, _, h, w = x.size()
            t = bt // b
            d_k = c // len(self.patchsize)
            output = []
            qs = torch.chunk(self.query_embedding(x), len(self.patchsize), dim=1)
            ks = torch.chunk(self.key_embedding(x), len(self.patchsize), dim=1)
            vs = torch.chunk(self.value_embedding(x), len(self.patchsize), dim=1)
            for (width, height), q, k, v in zip(self.patchsize, qs, ks, vs):
                out_w, out_h = w // width, h // height

                def emb(z):
                    z = z.view(b, t, d_k, out_h, height, out_w, width)
                    return z.permute(0, 1, 3, 5, 2, 4, 6).contiguous().view(b, t * out_h * out_w,
                                                                            d_k * height * width)
                q, k, v = emb(q), emb(k), emb(v)
                scores = torch.matmul(q, k.transpose(-2, -1)) / math.sqrt(q.size(-1))
                y = torch.matmul(F.softmax(scores, dim=-1), v)
                y = y.view(b, t, out_h, out_w, d_k, height, width)
                y = y.permute(0, 1, 4, 2, 5, 3, 6).contiguous().view(bt, d_k, h, w)
                output.append(y)
            return self.output_linear(torch.cat(output, 1))

    class FeedForward(nn.Module):
        def __init__(self, d_model):
            super().__init__()
            self.conv = nn.Sequential(
                nn.Conv2d(d_model, d_model, kernel_size=3, padding=2, dilation=2),
                nn.LeakyReLU(0.2, inplace=True),
                nn.Conv2d(d_model, d_model, kernel_size=3, padding=1),
                nn.LeakyReLU(0.2, inplace=True))

        def forward(self, x):
            return self.conv(x)

    class TransformerBlock(nn.Module):
        def __init__(self, patchsize, hidden=128):
            super().__init__()
            self.attention = MultiHeadedAttention(patchsize, d_model=hidden)
            self.feed_forward = FeedForward(hidden)

        def forward(self, x):
            x, b, c = x["x"], x["b"], x["c"]
            x = x + self.attention(x, b, c)
            x = x + self.feed_forward(x)
            return {"x": x, "b": b, "c": c}

    class InpaintGenerator(nn.Module):
        def __init__(self):
            super().__init__()
            channel = 256
            self.transformer = nn.Sequential(*[TransformerBlock(patchsize, hidden=channel) for _ in range(8)])
            self.encoder = nn.Sequential(
                nn.Conv2d(3, 64, kernel_size=3, stride=2, padding=1), nn.LeakyReLU(0.2, inplace=True),
                nn.Conv2d(64, 64, kernel_size=3, stride=1, padding=1), nn.LeakyReLU(0.2, inplace=True),
                nn.Conv2d(64, 128, kernel_size=3, stride=2, padding=1), nn.LeakyReLU(0.2, inplace=True),
                nn.Conv2d(128, channel, kernel_size=3, stride=1, padding=1), nn.LeakyReLU(0.2, inplace=True))
            self.decoder = nn.Sequential(
                Deconv(channel, 128, kernel_size=3, padding=1), nn.LeakyReLU(0.2, inplace=True),
                nn.Conv2d(128, 64, kernel_size=3, stride=1, padding=1), nn.LeakyReLU(0.2, inplace=True),
                Deconv(64, 64, kernel_size=3, padding=1), nn.LeakyReLU(0.2, inplace=True),
                nn.Conv2d(64, 3, kernel_size=3, stride=1, padding=1))

        def infer(self, feat):
            _, c, _, _ = feat.size()
            return self.transformer({"x": feat, "b": 1, "c": c})["x"]

    return InpaintGenerator()


def _ref_index(neighbor_ids, length):
    return [i for i in range(0, length, REF_LEN) if i not in neighbor_ids]


def _inpaint(net, x, torch, m=None):
    """x: (T,3,h,w) in [-1,1] on device -> (T,3,h,w) in [0,1]. m: holes to fill (det model)."""
    T = x.shape[0]
    feats = net.encoder(x if m is None else x * (1 - m))
    out = [None] * T
    for f in range(0, T, NEIGHBOR):
        nb = list(range(max(0, f - NEIGHBOR), min(T, f + NEIGHBOR + 1)))
        ref = _ref_index(nb, T)
        pred = net.infer(feats[nb + ref])
        img = (torch.tanh(net.decoder(pred[:len(nb)])) + 1) / 2
        for i, idx in enumerate(nb):
            out[idx] = img[i] if out[idx] is None else (out[idx] + img[i]) * 0.5
    return torch.stack(out)


def windows(y0, y1, H, win_h):
    """Horizontal strips of height win_h (the model's 16:3 shape) covering rows y0..y1."""
    band = y1 - y0
    n = max(1, math.ceil(band / win_h))
    if n == 1:
        c = (y0 + y1) // 2
        top = min(max(0, c - win_h // 2), max(0, H - win_h))
        return [(top, min(H, top + win_h))]
    step = (band - win_h) / (n - 1)
    res = []
    for i in range(n):
        top = int(round(y0 + i * step))
        top = min(max(0, top), max(0, H - win_h))
        res.append((top, min(H, top + win_h)))
    return res


def _probe(ff, video):
    fp = str(Path(ff).with_name("ffprobe" + (".exe" if ff.lower().endswith(".exe") else "")))
    if not os.path.isfile(fp):
        fp = "ffprobe"
    r = subprocess.run([fp, "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                        "stream=width,height,r_frame_rate,avg_frame_rate,nb_read_packets:format=duration",
                        "-of", "json", str(video)], capture_output=True, **_no_window())
    d = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    s = (d.get("streams") or [{}])[0]

    def rate(v):
        try:
            a, b = v.split("/")
            return float(a) / float(b) if float(b) else 0.0
        except Exception:
            return 0.0
    fps = rate(s.get("avg_frame_rate", "")) or rate(s.get("r_frame_rate", "")) or 30.0
    dur = float((d.get("format") or {}).get("duration") or 0)
    return int(s["width"]), int(s["height"]), fps, dur, int(s.get("nb_read_packets") or 0)


def _passthrough(ff):
    """'-fps_mode passthrough' (ffmpeg 5.1+) or the old spelling for ffmpeg 4.x."""
    try:
        out = subprocess.run([ff, "-hide_banner", "-version"], capture_output=True, timeout=20,
                             **_no_window()).stdout.decode("utf-8", "replace")
        import re as _re
        m = _re.search(r"ffmpeg version n?(\d+)\.(\d+)", out)
        if m and (int(m.group(1)), int(m.group(2))) < (5, 1):
            return ["-vsync", "passthrough"]
    except Exception:
        pass
    return ["-fps_mode", "passthrough"]


def _nvenc_ok(ff):
    try:
        r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
                           capture_output=True, timeout=60, **_no_window())
        return r.returncode == 0
    except Exception:
        return False


DET_W, DET_H = 432, 240         # det model input (1.8:1)
DET_PATCH = ((108, 60), (36, 20), (18, 10), (9, 5))


TEXT_DET = MODEL_DIR / "text_det.pt"   # PP-OCRv4 text detector (PaddleOCR, Apache-2.0) as TorchScript
DET_TEXT_W = 736                         # detector input width (multiple of 32)
_det = {}


def load_text_det(dev, torch):
    if "m" not in _det:
        m = None
        if TEXT_DET.exists():
            try:
                m = torch.jit.load(str(TEXT_DET), map_location="cpu").eval()
                try:
                    m = m.to(dev)
                    _det["dev"] = dev
                except Exception:
                    _det["dev"] = torch.device("cpu")
            except Exception as e:
                _say("INFO", f"detectorul de text nu s-a incarcat ({e}); folosesc metoda veche")
                m = None
        _det["m"] = m
    return _det["m"]


def detect_text(strip, rows_in_band, grow_px, torch, F):
    """Any kind of writing (any colour, font, language): a text detector finds the lines,
    then they are grown by grow_px so the whole letters + outline/shadow are covered.
    strip: (T,3,h,w) in [0,1] -> (T,1,h,w) in {0,1}, or None when there is no detector."""
    m = _det.get("m")
    if m is None:
        return None
    T, _, h, w = strip.shape
    dh = max(32, int(round(h * DET_TEXT_W / w / 32)) * 32)
    x = F.interpolate(strip, size=(dh, DET_TEXT_W), mode="bilinear", align_corners=False) * 2 - 1
    ddev = _det["dev"]
    probs = []
    for i in range(0, T, 16):
        xi = x[i:i + 16].to(ddev).float()
        try:
            probs.append(m(xi).to(strip.device))
        except Exception:
            if ddev.type == "cpu":
                raise
            _det["dev"] = ddev = torch.device("cpu")      # driver trouble on the card: go on on the CPU
            m.to(ddev)
            probs.append(m(xi.cpu()).to(strip.device))
    prob = torch.cat(probs)
    lines = (F.interpolate(prob.float(), size=(h, w), mode="bilinear", align_corners=False) > 0.3).float()
    mask = F.max_pool2d(lines, 2 * grow_px + 1, 1, grow_px) * rows_in_band
    if T > 2:     # the frames around too (captions fade in / out)
        mask = F.max_pool3d(mask.permute(1, 0, 2, 3).unsqueeze(0), (5, 1, 1), 1, (2, 0, 0))[0].permute(1, 0, 2, 3)
    return mask


def text_mask(strip, rows_in_band, k, F):
    """Letters of burned-in captions: white-ish pixels next to a dark outline/shadow,
    grown by k px so the outline goes too. strip: (T,3,h,w) in [0,1] -> (T,1,h,w) in {0,1}."""
    mn = strip.min(1, keepdim=True)[0]
    mx = strip.max(1, keepdim=True)[0]
    white = ((mn > 0.78) & ((mx - mn) < 0.18)).float()
    dark = (mx < 0.28).float()

    def grow(m, r):
        return F.max_pool2d(m, 2 * r + 1, 1, r)
    text = white * grow(dark, k)
    m = grow(text, k) * rows_in_band
    # also the frame before/after (captions fade in/out)
    if m.shape[0] > 2:
        m = F.max_pool3d(m.permute(1, 0, 2, 3).unsqueeze(0), (3, 1, 1), 1, (1, 0, 0))[0].permute(1, 0, 2, 3)
    return m


def build_esrgan(kind, torch):
    """Real-ESRGAN SRVGGNetCompact (x4)."""
    import torch.nn as nn
    import torch.nn.functional as F
    nconv = 16 if kind == "esr_rapid" else 32

    class SRVGG(nn.Module):
        def __init__(self, nf=64, nc=nconv, up=4):
            super().__init__()
            self.up = up
            body = [nn.Conv2d(3, nf, 3, 1, 1), nn.PReLU(nf)]
            for _ in range(nc):
                body += [nn.Conv2d(nf, nf, 3, 1, 1), nn.PReLU(nf)]
            body.append(nn.Conv2d(nf, 3 * up * up, 3, 1, 1))
            self.body = nn.ModuleList(body)
            self.ups = nn.PixelShuffle(up)

        def forward(self, x):
            o = x
            for layer in self.body:
                o = layer(o)
            return self.ups(o) + F.interpolate(x, scale_factor=self.up, mode="nearest")
    m = SRVGG()
    sd = torch.load(str(MODELS[kind][0]), map_location="cpu")
    m.load_state_dict(sd.get("params", sd))
    return m.eval()


def run(src, dst, y, h, ff="ffmpeg", method="litere"):
    """Old entry point: only the subtitle band."""
    process(src, dst, {"clean": {"y": y, "h": h, "method": method}}, ff)


PRESCALE = 1.3          # "prescale": ESRGAN input shrunk so x4 lands at ~1.3x the target size
PIN_READ_MAX = 192 << 20  # pinned (page-locked) read buffers only up to this size per chunk
PIN_WRITE_MAX = 384 << 20  # upscaled frames waiting for the encoder (pinned), at most about this much


def _read_full(stream, mv):
    """Fill the memoryview from the pipe (several reads); returns the number of bytes read."""
    got = 0
    n_all = len(mv)
    while got < n_all:
        n = stream.readinto(mv[got:])
        if not n:
            break
        got += n
    return got


def _enc_args(nv, same_ff):
    """Intermediate file only (ffmpeg makes the final video from it): fast and visually lossless."""
    if nv:
        # p1 = the fastest NVENC preset; at constant QP 13 nothing visible is lost. The image's old
        # ffmpeg 4.x knows only the legacy preset names.
        return ["-c:v", "h264_nvenc", "-preset", "p1" if same_ff else "fast", "-rc", "constqp", "-qp", "13"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "12"]


def process(src, dst, opts, ff="ffmpeg"):
    """One pass over the video on the video card:
       clean  = {"y","h","method"}  -> subtitles out of the band (rows of the original frame)
       crop   = [x, y, w, h]        -> only this part of the frame is kept (black bars out)
       up     = {"model", "w", "h", "prescale"} -> Real-ESRGAN x4, then down to exactly w x h
                (prescale: the input is first shrunk so x4 lands near w x h * 1.3: ~4x less work)
    Writes dst (video + the original sound, untouched)."""
    import queue
    import threading
    import numpy as np
    import torch
    import torch.nn.functional as F

    W, H, fps, dur, nframes = _probe(ff, src)
    clean, crop, up = opts.get("clean"), opts.get("crop"), opts.get("up")
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    _say("INFO", f"placa: {torch.cuda.get_device_name(0) if dev.type == 'cuda' else 'procesor (lent!)'}")
    half = dev.type == "cuda"
    cuda = dev.type == "cuda"
    if cuda:
        try:
            torch.backends.cudnn.benchmark = True     # fixed frame sizes: cuDNN picks the fastest kernels once
        except Exception:  # noqa: BLE001
            pass
    total = nframes or max(1, int(round(dur * fps)))

    # ---- subtitle band (same as before)
    net = None
    if clean:
        y0, y1 = max(0, int(clean["y"])), min(H, int(clean["y"]) + int(clean["h"]))
        if y1 - y0 < 4:
            clean = None
    if clean:
        det = clean.get("method", "litere") != "banda"
        kind = "det" if det else "auto"
        download_model(kind)
        net = build_net(DET_PATCH) if det else build_net()
        sd = torch.load(str(MODELS[kind][0]), map_location="cpu")
        net.load_state_dict(sd.get("netG", sd))
        net.to(dev).eval()
        if half:
            net.half()
        in_w, in_h = (DET_W, DET_H) if det else (IN_W, IN_H)
        win_h = min(H, max(8, int(W * in_h / in_w)))
        wins = windows(y0, y1, H, win_h)
        top = min(a for a, _ in wins) // 2 * 2
        bot = min(H // 2 * 2, (max(b for _, b in wins) + 1) // 2 * 2)
        rows = np.arange(top, bot, dtype=np.float32)
        if det:
            inside = ((rows >= y0 - 4) & (rows <= y1 + 4)).astype(np.float32)
            band_rows = torch.from_numpy(inside).to(dev).view(1, 1, -1, 1)
            k = max(3, int(round((y1 - y0) * 0.07)))
            grow = max(4, int(round((y1 - y0) * 0.16)))
            if load_text_det(dev, torch) is not None:
                _say("INFO", f"detector de text: {_det['dev'].type}")
        else:
            m = np.clip(np.minimum(rows - (y0 - FEATHER), (y1 + FEATHER) - rows) / FEATHER, 0, 1)
            band_rows = torch.from_numpy(m).to(dev).view(1, 1, -1, 1)
        _say("INFO", f"{W}x{H}, {total} cadre, banda {y0}-{y1}px, metoda {'doar literele' if det else 'toata banda'}")

    # ---- crop + upscale
    cx, cy, cw, ch = [int(v) for v in crop] if crop else (0, 0, W, H)
    ow, oh = (int(up["w"]) // 2 * 2, int(up["h"]) // 2 * 2) if up else (cw, ch)
    esr = None
    trt_run = [None]    # TensorRT engine for Real-ESRGAN, when this GPU has one ready
    pw, ph = cw, ch     # what Real-ESRGAN gets (smaller than the crop with "prescale")
    if up:
        kind = up.get("model", "esr_rapid")
        download_model(kind)
        esr = build_esrgan(kind, torch).to(dev)
        if half:
            esr.half()
        if cuda:
            try:
                esr = esr.to(memory_format=torch.channels_last)    # faster convolutions on tensor cores
            except Exception:  # noqa: BLE001
                pass
        if up.get("prescale"):
            kp = min(1.0, PRESCALE * max(ow / (4.0 * cw), oh / (4.0 * ch)))
            if kp < 0.95:
                pw, ph = max(16, int(round(cw * kp / 2)) * 2), max(16, int(round(ch * kp / 2)) * 2)
        _say("INFO", f"Real-ESRGAN {'rapid' if kind == 'esr_rapid' else 'general'}: {cw}x{ch} -> {ow}x{oh}"
                     + (f" (micsorat intai la {pw}x{ph})" if (pw, ph) != (cw, ch) else ""))
        if cuda:
            try:
                sys.path.insert(0, str(HERE))
                import trt_esr
                trt_run[0] = trt_esr.get_runner(kind, torch, _say)
            except Exception as e:  # noqa: BLE001 - PyTorch keeps working
                note(f"TensorRT: {e}")
    whole = bool(up or crop)
    # only the strip goes through Python when nothing else is done to the picture
    if whole:
        rd_w, rd_h, rd_y = W, H, 0
        vf = "format=rgb24"
    else:
        rd_w, rd_h, rd_y = W, bot - top, top
        vf = f"format=rgb24,crop={W}:{rd_h}:0:{top}"
    dec = subprocess.Popen([ff, "-hide_banner", "-loglevel", "error", "-i", str(src), "-map", "0:v:0",
                            "-vf", vf, *_passthrough(ff), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           stdout=subprocess.PIPE, **_no_window())
    part = str(dst) + ".part.mp4"
    nv = _nvenc_ok(ff)
    eff = ff
    if not nv:
        # on a rented GPU whose driver is too old for the new ffmpeg's NVENC: the image's old ffmpeg
        # can still encode on the GPU (simple pipe in, no new filters needed)
        try:
            alt = (HERE / "ffmpeg_nvenc.txt").read_text(encoding="utf-8").strip()
            if alt and _nvenc_ok(alt):
                eff, nv = alt, True
        except OSError:
            pass
    venc = _enc_args(nv, eff == ff)
    if whole:
        enc_args = [eff, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{ow}x{oh}", "-r", f"{fps:.6f}", "-i", "-",
                    "-i", str(src), "-map", "0:v", "-map", "1:a?", *venc, "-pix_fmt", "yuv420p",
                    "-c:a", "copy", "-movflags", "+faststart", part]
    else:
        # the cleaned strip is laid back over the original frames, frame by frame
        enc_args = [eff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                    "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{rd_h}", "-r", f"{fps:.6f}", "-i", "-",
                    "-filter_complex", f"[0:v]setpts=PTS-STARTPTS[b];[1:v]setpts=PTS-STARTPTS[s];"
                                       f"[b][s]overlay=0:{top}:eof_action=repeat[v]",
                    "-map", "[v]", "-map", "0:a?", *venc, "-pix_fmt", "yuv420p",
                    "-c:a", "copy", "-movflags", "+faststart", part]
    enc = subprocess.Popen(enc_args, stdin=subprocess.PIPE, **_no_window())
    fsize = rd_w * rd_h * 3
    done = skipped = 0
    esr_batch = 2 if cuda else 1
    esr_tiles = [1]     # strips per frame; grows when the card runs out of memory (VoiceStudio shares it)
    aa_half = [half]    # antialiased bicubic straight on the fp16 Real-ESRGAN output (fp32 if it refuses)
    cl = [cuda]         # channels_last for Real-ESRGAN (plain layout again if anything refuses it)

    def run_esr(x):
        """fp16 out on the video card, fp32 on the CPU (as before)."""
        r = trt_run[0]
        if r is not None and r.fits(x):
            try:
                return r(x)
            except Exception as e:  # noqa: BLE001 - back to PyTorch for the rest of the clip
                trt_run[0] = None
                _say("INFO", f"TensorRT a cazut ({str(e)[:120]}); continui cu PyTorch")
        xi = x.half() if half else x
        if cl[0]:
            try:
                return esr(xi.contiguous(memory_format=torch.channels_last))
            except RuntimeError as e:
                if "out of memory" in str(e).lower():
                    raise
                cl[0] = False
                esr.to(memory_format=torch.contiguous_format)
                note(f"channels_last nu merge aici ({str(e)[:150]}); Real-ESRGAN in formatul obisnuit")
        return esr(xi)

    def esr_apply(xb):
        """Real-ESRGAN on a batch; on CUDA out-of-memory it retries in horizontal strips."""
        while True:
            n = esr_tiles[0]
            try:
                if n == 1:
                    return run_esr(xb)
                hh = xb.shape[2]
                step = -(-hh // n)
                pad = 32
                parts = []
                for t in range(n):
                    a, b2 = t * step, min(hh, (t + 1) * step)
                    if a >= b2:
                        break
                    a0, b0 = max(0, a - pad), min(hh, b2 + pad)
                    xt = xb[:, :, a0:b0, :]
                    yt = run_esr(xt)
                    s4 = yt.shape[2] // (b0 - a0)
                    parts.append(yt[:, :, (a - a0) * s4:(a - a0 + b2 - a) * s4, :])
                return torch.cat(parts, dim=2)
            except RuntimeError as e:   # torch.cuda.OutOfMemoryError is a RuntimeError
                if "out of memory" not in str(e).lower() or n >= 16:
                    raise
                if cuda:
                    torch.cuda.empty_cache()
                esr_tiles[0] = n * 2
                _say("INFO", f"placa video plina: Real-ESRGAN continua pe {n * 2} bucati de cadru")

    def shrink(yb):
        """Real-ESRGAN output (x4) -> exactly ow x oh, fp32."""
        yb = yb.contiguous()
        if aa_half[0] and yb.dtype == torch.float16:
            try:
                return F.interpolate(yb, size=(oh, ow), mode="bicubic", align_corners=False,
                                     antialias=True).float()
            except RuntimeError as e:
                if "out of memory" in str(e).lower():
                    raise
                aa_half[0] = False
        return F.interpolate(yb.float(), size=(oh, ow), mode="bicubic", align_corners=False, antialias=True)

    # ---- reading: its own thread, so ffmpeg decodes the next chunk while the card works on this one
    chunk_bytes = CHUNK * fsize
    pin_in = [cuda and chunk_bytes <= PIN_READ_MAX]
    rq = queue.Queue(maxsize=2 if chunk_bytes <= (256 << 20) else 1)
    rstop = threading.Event()
    rerr = []

    def host_buf(n):
        if pin_in[0]:
            try:
                return torch.empty((n, rd_h, rd_w, 3), dtype=torch.uint8, pin_memory=True), True
            except Exception:  # noqa: BLE001 - no page-locked memory: plain memory
                pin_in[0] = False
        return torch.from_numpy(np.empty((n, rd_h, rd_w, 3), dtype=np.uint8)), False

    def rput(item):
        while not rstop.is_set():
            try:
                rq.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    def reader():
        try:
            while not rstop.is_set():
                buf, pinned = host_buf(CHUNK)
                got = _read_full(dec.stdout, memoryview(buf.numpy()).cast("B"))
                n = got // fsize
                if n and not rput((buf, n, pinned)):
                    return
                if n < CHUNK:
                    return
        except Exception as e:  # noqa: BLE001
            rerr.append(e)
        finally:
            rput(None)

    # ---- writing: its own thread, so the card does not wait for the encoder (and the other way round)
    out_frame = ow * oh * 3 if whole else rd_w * rd_h * 3
    pin_out = [cuda]
    per_item = esr_batch if esr is not None else CHUNK
    wq = queue.Queue(maxsize=max(3, min(3 * CHUNK, PIN_WRITE_MAX // max(1, out_frame)) // per_item))
    werr = []

    def to_host(t):
        """uint8 (N,h,w,3) -> (host array or pinned tensor, event to wait for or None)."""
        if not cuda:
            return t.numpy(), None
        if pin_out[0]:
            try:
                h_ = torch.empty(tuple(t.shape), dtype=torch.uint8, pin_memory=True)
                h_.copy_(t, non_blocking=True)
                ev = torch.cuda.Event()
                ev.record()
                return h_, ev
            except Exception:  # noqa: BLE001
                pin_out[0] = False
        return t.cpu().numpy(), None

    def writer():
        while True:
            item = wq.get()
            if item is None:
                return
            if werr:
                continue
            try:
                for data, ev in item:
                    if ev is not None:
                        ev.synchronize()
                    if not isinstance(data, np.ndarray):
                        data = data.numpy()
                    enc.stdin.write(memoryview(data).cast("B"))
            except Exception as e:  # noqa: BLE001
                werr.append(e)

    def as_bytes(x):
        """float frames [0,1] (N,3,h,w) -> uint8 (N,h,w,3) on the same device."""
        return (x.clamp(0, 1) * 255 + 0.5).byte().permute(0, 2, 3, 1).contiguous()

    wt = threading.Thread(target=writer, daemon=True)
    rt = threading.Thread(target=reader, daemon=True)
    wt.start()
    rt.start()
    try:
        with torch.no_grad():
            while True:
                item = rq.get()
                if item is None:
                    if rerr:
                        raise rerr[0]
                    break
                hbuf, nbuf, pinned = item
                hb = hbuf[:nbuf]
                if cuda:
                    hb = hb.to(dev, non_blocking=pinned)
                frames = hb.permute(0, 3, 1, 2).float() / 255.0
                del hb, hbuf
                if clean:
                    s0 = top - rd_y
                    strip = frames[:, :, s0:s0 + (bot - top), :]
                    result = strip
                    tm = None
                    if det:
                        tm = detect_text(strip, band_rows, grow, torch, F)
                        if tm is None:
                            tm = text_mask(strip, band_rows, k, F)
                        if tm.sum() < 20:
                            skipped += nbuf
                            tm = None
                        else:
                            result = strip.clone()
                            soft = F.avg_pool2d(tm, 5, 1, 2)
                    if not det or tm is not None:
                        if not det:
                            result = strip.clone()
                        for (a, b2) in wins:
                            sl = slice(a - top, b2 - top)
                            x = F.interpolate(strip[:, :, sl, :], size=(in_h, in_w), mode="bilinear",
                                              align_corners=False) * 2 - 1
                            hole = None
                            if det:
                                hole = (F.interpolate(tm[:, :, sl, :], size=(in_h, in_w), mode="bilinear",
                                                      align_corners=False) > 0.1).float()
                            if half:
                                x = x.half()
                                hole = hole.half() if hole is not None else None
                            comp = _inpaint(net, x, torch, hole).float()
                            comp = F.interpolate(comp, size=(b2 - a, W), mode="bilinear", align_corners=False)
                            mm = soft[:, :, sl, :] if det else band_rows[:, :, sl, :]
                            result[:, :, sl, :] = mm * comp + (1 - mm) * result[:, :, sl, :]
                        frames[:, :, s0:s0 + (bot - top), :] = result
                outs = []
                if whole:
                    frames = frames[:, :, cy:cy + ch, cx:cx + cw]
                    if esr is not None:
                        # each batch leaves the card as 8-bit right away (copied while the card goes on):
                        # a whole chunk of upscaled frames would take >1 GB of VRAM
                        for i in range(0, frames.shape[0], esr_batch):
                            fb = frames[i:i + esr_batch]
                            if (pw, ph) != (cw, ch):
                                fb = F.interpolate(fb, size=(ph, pw), mode="bicubic", align_corners=False,
                                                   antialias=True).clamp(0, 1)
                            yb = shrink(esr_apply(fb))
                            outs.append(to_host(as_bytes(yb)))
                            del yb
                    elif (ow, oh) != (cw, ch):
                        frames = F.interpolate(frames, size=(oh, ow), mode="bicubic", align_corners=False,
                                               antialias=True).clamp(0, 1)
                if not outs:
                    outs.append(to_host(as_bytes(frames)))
                del frames
                if werr:
                    raise werr[0]
                if esr is not None:
                    for o in outs:          # one batch per item: the pinned memory waiting stays small
                        wq.put([o])
                else:
                    wq.put(outs)
                done += nbuf
                _say("PROGRES", f"{min(99.0, done * 100.0 / total):.1f}")
                if nbuf < CHUNK:
                    break
    finally:
        rstop.set()
        rt.join(0.5)
        if rt.is_alive():
            try:
                dec.kill()          # stopped half way: the reader must not wait for more frames
            except Exception:  # noqa: BLE001
                pass
            while rt.is_alive():
                try:
                    rq.get(timeout=0.2)
                except queue.Empty:
                    pass
        wq.put(None)
        wt.join()
        try:
            enc.stdin.close()
        except Exception:
            pass
        dec.stdout.close()
        dec.wait()
        enc.wait()
    if werr:
        raise RuntimeError(f"ffmpeg nu a putut scrie clipul ({werr[0]})")
    if enc.returncode != 0 or done == 0:
        raise RuntimeError(f"ffmpeg nu a putut {'citi' if done == 0 else 'scrie'} clipul "
                           f"(cod {dec.returncode if done == 0 else enc.returncode})")
    os.replace(part, dst)
    extra = f", {skipped} fara text sarite" if skipped else ""
    if esr is not None:
        extra += ", Real-ESRGAN pe " + ("TensorRT" if trt_run[0] is not None else "PyTorch")
    _say("INFO", f"gata: {done} cadre{extra}, codare {'NVENC' if nv else 'procesor'}")
    _say("PROGRES", "100")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if cmd == "status":
            print(json.dumps(status()), flush=True)
        elif cmd == "install":
            install()
        elif cmd == "process":
            note(f"START {sys.executable} | {' '.join(sys.argv[2:])}")
            process(sys.argv[2], sys.argv[3], json.loads(sys.argv[4]), sys.argv[5] if len(sys.argv) > 5 else "ffmpeg")
        elif cmd == "run":
            note(f"START {sys.executable} | {' '.join(sys.argv[2:])}")
            run(sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5]),
                sys.argv[6] if len(sys.argv) > 6 else "ffmpeg",
                sys.argv[7] if len(sys.argv) > 7 else "litere")
        else:
            raise SystemExit("comanda necunoscuta")
    except Exception as e:
        import traceback
        print(f"EROARE {e}", flush=True)
        note(f"EROARE ({cmd}) {e}\n{traceback.format_exc()[-1500:]}")
        sys.exit(1)
