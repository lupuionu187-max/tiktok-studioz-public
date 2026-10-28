#!/usr/bin/env python3
"""
test_vorbitori.py - test: cine vorbeste cand, cu pyannote community-1, pe cateva clipuri.

Pentru fiecare clip (primul minut):
  - VoiceStudio de pe GPU transcrie (cu ora fiecarui cuvant) si spune vorbitorii cum face acum;
  - vorbitori.py (community-1) spune cine vorbeste in fiecare moment: pe sunetul brut si pe vocile
    separate de muzica;
  - fiecare replica e impartita acolo unde se schimba vorbitorul (dupa ora cuvintelor).
Nu dubleaza nimic si nu schimba nimic in aplicatie. Rezultatul: test_vorbitori\\raport.txt (+ date.json).

Ruleaza pe un GPU inchiriat pornit din aplicatie. Pornire: dublu-click pe "Test vorbitori.bat"
(sau trage clipuri peste el).
"""
import json
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
CLIPS = 5
APP_URL = "http://127.0.0.1:8765"
DEFAULT = ["azqylda_7693960684039671053", "awr643qa_7693994577258171678", "grfddf3_7693965629002386719",
           "husiiu_7693987346810785054", "ecnshf_7693963104316673310"]


def log(m):
    print(time.strftime("[%H:%M:%S] ") + m, flush=True)


# ---------------------------------------------------------------- words -> speakers (no GPU needed)
def speaker_at(turns, t, tol=0.3):
    """The speaker talking at time t (the nearest turn when t falls just between two)."""
    best, bd = None, 1e9
    for s, e, lab in turns:
        if s <= t <= e:
            return lab
        d = min(abs(t - s), abs(t - e))
        if d < bd:
            best, bd = lab, d
    return best if bd <= tol else None


def split_by_speaker(segments, turns, min_dur=0.3):
    """Each line cut where the speaker changes, by the time of its words. A piece shorter than
    min_dur joins its neighbour. -> [{"start","end","spk","text","line"}]"""
    out = []
    for seg in segments:
        words = [w for w in (seg.get("words") or []) if str(w.get("text") or "").strip()]
        if not words:
            out.append({"start": float(seg["start"]), "end": float(seg["end"]), "spk": None,
                        "text": seg.get("text_original") or seg.get("text") or "", "line": seg.get("id")})
            continue
        labs, prev = [], None
        for w in words:
            lab = speaker_at(turns, (float(w["start"]) + float(w["end"])) / 2) or prev
            labs.append(lab)
            prev = lab
        first = next((x for x in labs if x is not None), None)
        labs = [x if x is not None else first for x in labs]
        pieces = []
        for w, lab in zip(words, labs):
            if pieces and pieces[-1]["spk"] == lab:
                pieces[-1]["words"].append(w)
            else:
                pieces.append({"spk": lab, "words": [w]})
        merged = True
        while merged and len(pieces) > 1:
            merged = False
            for i, p in enumerate(pieces):
                dur = float(p["words"][-1]["end"]) - float(p["words"][0]["start"])
                if dur < min_dur:
                    j = i - 1 if i > 0 else i + 1
                    if j < i:
                        pieces[j]["words"] += p["words"]
                    else:
                        pieces[j]["words"] = p["words"] + pieces[j]["words"]
                    del pieces[i]
                    merged = True
                    break
            k = 1
            while k < len(pieces):                       # neighbours with the same speaker: one piece
                if pieces[k]["spk"] == pieces[k - 1]["spk"]:
                    pieces[k - 1]["words"] += pieces.pop(k)["words"]
                else:
                    k += 1
        for p in pieces:
            out.append({"start": float(p["words"][0]["start"]), "end": float(p["words"][-1]["end"]),
                        "spk": p["spk"], "text": " ".join(str(w["text"]).strip() for w in p["words"]),
                        "line": seg.get("id")})
    return out


def names(pieces):
    """SPEAKER_00 -> A, B, C... in order of first appearance."""
    order = []
    for p in pieces:
        if p["spk"] is not None and p["spk"] not in order:
            order.append(p["spk"])
    return {s: chr(ord("A") + i) if i < 26 else str(s) for i, s in enumerate(order)}


def fmt_t(t):
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


# ---------------------------------------------------------------- GPU
def autostop(minutes=None):
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
            if engine.Dubber(c, log=lambda x: None, machine=m).check_access() is None:
                return m
    raise SystemExit("Porneste intai un GPU inchiriat in aplicatie (testul ruleaza pe el), apoi incearca din nou.")


def transcribe(c, m, wav):
    """VoiceStudio on the GPU: lines with the time of every word, and its own speakers."""
    d = engine.Dubber(c, log=lambda x: log("   " + x.strip()), machine=m)
    d.ensure_diarization(lambda x: log("   " + x.strip()))
    with open(wav, "rb") as fh:
        r = d.s.post(f"{d.base}/dub/upload", files={"video": (wav.name, fh)},
                     data={"job_id": "dv" + uuid.uuid4().hex[:12], "input_type": "audio"}, timeout=(10, 900))
    if r.status_code >= 400:
        raise SystemExit(f"VoiceStudio: upload HTTP {r.status_code} {r.text[:200]}")
    up = r.json()
    d.job_id, d.task_id = up["job_id"], up["task_id"]
    d.wait(f"{d.base}/tasks/stream/{d.task_id}", "ready")
    res = {}
    d.wait(f"{d.base}/dub/transcribe-stream/{d.job_id}", "done",
           lambda ev: res.update(ev) if ev.get("type") == "final" else None)
    return res.get("segments") or []


def diarize(m, wav):
    rv = engine.RemoteVideo(m)
    rv.ready(log)
    for f in ("vorbitori.py", "randare.py"):
        rv._req("PUT", f"/app/{f}", data=(HERE / f).read_bytes(), timeout=120)
    jid = "vb" + uuid.uuid4().hex[:10]
    rv.upload(jid, wav, "audio.wav")
    r = rv._req("POST", f"/run/{jid}", json={"script": "vorbitori.py", "no_slot": True, "audio": "audio.wav",
                                              "vocals": True})
    if r.status_code not in (200, 409):
        raise SystemExit(f"GPU-ul nu a pornit testul: {r.text[:200]}")
    seen, t0 = set(), time.time()
    try:
        while True:
            time.sleep(4)
            stt = rv._req("GET", f"/st/{jid}").json()
            for ln in stt.get("lines") or []:
                if ln not in seen and ln.startswith("   "):
                    seen.add(ln)
                    log("   " + ln.strip()[:200])
            if stt.get("state") == "running":
                if time.time() - t0 > 60 * 60:
                    raise SystemExit("testul pe GPU dureaza prea mult")
                continue
            if stt.get("state") != "done":
                raise SystemExit(f"detectarea vorbitorilor pe GPU a dat eroare: {stt.get('error')}")
            break
        files = stt.get("files") or {}
        dest = HERE / "test_vorbitori" / "_tmp" / (jid + ".json")
        dest.parent.mkdir(parents=True, exist_ok=True)
        rv.download(jid, "out/vorbitori.json", dest, files["out/vorbitori.json"])
        return json.loads(dest.read_text(encoding="utf-8"))
    finally:
        try:
            rv._req("POST", f"/del/{jid}", tries=1)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- report
def clip_report(name, segs, diar):
    lines = [f"=== {name} (primul minut) ==="]
    vs_spk = sorted({s.get("speaker_id") or "?" for s in segs})
    lines.append(f"VoiceStudio (cum e acum): {len(segs)} replici, {len(vs_spk)} vorbitor(i): {', '.join(vs_spk)}")
    summary = {"clip": name, "vs_lines": len(segs), "vs_speakers": len(vs_spk), "variants": {}}
    best = "voci" if "voci" in diar["variants"] else "brut"
    for var, d in diar["variants"].items():
        pieces = split_by_speaker(segs, d["exclusive"])
        mixed = sum(1 for s in segs if len({p["spk"] for p in pieces if p["line"] == s.get("id")}) > 1)
        lines.append(f"community-1 pe {'vocile separate' if var == 'voci' else 'sunetul brut'}: "
                     f"{d['speakers']} vorbitori ({d['secs']} s); {mixed} din {len(segs)} replici au mai multi "
                     f"vorbitori -> {len(pieces)} replici dupa impartire")
        summary["variants"][var] = {"speakers": d["speakers"], "mixed_lines": mixed, "pieces": len(pieces),
                                    "secs": d["secs"], "split": pieces}
    lines.append("")
    lines.append("Replicile de acum (VoiceStudio), cu vorbitorul lor:")
    for s in segs:
        lines.append(f"  [{fmt_t(float(s['start']))}-{fmt_t(float(s['end']))}] {s.get('speaker_id') or '?'}: "
                     f"{(s.get('text_original') or s.get('text') or '').strip()}")
    pieces = summary["variants"][best]["split"]
    nm = names(pieces)
    lines.append("")
    lines.append(f"Dupa impartire (community-1, {'vocile separate' if best == 'voci' else 'sunetul brut'}):")
    last_line = None
    for p in pieces:
        mark = "  " if p["line"] == last_line else "* "
        last_line = p["line"]
        lines.append(f"{mark}[{fmt_t(p['start'])}-{fmt_t(p['end'])}] {nm.get(p['spk'], '?')}: {p['text']}")
    lines.append("  (* = inceputul unei replici de acum; fara * = bucata noua taiata din aceeasi replica)")
    lines.append("")
    return lines, summary


def excerpt_wav(src, dst):
    ff = randare.find_ffmpeg()[0]
    subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-t", str(SECONDS), "-vn",
                    "-ac", "1", "-ar", "44100", str(dst)], check=True)


def find_clips(c, args):
    root = engine.folder(c["folder_clipuri"])
    allv = [p for p in root.rglob("*") if p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm")]
    if args:
        want = {Path(a).stem for a in args}
        got = [p for p in allv if p.stem in want] + [Path(a) for a in args if Path(a).is_file()]
    else:
        got = [p for stem in DEFAULT for p in allv if p.stem == stem]
        if len(got) < CLIPS:
            got += [p for p in sorted(allv, key=lambda p: -p.stat().st_mtime) if p not in got]
    uniq = []
    for p in got:
        if p.resolve() not in [u.resolve() for u in uniq]:
            uniq.append(p)
    return uniq[:CLIPS]


def main():
    c = engine.load_settings()
    clips = find_clips(c, sys.argv[1:])
    if not clips:
        raise SystemExit("Nu am gasit clipuri in folderul clipuri.")
    m = pick_gpu(c)
    out = HERE / "test_vorbitori"
    tmp = out / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    old_stop = autostop()
    log(f"Testez pe {m['nume']}: {', '.join(p.name for p in clips)} (primul minut din fiecare)")
    report, data = [f"Test vorbitori - {time.strftime('%Y-%m-%d %H:%M')} - pe {m['nume']}", ""], []
    try:
        for clip in clips:
            log(f"=== {clip.name} ===")
            wav = tmp / (clip.stem + ".wav")
            excerpt_wav(clip, wav)
            log("   VoiceStudio transcrie...")
            segs = transcribe(c, m, wav)
            log(f"   {len(segs)} replici; community-1 detecteaza vorbitorii...")
            diar = diarize(m, wav)
            lines, summary = clip_report(clip.stem, segs, diar)
            report += lines
            data.append({"summary": summary, "segments": segs, "diar": diar})
            (out / "raport.txt").write_text("\n".join(report), encoding="utf-8")
            (out / "date.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    finally:
        if old_stop:
            autostop(old_stop)
        shutil.rmtree(tmp, ignore_errors=True)
    log(f"Gata. Raportul: {out / 'raport.txt'}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            log(str(e.code))
        raise
