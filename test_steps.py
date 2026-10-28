#!/usr/bin/env python3
"""
test_steps.py - gaseste "sweet spot"-ul la steps (cati pasi face vocea la generare).

Dubleaza primul minut dintr-un clip de 3 ori, cu steps 24 / 32 / 64, cu ACEEASI
traducere (tradusa o singura data), ca singura diferenta sa fie steps. Fisierele ies cu nume
amestecate (1.mp3 ... 3.mp3), ca sa le asculti fara sa stii care e care ("orb"):
noteaza-le, apoi deschide raspuns.txt: acolo e ce steps are fiecare si cat a durat vocea.

Ruleaza separat de aplicatie (nu porni dublarea in acelasi timp). Foloseste VoiceStudio de pe
PC (gratis); un GPU inchiriat doar daca VoiceStudio de pe PC nu e pornit.
Rezultatul: folderul  test_voce\\steps\\

Pornire: dublu-click pe "Test steps.bat" (sau trage un clip peste el).
"""
import random
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dubleaza as engine  # noqa: E402

MINUTES = 1.0
STEPS = [24, 32, 64]


def log(m):
    print(time.strftime("[%H:%M:%S] ") + m, flush=True)


def pick_machine(c):
    rented = [m for m in c["masini"] if m.get("activ") and engine.runpod_pod_id(m.get("adresa", ""))]
    local = [m for m in c["masini"] if m.get("id") == "local" and m.get("adresa")]
    for m in local + rented:          # the PC first: free (only the relative times matter)
        d = engine.Dubber(c, log=lambda x: None, machine=m)
        if d.check_access() is None:
            return m
    raise SystemExit("Nu gasesc niciun VoiceStudio pornit (nici GPU inchiriat, nici pe PC).")


# the same translation for every run: the first run's lines are reused by source text
_tr_cache = {}
_orig_translate = engine.Dubber._agent_translate


def _cached_translate(self, segs, source_lang, extra, glossary):
    todo = [s for s in segs if s["text_original"] not in _tr_cache]
    if todo:
        got = _orig_translate(self, todo, source_lang, extra, glossary)
        for s in todo:
            t = (got.get(s["id"]) or {}).get("text")
            if t:
                _tr_cache[s["text_original"]] = t
    return {s["id"]: {"id": s["id"], "text": _tr_cache.get(s["text_original"], "")} for s in segs
            if _tr_cache.get(s["text_original"])}


engine.Dubber._agent_translate = _cached_translate

# time spent generating the voice (only that, not transcription / export)
_gen_time = [0.0]
_orig_generate = engine.Dubber._generate


def _timed_generate(self, gen):
    t = time.time()
    try:
        return _orig_generate(self, gen)
    finally:
        _gen_time[0] += time.time() - t


engine.Dubber._generate = _timed_generate


def main():
    c = engine.load_settings()
    vids = [v for v in engine.list_videos(c) if not engine.is_audio(v)]
    if len(sys.argv) > 1:
        want = Path(sys.argv[1]).name
        vids = [v for v in vids if v.name == want] or vids
    if not vids:
        raise SystemExit("Nu am clipuri video in folderul clipuri.")
    clip = vids[0]
    out_dir = HERE / "test_voce" / "steps"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    m = pick_machine(c)
    log(f"Clip: {clip.name} (primul {MINUTES:g} min) | pe: {m['nume']}")

    base = dict(c, folder_dublate=str(out_dir / "_tmp"), durata_max_min=MINUTES, format="mp3",   # only the voice: no video to render
                sterge_mod="blur" if c.get("sterge_mod") == "ai" else c.get("sterge_mod"),
                upscale_metoda="lanczos", iesire_1080=False, video_pe_gpu=False,
                # identical text in every run: no rewrites / regenerations that differ between runs
                agent_fit=False, qc_auto=False, verificare_limba=False)
    labels = list(range(1, len(STEPS) + 1))
    random.shuffle(labels)
    key = []
    for steps, label in zip(STEPS, labels):
        cc = dict(base, steps=steps)
        log(f"=== varianta {label} ===")
        _gen_time[0] = 0.0
        d = engine.Dubber(cc, log=lambda x: None if x.startswith("      ") else log(x), machine=m)
        tmp_out = engine.output_path(clip, cc)
        info = d.dub(clip, tmp_out, d.voice_id()) or {}
        job = info.pop("render", None)
        if job:
            engine.finish_render(job, cc)
        out = out_dir / f"{label}.mp3"
        if tmp_out.exists():
            shutil.move(str(tmp_out), str(out))
            log(f"varianta {label}: gata (vocea a durat {_gen_time[0]:.0f} s)")
        else:
            log(f"varianta {label}: nu a iesit niciun fisier")
        key.append((label, steps, _gen_time[0]))
    shutil.rmtree(out_dir / "_tmp", ignore_errors=True)
    lines = ["Raspuns (deschide-l DUPA ce ai ascultat 1.mp3 ... 3.mp3):", ""]
    base_t = next((t for _, s, t in key if s == 64), 0) or 1
    for label, steps, t in sorted(key):
        lines.append(f"  {label}.mp3  =  steps {steps:>2}   vocea in {t:5.0f} s   "
                     f"({t / base_t * 100:3.0f}% din timpul de la steps 64)")
    lines += ["", "Alege cel mai mic steps la care nu mai auzi diferenta fata de 64,",
              "si spune-mi-l: il pun in aplicatie."]
    (out_dir / "raspuns.txt").write_text("\n".join(lines), encoding="utf-8")
    log(f"Gata. Asculta 1.mp3 ... 3.mp3 din {out_dir}, apoi deschide raspuns.txt")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        log(str(e))
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        log(f"EROARE: {e}")
