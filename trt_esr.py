"""
trt_esr.py - Real-ESRGAN (SRVGGNetCompact) compiled with NVIDIA TensorRT, for the rented GPUs.

Same network, same weights, same picture (fp16, checked against PyTorch before use), usually
2-3x faster than plain PyTorch. The engine is built once per GPU (a few minutes, in the
background while the first clip still uses PyTorch) and kept on the pod's disk.

Everything here is optional: any problem (no TensorRT, build failed, different output, out of
memory) and sterge_ai.py simply keeps using PyTorch.

CLI (started by sterge_ai.py in the background):  python trt_esr.py prep <kind>
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRT_DIR = HERE / "modele" / "trt"
TRT_PIN = "10.13.3.9"
# input shapes the engine accepts (batch, 3, H, W); bigger frames fall back to PyTorch.
# OPT = what it is tuned for: 2 frames (sterge_ai's batch) of a 9:16 story clip around 540x960
# (the sources Real-ESRGAN gets most; 720x1280 / prescaled ~350x620 still run well).
MIN_SHAPE = (1, 3, 16, 16)
OPT_SHAPE = (2, 3, 960, 540)
MAX_SHAPE = (4, 3, 1280, 1280)


def _gpu_tag(torch):
    name = torch.cuda.get_device_name(0)
    return "".join(ch if ch.isalnum() else "_" for ch in name)[:40]


def _paths(kind, torch):
    try:
        import tensorrt as trt
        ver = trt.__version__
    except Exception:  # noqa: BLE001
        ver = "none"
    base = TRT_DIR / f"{kind}_{_gpu_tag(torch)}_trt{ver}"
    return {"engine": base.with_suffix(".engine"), "bad": base.with_suffix(".bad"),
            "tries": base.with_suffix(".tries"),
            "lock": base.with_suffix(".lock"), "onnx": TRT_DIR / f"{kind}_fp16.onnx",
            "log": TRT_DIR / "trt.log"}


def _note(line):
    try:
        TRT_DIR.mkdir(parents=True, exist_ok=True)
        with open(TRT_DIR / "trt.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + line + "\n")
    except OSError:
        pass


def supported():
    """Only on Linux (the rented GPUs), with a CUDA build of torch."""
    return sys.platform.startswith("linux") and os.environ.get("ESR_TRT", "1") != "0"


def ensure_tensorrt(torch):
    try:
        import tensorrt  # noqa: F401
        import onnx  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        pass
    major = (torch.version.cuda or "").split(".")[0]
    if major not in ("12", "13"):
        _note(f"torch e pe CUDA {torch.version.cuda}: fara TensorRT")
        return False
    pkg = f"tensorrt-cu{major}=={TRT_PIN}"
    _note(f"instalez {pkg}")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", pkg, "onnx"],
                       capture_output=True, timeout=1800)
    if r.returncode != 0:
        _note("pip a esuat: " + r.stderr.decode("utf-8", "replace")[-400:])
        return False
    import importlib
    importlib.invalidate_caches()
    return True


def build(kind):
    """Export to ONNX (fp16) and build the TensorRT engine. Runs in its own process."""
    import torch
    sys.path.insert(0, str(HERE))
    import sterge_ai
    if not torch.cuda.is_available() or not ensure_tensorrt(torch):
        return False
    import tensorrt as trt
    p = _paths(kind, torch)
    if p["engine"].exists() or p["bad"].exists():
        return p["engine"].exists()
    TRT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(p["lock"]), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        if time.time() - p["lock"].stat().st_mtime < 3600:
            return False      # another process is building it
        p["lock"].unlink(missing_ok=True)
        return build(kind)
    t0 = time.time()
    try:
        sterge_ai.download_model(kind)
        net = sterge_ai.build_esrgan(kind, torch).cuda().half()
        x = torch.rand(*OPT_SHAPE, device="cuda", dtype=torch.float16)
        kw = dict(input_names=["input"], output_names=["output"],
                  dynamic_axes={"input": {0: "b", 2: "h", 3: "w"}, "output": {0: "b", 2: "h4", 3: "w4"}},
                  opset_version=17)
        try:
            torch.onnx.export(net, x, str(p["onnx"]), dynamo=False, **kw)
        except TypeError:       # older torch: no dynamo switch (and no dynamo exporter by default)
            torch.onnx.export(net, x, str(p["onnx"]), **kw)
        logger = trt.Logger(trt.Logger.WARNING)
        builder = trt.Builder(logger)
        flags = 0
        if hasattr(trt.NetworkDefinitionCreationFlag, "STRONGLY_TYPED"):
            flags |= 1 << int(trt.NetworkDefinitionCreationFlag.STRONGLY_TYPED)
        if hasattr(trt.NetworkDefinitionCreationFlag, "EXPLICIT_BATCH"):
            flags |= 1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
        network = builder.create_network(flags)
        parser = trt.OnnxParser(network, logger)
        if not parser.parse(p["onnx"].read_bytes()):
            errs = "; ".join(str(parser.get_error(i)) for i in range(parser.num_errors))
            raise RuntimeError(f"ONNX: {errs[:300]}")
        cfg = builder.create_builder_config()
        cfg.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 2 << 30)
        prof = builder.create_optimization_profile()
        prof.set_shape("input", MIN_SHAPE, OPT_SHAPE, MAX_SHAPE)
        cfg.add_optimization_profile(prof)
        blob = builder.build_serialized_network(network, cfg)
        if blob is None:
            raise RuntimeError("build_serialized_network a dat None")
        tmp = p["engine"].with_suffix(".part")
        tmp.write_bytes(bytes(blob))
        # check against PyTorch before anyone uses it
        runner = Runner(tmp, torch)
        xs = torch.rand(2, 3, 240, 136, device="cuda", dtype=torch.float16)
        with torch.no_grad():
            ref = net(xs).float()
        got = runner(xs).float()
        diff = float((ref - got).abs().mean())
        if diff > 0.01:
            raise RuntimeError(f"rezultat diferit de PyTorch (diferenta medie {diff:.4f})")
        os.replace(tmp, p["engine"])
        _note(f"{kind}: motor TensorRT gata in {time.time() - t0:.0f}s (diferenta fata de PyTorch {diff:.5f})")
        return True
    except Exception as e:  # noqa: BLE001
        _note(f"{kind}: TensorRT nu merge ({e})")
        # out of memory (the card is shared with VoiceStudio and the current clip) is worth
        # another try later; anything else, or 3 failures, and this GPU stays on PyTorch
        try:
            tries = int(p["tries"].read_text() or 0) + 1 if p["tries"].exists() else 1
            p["tries"].write_text(str(tries))
            low = str(e).lower()
            transient = "memory" in low or isinstance(e, (ImportError, ModuleNotFoundError))
            if not transient or tries >= 3:
                p["bad"].write_text(str(e)[:500], encoding="utf-8")
        except OSError:
            pass
        return False
    finally:
        p["lock"].unlink(missing_ok=True)


class Runner:
    """Calls the engine on CUDA torch tensors (B, 3, H, W), fp16 in, fp16 out (x4)."""

    def __init__(self, engine_path, torch):
        import tensorrt as trt
        self.torch = torch
        self.trt = trt
        self.logger = trt.Logger(trt.Logger.ERROR)
        self.runtime = trt.Runtime(self.logger)      # must outlive the engine
        self.engine = self.runtime.deserialize_cuda_engine(Path(engine_path).read_bytes())
        if self.engine is None:
            raise RuntimeError("motorul TensorRT nu se poate incarca")
        self.ctx = self.engine.create_execution_context()
        # its own stream: TensorRT on the legacy default stream adds a full synchronisation per call
        self.stream = torch.cuda.Stream()

    def fits(self, x):
        b, c, h, w = x.shape
        return (c == 3 and MIN_SHAPE[0] <= b <= MAX_SHAPE[0] and MIN_SHAPE[2] <= h <= MAX_SHAPE[2]
                and MIN_SHAPE[3] <= w <= MAX_SHAPE[3])

    def __call__(self, x):
        torch = self.torch
        x = x.half().contiguous()         # made on the caller's stream
        b, _, h, w = x.shape
        cur = torch.cuda.current_stream(x.device)
        side = self.stream
        if not self.ctx.set_input_shape("input", tuple(x.shape)):
            raise RuntimeError(f"TensorRT nu accepta forma {tuple(x.shape)}")
        if tuple(self.ctx.get_tensor_shape("output")) != (b, 3, h * 4, w * 4):
            raise RuntimeError(f"TensorRT: iesire {tuple(self.ctx.get_tensor_shape('output'))} != {(b, 3, h * 4, w * 4)}")
        side.wait_stream(cur)             # x is ready before TensorRT reads it
        with torch.cuda.stream(side):
            out = torch.empty((b, 3, h * 4, w * 4), device=x.device, dtype=torch.float16)
            self.ctx.set_tensor_address("input", x.data_ptr())
            self.ctx.set_tensor_address("output", out.data_ptr())
            if not self.ctx.execute_async_v3(side.cuda_stream):
                raise RuntimeError("TensorRT execute a esuat")
        x.record_stream(side)             # x's memory is not reused while TensorRT may still read it
        cur.wait_stream(side)             # the caller sees the finished output
        out.record_stream(cur)
        return out


def get_runner(kind, torch, say=lambda kind, text: None):
    """The TensorRT runner when its engine is ready on this GPU; otherwise None (and the
    engine starts building in the background for the next clips)."""
    if not supported() or not torch.cuda.is_available():
        return None
    try:
        p = _paths(kind, torch)
        if p["bad"].exists():
            return None
        if p["engine"].exists():
            r = Runner(p["engine"], torch)
            say("INFO", "Real-ESRGAN pe TensorRT (motor compilat pentru placa asta)")
            return r
        if not p["lock"].exists():
            TRT_DIR.mkdir(parents=True, exist_ok=True)
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "prep", kind],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            say("INFO", "pregatesc TensorRT in fundal pentru clipurile urmatoare (acum PyTorch)")
    except Exception as e:  # noqa: BLE001
        _note(f"get_runner: {e}")
    return None


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "prep":
        ok = build(sys.argv[2])
        print(json.dumps({"ok": ok}), flush=True)
