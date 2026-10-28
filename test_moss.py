#!/usr/bin/env python3
"""
test_moss.py - compara OmniVoice cu MOSS-TTS-v1.5 pe primul minut dintr-un clip.

Porneste UN pod separat pe RunPod (nu e in lista ta de GPU-uri, aplicatia nu il vede),
dubleaza primul minut din clip cu OmniVoice, instaleaza MOSS-TTS-v1.5 pe el, dubleaza
acelasi minut cu MOSS, apoi STERGE pod-ul (si daca ceva nu merge, si daca il opresti cu
Ctrl+C). Rezultatul: test_voce\\moss\\<clip>_1_OmniVoice.mp4 si <clip>_2_MOSS.mp4

Pornire: dublu-click pe "Test MOSS.bat" (sau trage un clip peste el).
Cost: pod de 48 GB (A40 / L40S) sau RTX 4090, cam 30-60 min -> ~0.5-1 $.
"""
import json
import secrets
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dubleaza as engine  # noqa: E402

MINUTES = 1.0
GPUS = ["NVIDIA A40", "NVIDIA L40S", "NVIDIA RTX 6000 Ada Generation", "NVIDIA L40", "NVIDIA GeForce RTX 4090"]
ENGINE = "moss-tts-v15"
T0 = time.time()


def log(m):
    print(time.strftime("[%H:%M:%S] ") + m, flush=True)


def vs_local(rv, method, path, body=None):
    """VoiceStudio called from the pod itself (the worker forwards it over loopback)."""
    r = rv._req("POST", "/vs", json={"method": method, "path": path, "json": body}, timeout=180)
    d = r.json()
    return d.get("status"), d.get("body")


def wait_health(d, limit=25 * 60):
    t = time.time()
    while time.time() - t < limit:
        if d.check_access() is None:
            return
        log(f"   VoiceStudio porneste pe pod ({int(time.time() - t)}s)...")
        time.sleep(20)
    raise RuntimeError("VoiceStudio nu a pornit pe pod")


def dub(c, m, clip, out_dir, name):
    cc = dict(c, folder_dublate=str(out_dir), durata_max_min=MINUTES, format="mp4",
              sterge_mod="blur" if c.get("sterge_mod") == "ai" else c.get("sterge_mod"),
              upscale_metoda="lanczos", iesire_1080=False, video_pe_gpu=False)
    out = out_dir / f"{clip.stem}_{name}.mp4"
    d = engine.Dubber(cc, log=log, machine=m)
    info = d.dub(clip, engine.output_path(clip, cc), d.voice_id()) or {}
    job = info.pop("render", None)
    if job:
        engine.finish_render(job, cc)
    tmp = engine.output_path(clip, cc)
    if tmp.exists():
        shutil.move(str(tmp), str(out))
        log(f"{name}: gata -> {out.name}")
    else:
        log(f"{name}: nu a iesit niciun fisier")


def main():
    c = engine.load_settings()
    if not c.get("runpod_api_key"):
        raise SystemExit("Lipseste cheia de API RunPod in aplicatie.")
    vids = [v for v in engine.list_videos(c) if not engine.is_audio(v)]
    if len(sys.argv) > 1:
        want = Path(sys.argv[1]).name
        vids = [v for v in vids if v.name == want] or vids
    if not vids:
        raise SystemExit("Nu am clipuri video in folderul clipuri.")
    clip = vids[0]
    out_dir = HERE / "test_voce" / "moss"
    out_dir.mkdir(parents=True, exist_ok=True)
    api = engine.RunPod(c["runpod_api_key"])
    key = "test-moss-" + secrets.token_urlsafe(16)
    log(f"Clip: {clip.name} (primul {MINUTES:g} min). Creez un pod separat pentru test...")
    pid, gpu, cost = api.create("test-moss", GPUS, {"OMNIVOICE_API_KEY": key}, True, log=log,
                                worker=True, disk_gb=120)
    log(f"Pod {pid} pe {gpu}" + (f" ({float(cost):.2f} $/ora)" if cost else ""))
    try:
        m = {"id": "testmoss", "nume": "Test MOSS", "adresa": f"https://{pid}-3900.proxy.runpod.net",
             "cheie": key, "activ": True}
        d = engine.Dubber(c, log=log, machine=m)
        wait_health(d)
        log("Instalez modelele de baza (OmniVoice + Whisper)...")
        d.install_models(progress=lambda x: log("   " + x))

        log("=== 1) OmniVoice (ce folosesti acum) ===")
        dub(c, m, clip, out_dir, "1_OmniVoice")

        log("=== Instalez MOSS-TTS-v1.5 pe pod (codul + ~7 GB de pachete) ===")
        rv = engine.RemoteVideo(m)
        rv.ready(log)
        st, body = vs_local(rv, "POST", f"/engines/sidecar/{ENGINE}/install")
        log(f"   pornit: HTTP {st} {str(body)[:200]}")
        if st and st >= 400 and "already" not in str(body):
            raise RuntimeError(f"instalarea MOSS a fost refuzata: {body}")
        last = ""
        t = time.time()
        while time.time() - t < 75 * 60:
            st, body = vs_local(rv, "GET", f"/engines/sidecar/{ENGINE}/install/status")
            try:
                s = json.loads(body)
            except (TypeError, ValueError):
                s = {}
            job = s.get("job") or {}
            if s.get("installed") and (not job or job.get("state") in (None, "done", "succeeded", "completed")):
                break
            if job.get("state") in ("failed", "error"):
                raise RuntimeError(f"instalarea MOSS a esuat: {job.get('error')} {job.get('remediation') or ''}")
            steps = [x for x in (job.get("steps") or []) if isinstance(x, dict)]
            now = next((x.get("id") or "" for x in steps if x.get("state") == "running"), "")
            wp = job.get("weights_progress") or {}
            msg = f"{now} {wp.get('percent', '')}".strip()
            if msg and msg != last:
                log(f"   {msg}")
                last = msg
            time.sleep(15)
        else:
            raise RuntimeError("instalarea MOSS dureaza prea mult")
        log("   MOSS instalat. Il fac motorul de voce al pod-ului...")
        r = d.s.post(f"{d.base}/engines/select", json={"family": "tts", "backend_id": ENGINE}, timeout=120)
        if r.status_code >= 400:
            st, body = vs_local(rv, "POST", "/engines/select", {"family": "tts", "backend_id": ENGINE})
            if st >= 400:
                raise RuntimeError(f"nu pot alege MOSS ca motor: {r.text[:200]} / {str(body)[:200]}")

        log("=== 2) MOSS-TTS-v1.5 (prima replica descarca ~16 GB de model, dureaza) ===")
        dub(c, m, clip, out_dir, "2_MOSS")
        log(f"Gata. Compara clipurile din: {out_dir}")
    finally:
        try:
            api.terminate(pid)
            log(f"Pod-ul de test a fost sters (a mers {(time.time() - T0) / 60:.0f} min"
                + (f", ~{float(cost) * (time.time() - T0) / 3600:.2f} $" if cost else "") + ").")
        except Exception as e:  # noqa: BLE001
            log(f"ATENTIE: nu am putut sterge pod-ul {pid} ({e}). Sterge-l de mana pe runpod.io!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Oprit.")
    except SystemExit as e:
        log(str(e))
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        log(f"EROARE: {e}")
