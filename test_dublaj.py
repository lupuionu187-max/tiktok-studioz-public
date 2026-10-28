#!/usr/bin/env python3
"""
test_dublaj.py - dubleaza primul minut dintr-un clip in doua feluri, ca sa le asculti:
  A_ca_acum            - setarile tale de acum (vocea clonata din rusa)
  B_mostra_romaneasca  - la fel, dar cu "Mostra romaneasca" pornita (mai putin accent)

Ruleaza separat de aplicatie (n-o atinge cat lucreaza): foloseste acelasi motor
(dubleaza.py) si un GPU inchiriat pornit, sau VoiceStudio de pe PC daca nu e niciunul.
Video-ul se face simplu (fara stergere AI / marire), ca sa iasa repede: aici conteaza vocea.
Rezultatul: folderul  test_voce\\dublaj\\

Pornire: dublu-click pe "Test dublaj.bat", sau trage un clip peste el.
"""
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dubleaza as engine  # noqa: E402
import randare  # noqa: E402

MINUTES = 1.0


def log(m):
    print(time.strftime("[%H:%M:%S] ") + m, flush=True)


def pick_machine(c):
    rented = [m for m in c["masini"] if m.get("activ") and engine.runpod_pod_id(m.get("adresa", ""))]
    local = [m for m in c["masini"] if m.get("id") == "local" and m.get("adresa")]
    for m in rented + local:
        d = engine.Dubber(c, log=lambda x: None, machine=m)
        if d.check_access() is None:
            return m
    raise SystemExit("Nu gasesc niciun VoiceStudio pornit (nici GPU inchiriat, nici pe PC).")


def main():
    c = engine.load_settings()
    vids = [v for v in engine.list_videos(c) if not engine.is_audio(v)]
    if len(sys.argv) > 1:
        want = Path(sys.argv[1]).name
        vids = [v for v in vids if v.name == want] or vids
    if not vids:
        raise SystemExit("Nu am clipuri video in folderul clipuri.")
    clip = vids[0]
    out_dir = HERE / "test_voce" / "dublaj"
    out_dir.mkdir(parents=True, exist_ok=True)
    m = pick_machine(c)
    log(f"Clip: {clip.name} (primul {MINUTES:g} min) | pe: {m['nume']}")

    base = dict(c, folder_dublate=str(out_dir), durata_max_min=MINUTES, format="mp4",
                # simple, fast picture: the point is the voice
                sterge_mod="blur" if c.get("sterge_mod") == "ai" else c.get("sterge_mod"),
                upscale_metoda="lanczos", iesire_1080=False, video_pe_gpu=False)
    variants = [("A_ca_acum", {"mostra_romana": bool(c.get("mostra_romana"))}),
                ("B_mostra_romaneasca", {"mostra_romana": True})]
    if c.get("mostra_romana"):
        variants[0] = ("A_fara_mostra_romaneasca", {"mostra_romana": False})
    for name, extra in variants:
        cc = dict(base, **extra)
        out = out_dir / f"{clip.stem}_{name}.mp4"
        if out.exists():
            log(f"{name}: exista deja ({out.name}), sar peste")
            continue
        log(f"=== {name} ===")
        d = engine.Dubber(cc, log=log, machine=m)
        voice = d.voice_id()
        tmp_out = engine.output_path(clip, cc)
        info = d.dub(clip, tmp_out, voice) or {}
        job = info.pop("render", None)
        if job:
            log("   fac video-ul (simplu)...")
            engine.finish_render(job, cc)
        if tmp_out.exists():
            shutil.move(str(tmp_out), str(out))
            log(f"{name}: gata -> {out.name}")
        else:
            log(f"{name}: nu a iesit niciun fisier")
    log(f"Gata. Asculta clipurile din: {out_dir}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        log(str(e))
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        log(f"EROARE: {e}")
