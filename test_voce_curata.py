#!/usr/bin/env python3
"""
test_voce_curata.py - test "pe nevazute": ce voce suna mai bine si mai aproape de original.

Pentru primele 2 clipuri (sau cele trase peste .bat), pe primul minut din fiecare:
  - varianta "cum e acum": dublajul aplicatiei (clona din clip, setarile tale);
  - "clona curata": vocile originale separate de muzica/zgomot, mostra cea mai curata pe vorbitor;
  - "clona curata + Seed-VC": aceeasi clona, cu timbrul vorbitorului pus inca o data peste (Seed-VC).
Doar vocile din clip; nicio voce salvata.
Aceeasi traducere la toate trei. Fisierele ies amestecate (A/B/C); raspuns.txt spune care e care.

Ruleaza pe placa ta video (VoiceStudio deschis pe PC); cu --gpu ruleaza pe un GPU inchiriat pornit.
Rezultatul: test_voce\\curat\\<clip>\\
Pornire: dublu-click pe "Test voce curata.bat" (sau trage clipuri peste el).
"""
import json
import random
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dubleaza as engine  # noqa: E402
import randare  # noqa: E402

SECONDS = 60
CLIPS = 2


def log(m):
    print(time.strftime("[%H:%M:%S] ") + m, flush=True)


def pick_pc(c):
    m = next((x for x in c["masini"] if x.get("id") == "local"), None)
    if not m or engine.Dubber(c, log=lambda x: None, machine=m).check_access() is not None:
        raise SystemExit("Deschide VoiceStudio pe calculator (testul ruleaza pe placa ta), apoi incearca din nou.")
    return m


def run_on_pc(jobdir):
    """exp_voce.py on this PC, its lines shown here as it goes."""
    import os
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    p = subprocess.Popen([sys.executable, str(HERE / "exp_voce.py"), str(jobdir)], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, env=env, cwd=str(HERE))
    err = None
    for raw in p.stdout:
        ln = raw.decode("utf-8", "replace").rstrip()
        if ln.startswith("PROGRES ") or ln.startswith("REZULTAT "):
            continue
        if ln.startswith("EROARE "):
            err = ln[7:]
        log("   " + ln[:300])
    p.wait()
    if p.returncode != 0:
        raise SystemExit(f"testul a dat eroare: {err or p.returncode}")


APP_URL = "http://127.0.0.1:8765"


def autostop(minutes=None):
    """The app deletes idle rented GPUs after N minutes and does not see this test working:
    switch that off while the test runs (returns the old value), put it back after."""
    import requests
    try:
        if minutes is None:
            old = requests.get(f"{APP_URL}/api/state", timeout=10).json()["settings"]["oprire_auto_min"]
            requests.post(f"{APP_URL}/api/settings", json={"oprire_auto_min": 0}, timeout=10)
            return old
        requests.post(f"{APP_URL}/api/settings", json={"oprire_auto_min": int(minutes)}, timeout=10)
    except Exception:  # noqa: BLE001
        return None


def pick_gpu(c):
    for m in c["masini"]:
        if m.get("id") != "local" and m.get("activ") and engine.rented_info(m) and engine.worker_url(m):
            d = engine.Dubber(c, log=lambda x: None, machine=m)
            if d.check_access() is None:
                return m
    raise SystemExit("Porneste intai un GPU inchiriat in aplicatie (testul ruleaza pe el), apoi incearca din nou.")


def excerpt(src, dst):
    ff = randare.find_ffmpeg()[0]
    subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-t", str(SECONDS),
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(dst)], check=True)


def main():
    c = engine.load_settings()
    vids = [v for v in engine.list_videos(c) if not engine.is_audio(v)]
    on_gpu = "--gpu" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--gpu"]
    if args:
        want = {Path(a).name for a in args}
        vids = [v for v in vids if v.name in want] or vids
    vids = vids[:CLIPS]
    if not vids:
        raise SystemExit("Nu am clipuri video in folderul clipuri.")
    m = pick_gpu(c) if on_gpu else pick_pc(c)
    old_stop = autostop() if on_gpu else None
    if on_gpu and old_stop is None:
        log("Atentie: nu am putut opri „Oprire automata” din aplicatie; pune-o pe „Niciodata” cat ruleaza testul.")
    try:
        run_clips(c, vids, m, on_gpu)
    finally:
        if old_stop:
            autostop(old_stop)


def run_clips(c, vids, m, on_gpu):
    log(f"Testez pe {m['nume']}: {', '.join(v.name for v in vids)} (primul minut din fiecare)")
    root = HERE / "test_voce" / "curat"
    for clip in vids:
        out = root / clip.stem
        shutil.rmtree(out, ignore_errors=True)
        (out / "mostre").mkdir(parents=True, exist_ok=True)
        tmp = out / "_tmp"
        tmp.mkdir()
        ex = tmp / ("bucata" + clip.suffix)
        excerpt(clip, ex)
        cc = dict(c, folder_dublate=str(tmp), format="mp3", mod_voce="sincron", voce="", sunet_fundal=False,
                  taie_pauze=False, durata_max_min=0, limbi_extra=[])
        log(f"=== {clip.name}: varianta de acum (transcriere, traducere, voce) ===")
        d = engine.Dubber(cc, log=lambda x: None if x.startswith("      ") else log(x), machine=m)
        st = d.prepare(ex, tmp / "acum.mp3", None)
        segs = [{"id": s["id"], "start": float(s["start"]), "end": float(s["end"]), "text": s.get("text") or "",
                 "text_original": s.get("text_original") or "", "speaker_id": s.get("speaker_id") or ""}
                for s in st["segments"]]
        d.finish(st)
        a_now = tmp / "acum.mp3"
        if not a_now.exists():
            raise SystemExit("varianta de acum nu a iesit")

        wav = tmp / "audio.wav"
        ff = randare.find_ffmpeg()[0]
        subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(ex), "-vn", "-ac", "1",
                        "-ar", "44100", str(wav)], check=True)
        spec = {"script": "exp_voce.py", "segments": segs, "steps": c["steps"], "cfg": c["cfg"],
                "speed": c["speed"]}
        if not on_gpu:
            log("=== variantele noi, pe placa ta (prima data se instaleaza uneltele: cateva minute) ===")
            jobdir = tmp / "job"
            jobdir.mkdir()
            shutil.copy(wav, jobdir / "audio.wav")
            (jobdir / "job.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            run_on_pc(jobdir)
            for f in (jobdir / "out").iterdir():
                shutil.copy(f, out / ("mostre" if "mostra" in f.name or "original" in f.name else "_tmp") / f.name)
        else:
            gpu_variants(m, tmp, out, wav, spec)
        finish_clip(clip, out, tmp, ex, a_now)
    log(f"Gata. Asculta A/B/C din fiecare folder din {root}, apoi deschide raspuns.txt.")


def gpu_variants(m, tmp, out, wav, spec):
    if True:
        log("=== variantele noi, pe GPU-ul inchiriat (prima data se instaleaza uneltele: cateva minute) ===")
        rv = engine.RemoteVideo(m)
        rv.ready(log)
        rv._req("PUT", "/app/exp_voce.py", data=(HERE / "exp_voce.py").read_bytes(), timeout=120)
        jid = "exp" + uuid.uuid4().hex[:10]
        rv.upload(jid, wav, "audio.wav")
        r = rv._req("POST", f"/run/{jid}", json=spec)
        if r.status_code not in (200, 409):
            raise SystemExit(f"GPU-ul nu a pornit testul: {r.text[:200]}")
        seen, t0 = set(), time.time()
        while True:
            time.sleep(5)
            stt = rv._req("GET", f"/st/{jid}").json()
            for ln in stt.get("lines") or []:
                if ln not in seen and not ln.startswith(("Traceback", "  File")):
                    seen.add(ln)
                    log("   " + ln[:200])
            if stt.get("state") == "running":
                if time.time() - t0 > 90 * 60:
                    raise SystemExit("testul pe GPU dureaza prea mult")
                continue
            if stt.get("state") != "done":
                raise SystemExit(f"testul pe GPU a dat eroare: {stt.get('error')}")
            break
        files = stt.get("files") or {}
        for name, size in files.items():
            if name.startswith("out/"):
                dest = out / ("mostre" if "mostra" in name or "original" in name else "_tmp") / Path(name).name
                rv.download(jid, name, dest, size)
        rv._req("POST", f"/del/{jid}", tries=1)


def finish_clip(clip, out, tmp, ex, a_now):
    if True:
        ff = randare.find_ffmpeg()[0]
        variants = [("cum e acum (clona din clip)", a_now),
                    ("clona din mostra curata", tmp / "v2_clona_curata.mp3"),
                    ("clona curata + timbrul pus inca o data (Seed-VC)", tmp / "v3_seedvc.mp3")]
        letters = ["A", "B", "C"]
        random.shuffle(letters)
        key = []
        for (label, path), letter in zip(variants, letters):
            if path.exists():
                shutil.copy(path, out / f"{letter}.mp3")
                key.append(f"  {letter}.mp3 = {label}")
        subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(ex), "-vn", "-b:a", "192k",
                        str(out / "original.mp3")], check=True)
        (out / "raspuns.txt").write_text("Deschide-l DUPA ce ai ascultat A, B si C:\n\n" + "\n".join(sorted(key))
                                         + "\n\noriginal.mp3 = sunetul original (pentru comparatie)\n"
                                         "mostre\\ = mostrele curate folosite pentru fiecare vorbitor\n",
                                         encoding="utf-8")
        shutil.rmtree(tmp, ignore_errors=True)
        log(f"{clip.name}: gata -> {out}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        log(str(e))
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        log(f"EROARE: {e}")
