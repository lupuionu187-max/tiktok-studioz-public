#!/usr/bin/env python3
"""
dublare_ui.py - interfata locala pentru dublare in serie cu VoiceStudio.

Porneste un mic server doar pe calculatorul tau (127.0.0.1) si deschide
pagina in browser. Setarile se salveaza in setari.json, clipurile se iau
din folderul "clipuri", rezultatele merg in "dublate".
"""
import json
import queue
import re
import secrets
import shutil
import socket
import tempfile
import os
import subprocess
import sys
import threading
import time
import types
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import dubleaza as engine
import povestire
import radar as radar_mod
import randare

HERE = Path(__file__).resolve().parent
UI_FILE = HERE / "ui.html"
PORT = 8765
VERSION = "2026-10-05a"  # must match UI_VERSION in ui.html and RADAR_VERSION in radar.html
# pregătire pentru traducerea CapCut de pe telefoane (tiktok_bot\capcut ia clipurile din capcut_de_tradus)
CAPCUT_IN = HERE / "capcut_de_tradus"
CAPCUT_OUT = HERE / "capcut_traduse"
CAPCUT_MAX_S = 240          # „doar finalul” (setarea capcut_pregatire): ce trece de 4:00 se taie de la final, nimic altceva
CAPCUT_MAX_S_VECHI = 270    # „ca înainte”: după ce scot toate pauzele, ce trece de 4:30 se taie de la final
CAPCUT_TAG_FINAL = "capcut_pregatit_v3_doar_final"    # marcajul „doar finalul” (același ca montaj.TAG_FINAL)
CAPCUT_MIN_S = 5.5          # sub 5 s CapCut nu traduce
CAPCUT_FINAL = HERE / "capcut_final"                  # montajele (imaginea + vocea română CapCut)
CAPCUT_DUBLATE = HERE / "capcut_finalizate.json"      # traducerile CapCut trimise deja la randare (nu le trimit de 2 ori)
CAPFIN_OPRIT = "oprit_capcut.txt"   # semn în folderul de lucru: video-ul acestei traduceri CapCut e oprit („Oprește video-urile”)


def capcut_final_clips() -> dict:
    """Traducerile CapCut cu montaj (capcut_final): {nume clip: montaj}. Pe montaj se desenează banda clipului
    (e chiar imaginea care se randează) și benzile lor nu se șterg la pornire (clipurile nu stau în clipuri)."""
    import re as _re
    out = {}
    try:      # cele mai noi întâi: un clip tradus din nou (<nume>_ro_2) = același clip, contează montajul nou
        for r in sorted((CAPCUT_FINAL / "rapoarte").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            m = _re.fullmatch(r"(.+?)_ro(?:_primele_4m59)?(?:_(\d+))?", r.stem)
            f = CAPCUT_FINAL / f"{r.stem}.mp4"
            if m and f.is_file():
                out.setdefault(f"{m.group(1)}.mp4", f)
    except OSError:
        pass
    return out
CAPCUT_JOURNAL = ".pregatire_capcut.json"   # în clipuri\capcut: clipul la care lucrează pregătirea acum


def _capcut_known(stem: str) -> bool:
    """Clipul (după nume) e deja în coada CapCut, tradus sau pus deoparte acolo."""
    from glob import escape as _gesc
    s = _gesc(stem)
    try:
        # de_refacut / arhiva: scoase sau arhivate din coadă de proprietar (fila CapCut) — nu le pregătesc din nou;
        # acolo pot fi și copii cu ora („nume__1792….mp4”)
        return ((CAPCUT_IN / f"{stem}.mp4").exists()
                or any((CAPCUT_IN / d / f"{stem}.mp4").exists()
                       for d in ("gata", "partial", "de_verificat", "respinse", "originale", "de_refacut", "arhiva"))
                or any(any((CAPCUT_IN / d).glob(f"{s}__*.mp4")) for d in ("de_refacut", "arhiva"))
                or any((CAPCUT_IN / "in_lucru").glob(f"*__{s}.mp4")) or any(CAPCUT_OUT.glob(f"{s}_ro*.mp4")))
    except OSError:
        return True


def _capcut_lock(keep: Path):
    """Zăvor pe clipuri\\capcut\\.pregatire_capcut.lock cât lucrează o pregătire: o a doua fereastră a aplicației
    pornită între timp nu-i „repară” clipul la care lucrează. Întoarce zăvorul sau None dacă îl ține alt proces."""
    try:
        import msvcrt
    except ImportError:          # nu e Windows
        return True
    try:
        keep.mkdir(parents=True, exist_ok=True)
        f = open(keep / ".pregatire_capcut.lock", "a+b")
    except OSError:
        return None
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return f
    except OSError:
        f.close()
        return None


def _capcut_unlock(f):
    if f is None or f is True:
        return
    try:
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    try:
        f.close()
    except OSError:
        pass


def capcut_recover(c, held=False) -> list:
    """După o oprire a aplicației în timpul pregătirii pentru CapCut: clipul mutat în clipuri\\capcut, dar nepus încă
    în capcut_de_tradus, se întoarce în clipuri (altfel nu l-ar mai lua nici dublarea, nici CapCut).
    held: zăvorul e deja luat de cine mă cheamă. Întoarce numele puse înapoi."""
    keep = engine.folder(c["folder_clipuri"]) / "capcut"
    j = keep / CAPCUT_JOURNAL
    if not j.is_file():
        return []
    lk = None
    if not held:
        lk = _capcut_lock(keep)
        if lk is None:
            return []                    # altă fereastră a aplicației pregătește acum: jurnalul e al ei
    back = []
    try:
        d = json.loads(j.read_text(encoding="utf-8"))
        orig, moved = keep.parent / Path(d["orig"]).name, keep / Path(d["moved"]).name
        final = Path(d["final"]).name if d.get("final") else ""
        published = bool(final) and _capcut_known(Path(final).stem)
        if final and not published:
            tmp = CAPCUT_IN / (final + ".tmp")
            if tmp.is_file():
                tmp.unlink()                     # copia scrisă pe jumătate de pregătirea întreruptă
        if not published and moved.is_file() and not orig.exists():
            engine._replace_retry(moved, orig)
            back.append(orig.name)
        j.unlink()
    except Exception as e:  # noqa: BLE001
        print("CapCut reluare:", e, flush=True)
    finally:
        if not held:
            _capcut_unlock(lk)
    return back
SPENT_FILE = HERE / "cheltuieli.json"   # day -> dollars spent on rented GPUs
HEARTBEAT_S = 60                        # the pods delete themselves ~25 min after the last heartbeat
POD_FIELDS = ("adresa", "cheie")        # set by the app when it makes a pod; the page never overwrites them
# what a rented row loses when its machine is deleted (the row stays, free for a new GPU)
CLEAR_RENTAL = {"adresa": "", "lucrator": "", "vast_id": "", "furnizor": ""}
KEY_FIELDS = ("runpod_api_key", "anthropic_api_key", "gemini_api_key", "vast_api_key")


class App:
    def __init__(self):
        self.lock = threading.RLock()
        self.settings = engine.load_settings()
        try:     # o pregătire pentru CapCut întreruptă de oprirea aplicației: clipul ei se întoarce în clipuri
            self._capcut_back = capcut_recover(self.settings)
        except Exception:  # noqa: BLE001
            self._capcut_back = []
        # (bands drawn for clips that were dubbed and deleted since are dropped; also writes the copy .bak)
        try:     # benzile desenate pe traducerile CapCut (nu stau în clipuri): nu le șterge curățenia de mai jos
            cc_zones = {k: v for k, v in (self.settings.get("sterge_zone_clip") or {}).items()
                        if k in capcut_final_clips()}
        except Exception:  # noqa: BLE001
            cc_zones = {}
        pruned = engine.prune_clip_zones(self.settings)
        if cc_zones:
            pruned = dict(pruned, sterge_zone_clip=dict(pruned.get("sterge_zone_clip") or {}, **cc_zones))
        self.settings = engine.save_settings(pruned)
        self.failed = {}          # clip name -> error
        self.attempts = {}        # clip name -> times it failed in this session (it gets one more try)
        self.retry_note = {}      # clip name -> its first error: waits at the end of the queue for the 2nd try
        self.stuck = {}           # machine id -> {"dubber", "why", "stage"}: its clip's step stopped moving
        self.stuck_n = {}         # machine id -> clips in a row whose step got stuck on that card
        self.times = {}           # clip name -> seconds
        self.results = {}         # clip name -> {"translator", "qc"} from the last dub
        self.idle_since = None    # auto-stop: when the rented GPUs ran out of work
        self.jobs = {}            # machine id -> {"clip", "stage", "percent", "started", "dubber"}
        self.mstate = {}          # machine id -> {"online", "error", "voices"}
        self.workers = set()      # machine ids with a live worker thread
        self.stop_after = False
        self.logs = []
        self.agent = {"found": None, "version": None}
        self.run_done = 0
        self.alert = engine.SETTINGS_NOTE["msg"] or None     # setari.json was broken: started from the copy
        self.wake = threading.Event()
        self.machine_errors = {}  # machine id -> consecutive connection failures
        self.machine_stop = set() # machine ids being stopped from the page (not a global cancel)
        self.pods = {}            # machine id -> {"status", "cost", "busy", "msg"}
        self.installs = {}        # machine id -> {"busy": bool, "msg": str, "ok": bool|None}
        self.renders = {}         # clip name -> {"percent", "running"}: MP4 waiting for / in ffmpeg
        self.render_cancel = {}   # clip name -> (job, Event): video-ul care se face acum pe PC („Oprește video-urile”)
        self.render_q = queue.Queue()
        self.render_info = {"encoder": None}
        self.no_worker = {}       # machine id -> when its pod was found without the video worker
        self.bg = {}              # machine id -> (thread, dubber): video made in the background on that GPU
        self.prefetch = {}        # machine id -> {"clip", "dubber"}: next clip prepared while this one gets its voice
        self.dead = set()         # machine ids whose pod died / was stopped mid-clip (their worker must let go)
        self.rq_pods = {}         # machine id -> {"clip", "cancel"}: a PC-queue video being made on that pod
        self.local_q = queue.Queue()  # videos this PC makes itself (ffmpeg), one at a time
        self.rq_free = threading.Event()  # a GPU finished a PC-queue video: hand out the next one now
        self.finishing = False    # "Termin pe azi": finish what started, then delete the rented GPUs
        self.log_seq = 0          # number of log lines ever written (the page asks only for new ones)
        self._gata_logged = None  # run_done value of the last "Gata." line (no duplicates)
        self.session_done = 0     # clips finished since the app started (cost per clip)
        self.spent_session = 0.0  # dollars on rented GPUs since the app started
        self.spent_days = self._load_spent()
        self._spent_saved = 0.0
        self.cost_tick = {}       # machine id -> last time its running cost was counted
        self.pod_idle_since = {}  # machine id -> since when its running pod has nothing to do
        self.http404 = {}         # machine id -> consecutive HTTP 404 from the RunPod proxy
        self.gone = {}            # machine id -> consecutive "pod not found" answers from RunPod
        self._hb_busy = set()
        self._nv_seen = set()          # (row, rental) whose GPU video result is already remembered
        self._scan_cache = None   # (key, time, rows, ignored): folder scan reused for ~2 s
        self._rev, self._rev_obj = 0, None
        self._init_rental_state()
        self.ollama = {"ok": False, "models": [], "installed": False}
        self.ollama_pull = {"busy": False, "msg": ""}
        threading.Thread(target=self._watch_ollama, daemon=True).start()
        self.ai = {"checked": False, "torch": False, "cuda": False, "gpu": "", "model": False,
                   "busy": False, "msg": ""}
        threading.Thread(target=self._ai_check, daemon=True).start()
        self.tool = {"busy": False, "clip": None, "percent": None, "msg": "", "done": 0, "total": 0,
                     "stop": threading.Event()}
        self.cover = {"busy": False, "clip": None, "percent": None, "msg": "", "stop": threading.Event()}
        self.capcut = {"busy": False, "clip": None, "percent": None, "msg": "", "done": 0, "total": 0,
                       "stop": threading.Event()}
        self._capcut_new = (0.0, 0)          # (când am numărat, câte clipuri noi) — numărat cel mult o dată la 5 s
        self._capcut_short = set()           # (nume, mărime) clipuri prea scurte pentru CapCut: nu le mai număr ca noi
        self.capfin = {"busy": False, "msg": "", "done": 0, "total": 0, "stop": threading.Event()}
        self._capfin_new = (0.0, [])          # (când am numărat, clipurile CapCut de finalizat)
        # „Film → povestire”: filmele date în pagină, una câte una în fundal (povestire.py, folderul povestiri)
        self.film = povestire.Povestiri(log=self.log, settings=self.snapshot, game_running=self._game_running)
        self._threads = {}        # name -> (thread, target): the loops that must never stop (_ensure_threads)
        self._threads_checked = 0.0
        self._spawn("video", self._render_loop)
        self._spawn("video pe PC", self._local_render_loop)
        self._spawn("verificarea placilor", self._watch_machines)
        self._spawn("semnalul catre GPU-uri", self._heartbeat_loop)
        # clips whose voice was done when the app stopped: only their video is made now
        threading.Thread(target=self._startup_resume, daemon=True).start()

    _THREADS_LOCK = threading.Lock()

    def _spawn(self, name, target):
        t = threading.Thread(target=target, daemon=True, name=name)
        self._threads[name] = (t, target)
        t.start()

    def _ensure_threads(self):
        """The loops that must never stop (the watcher keeps an eye on the rented GPUs' money, the render
        loops hand out the videos, the heartbeat keeps the pods alive): one that died anyway of an unexpected
        error is started again, and it says so in the log."""
        restarted = []
        with self._THREADS_LOCK:
            now = time.time()
            if now - self._threads_checked < 10:
                return
            self._threads_checked = now
            for name, (t, target) in list(self._threads.items()):
                if not t.is_alive():
                    self._spawn(name, target)
                    restarted.append(name)
        for name in restarted:
            self.log(f"Am repornit „{name}” (se oprise din cauza unei erori neprevazute).")

    def _startup_resume(self):
        time.sleep(2)
        if getattr(self, "_capcut_back", None):
            self.log("CapCut pregătire: am pus înapoi în clipuri " + ", ".join(self._capcut_back)
                     + " (aplicația s-a oprit în timpul pregătirii)")
        try:
            self._resume_renders()
            with self.lock:
                idle = not self.workers
            if idle:
                engine.cleanup_work(self.snapshot())    # half-done folders of clips cut short by the stop
        except Exception as e:  # noqa: BLE001
            print("reluare:", e, flush=True)

    def _resume_renders(self, c=None, only=None):
        """Clips whose voice is done (render.json in their working folder) and whose video isn't made: into the
        video queue (a rented GPU or this PC), instead of being dubbed again from the start. only: just these."""
        c = c or self.snapshot()
        try:
            found = engine.pending_renders(c)
        except Exception as e:  # noqa: BLE001
            print("reluare video:", e, flush=True)
            return []
        names = []
        with self.lock:
            busy = self._busy_clips()
            for name, job, t_dub, info in found:
                if (only is not None and name not in only) or name in busy or name in self.failed:
                    continue
                if self._capfin_paused(job):          # oprit cu „Oprește video-urile CapCut”: așteaptă „Pornește iar”
                    continue
                try:      # (checked again here: its video may have been finished since the scan)
                    still = (not Path(job["out"]).exists() and (Path(job["work"]) / engine.RENDER_FILE).is_file()
                             and Path(job["video"]).is_file() and Path(job["audio"]).is_file())
                except (OSError, KeyError, TypeError):
                    still = False
                if not still:
                    continue
                self.renders[name] = {"percent": None, "running": False}
                if info:
                    self.results.setdefault(name, info)
                self.render_q.put((name, job, t_dub))
                names.append(name)
        if names:
            self.log(f"Vocea era deja gata pentru {len(names)} clip(uri); fac doar video-ul: "
                     + ", ".join(names[:6]) + (" ..." if len(names) > 6 else ""))
        return names

    # timings of the checks on newly rented machines (class attributes: the tests shorten them)
    START_HEALTHY_S = {"community": 10 * 60, "secure": 6 * 60, "vast": 15 * 60}  # container up -> VoiceStudio OK
    START_MAX_S = 25 * 60         # created -> VoiceStudio OK, whatever happens (image pull included)
    PULL_ALLOW_S = 8 * 60         # RunPod can't say when the container is up: assume it is by then
    ERR5XX_S = 3 * 60             # VoiceStudio itself answers 5xx (not the proxy) for this long = broken
    TRANSCRIBE_STUCK_S = 8 * 60   # first clip's transcription without any progress for this long = broken
    START_POLL_S = 10
    MAX_REPLACE = 3               # broken at start -> new machine, at most this many times per row
    MAX_DEATH_REPLACE = 5         # Vast machine died mid-run -> new one, at most this many times per row
    VAST_RETRY_S = 120            # no Vast offer right now: try again after this long
    VAST_UNREACH_S = 10 * 60      # a running Vast machine that doesn't answer for this long is dead
    VAST_GONE_S = 150             # a new Vast machine "not found" for this long = it really is gone
    VAST_OFFLINE_POLLS = 3        # Vast says "offline" this many polls in a row (~45 s) = dead

    def _init_rental_state(self):
        self.replaced = {}        # machine id -> new machines taken because one started broken
        self.death_repl = {}      # machine id -> new Vast machines taken because one died mid-run
        self.want_replace = {}    # machine id -> {"next", "why", "tries"}: a dead Vast GPU to replace
        self.resume_on_ready = set()  # machine ids whose replacement should restart the queue
        self.fresh = {}           # machine id -> created at: new machine, first transcription not done
        self.unreach = {}         # machine id -> since when a Vast machine doesn't answer
        self.offline = {}         # machine id -> consecutive "offline" answers from Vast
        self.dropped = {}         # machine id -> [(clip, when)]: links broken mid-step in a row while it answers
        self.link_errs = []       # (when, machine id): every broken link to a rented card (this PC's internet?)
        self.excused = {}         # machine id -> when its drops were last put down to this PC's internet

    # ---------------------------------------------------------- rented machines: provider helpers
    def _key(self, prov, c=None):
        c = c or self.settings
        return str(c.get("vast_api_key" if prov == "vast" else "runpod_api_key") or "")

    def _api(self, prov, c=None):
        key = self._key(prov, c)
        return engine.Vast(key) if prov == "vast" else engine.RunPod(key)

    def _prov_of(self, m, c=None):
        """The provider a row's machine is (or will be) rented from."""
        info = engine.rented_info(m)
        if info:
            return info["provider"]
        p = self.pods.get((m or {}).get("id"), {})
        if p.get("busy") and p.get("prov"):
            return p["prov"]
        return (c or self.settings).get("gpu_furnizor") or "runpod"

    def _any_key(self, c=None):
        c = c or self.settings
        return bool(c.get("runpod_api_key") or c.get("vast_api_key"))

    def _row(self, mid):
        with self.lock:
            return next((dict(x) for x in self.settings["masini"] if x["id"] == mid), None)

    # ---------------------------------------------------------- logging
    def log(self, msg, machine=None):
        prefix = ""
        if machine and len(self.active_machines()) > 1:
            prefix = f"[{machine['nume']}] "
        line = time.strftime("[%H:%M:%S] ") + prefix + msg
        print(line, flush=True)
        with self.lock:
            self.logs.append(line)
            del self.logs[:-500]
            self.log_seq += 1
        # the whole log also on disk (jurnal.log next to the app), to find out afterwards what happened
        try:
            p = Path(__file__).resolve().parent / "jurnal.log"
            if p.exists() and p.stat().st_size > 5_000_000:
                p.replace(p.with_suffix(".vechi.log"))
            with open(p, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d ") + line + "\n")
        except OSError:
            pass

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.settings))

    def logs_since(self, since):
        """(lines, seq, reset): only the lines the page has not seen yet; everything (last 250)
        when it is too far behind or the app was restarted."""
        with self.lock:
            seq, n = self.log_seq, len(self.logs)
            if since is None or since < 0 or since > seq or seq - since > n:
                return self.logs[-250:], seq, True
            return (self.logs[n - (seq - since):] if seq > since else []), seq, False

    def settings_rev(self):
        """Changes every time self.settings is replaced (it is never changed in place)."""
        with self.lock:
            if self.settings is not self._rev_obj:
                self._rev_obj = self.settings
                self._rev += 1
            return self._rev

    def public_settings(self):
        """The settings for the page: keys are never sent back."""
        with self.lock:
            settings = json.loads(json.dumps(self.settings))
        for m in settings["masini"]:
            m["cheie"] = "********" if m["cheie"] else ""
        for k in KEY_FIELDS:
            settings[k] = "********" if settings.get(k) else ""
        return settings

    def _pod_managed(self, m):
        """Row whose address/key belong to a machine the app rented (or is renting). Without the
        provider's API key the app makes no machines: those addresses are typed by hand and stay editable."""
        if self.pods.get(m["id"], {}).get("busy"):
            return True
        info = engine.rented_info(m)
        return bool(info) and bool(self._key(info["provider"]))

    def save_from_page(self, data):
        """Settings changed on the page. Only the keys it sends change. "masini" holds only
        the edited rows (id + the changed fields); rows the page does not send stay as they
        are, and the address/key of a pod made by the app are never taken from the page
        (it may still show the old values for a moment)."""
        with self.lock:
            data = dict(data)
            for k in KEY_FIELDS:
                if k in data and (data[k] == "********" or not str(data[k] or "").strip()):
                    data.pop(k)          # the page only shows "********": keep the real key
            rows = data.pop("masini", None)
            masini = [dict(m) for m in self.settings["masini"]]
            if isinstance(rows, list):
                byid = {m["id"]: m for m in masini}
                for r in rows:
                    if not isinstance(r, dict) or not r.get("id"):
                        continue
                    cur = byid.get(str(r["id"]))
                    if cur is None:
                        # new row typed by hand on the page (no RunPod key)
                        if len([m for m in masini if m["id"] != "local"]) >= engine.MAX_MACHINES:
                            continue
                        new = {"id": str(r["id"]), "nume": str(r.get("nume") or ""),
                               "adresa": str(r.get("adresa") or ""), "activ": bool(r.get("activ", False)),
                               "cheie": "" if r.get("cheie") == "********" else str(r.get("cheie") or ""),
                               "managed": True}
                        masini.append(new)
                        byid[new["id"]] = new
                        continue
                    managed = self._pod_managed(cur)
                    for k in ("nume", "activ", "adresa", "cheie"):
                        if k not in r:
                            continue
                        v = r[k]
                        if k in POD_FIELDS and managed:
                            continue
                        if k == "cheie" and (v == "********" or not str(v or "").strip()):
                            continue     # masked or empty: keep the saved key
                        cur[k] = bool(v) if k == "activ" else str(v or "")
            self.settings = engine.save_settings({**self.settings, **data, "masini": masini})

    def active_machines(self):
        with self.lock:
            sursa = self.settings["sursa"]
            return [m for m in self.settings["masini"]
                    if m["activ"] and m["adresa"] and engine.machine_allowed(m, sursa)]

    @property
    def running(self):
        with self.lock:
            return bool(self.workers)

    # ---------------------------------------------------------- machine status
    def _watch_ollama(self):
        while True:
            c = self.snapshot()
            st = engine.ollama_status(c["ollama_adresa"])
            with self.lock:
                self.ollama = st
            time.sleep(10 if c["traducator"] == "local" or c["rezerva_sursa"] == "local" else 30)

    def pull_model(self, model):
        with self.lock:
            if self.ollama_pull["busy"]:
                return False
            self.ollama_pull = {"busy": True, "msg": "Pornesc descarcarea..."}

        def run():
            def progress(msg):
                with self.lock:
                    self.ollama_pull["msg"] = msg
            try:
                engine.ollama_pull(self.snapshot()["ollama_adresa"], model, progress)
                with self.lock:
                    self.ollama_pull = {"busy": False, "msg": f"Modelul {model} e descarcat si gata."}
                    self.ollama = engine.ollama_status(self.snapshot()["ollama_adresa"])
                self.log(f"Modelul local {model} e descarcat.")
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.ollama_pull = {"busy": False, "msg": f"Nu a mers: {e}"}
        threading.Thread(target=run, daemon=True).start()
        return True

    # ---------------------------------------------------------- AI subtitle removal (sterge_ai.py)
    def _ai_check(self):
        import subprocess as _sp
        try:
            r = _sp.run([sys.executable, str(randare.AI_SCRIPT), "status"], capture_output=True, timeout=180,
                        **randare._no_window())
            st = json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except Exception as e:  # noqa: BLE001
            st = {"err": str(e)[:200]}
        with self.lock:
            self.ai.update(checked=True, torch=st.get("torch") or False, cuda=bool(st.get("cuda")),
                           gpu=st.get("gpu") or "", model=bool(st.get("model")))

    def install_ai(self):
        with self.lock:
            if self.ai["busy"]:
                return False
            self.ai.update(busy=True, msg="Pornesc instalarea...")

        def run():
            import subprocess as _sp
            ok = False
            try:
                p = _sp.Popen([sys.executable, str(randare.AI_SCRIPT), "install"], stdout=_sp.PIPE,
                              stderr=_sp.STDOUT, **randare._no_window())
                for raw in p.stdout:
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("INFO "):
                        self.log("Stergere AI: " + line[5:])
                        with self.lock:
                            self.ai["msg"] = line[5:]
                    elif line.startswith("PROGRES "):
                        with self.lock:
                            self.ai["msg"] = f"descarc modelul... {line.split()[1]}%"
                    elif line.startswith("EROARE "):
                        self.log("Stergere AI: EROARE " + line[7:])
                        with self.lock:
                            self.ai["msg"] = "Nu a mers: " + line[7:]
                p.wait()
                ok = p.returncode == 0
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.ai["msg"] = f"Nu a mers: {e}"
            self._ai_check()
            with self.lock:
                a = self.ai
                if ok and a["torch"] and a["model"]:
                    a["msg"] = (f"Instalat. Rulează pe {a['gpu']}." if a["cuda"]
                                else "Instalat, dar nu vede placa video: merge foarte încet.")
                a["busy"] = False
            self.log("Stergere AI: " + self.ai["msg"])
        threading.Thread(target=run, daemon=True).start()
        return True

    def _watch_machines(self):
        try:
            path = engine.find_claude()
            with self.lock:
                self.agent = {"found": bool(path), "version": engine.claude_version() if path else None}
        except Exception as e:  # noqa: BLE001
            print("claude:", e, flush=True)
        while True:
            try:
                self._watch_once()
            except Exception as e:  # noqa: BLE001 - never kill the watcher: it keeps the money checks running
                print("verificarea placilor:", e, flush=True)
            self.wake.wait(5)
            self.wake.clear()

    def _machine_state(self, c, m):
        if not m["adresa"]:
            return {"online": False, "error": "lipseste adresa", "voices": []}
        d = engine.Dubber(c, machine=m)
        err = d.check_access()
        voices = []
        if err is None:
            try:
                voices = d.voices()
            except Exception:  # noqa: BLE001
                pass
        self._note_404(c, m, err)
        self._note_unreachable(c, m, err)
        return {"online": err is None, "error": err, "voices": voices}

    def _watch_once(self):
        c = self.snapshot()
        seen = {m["id"] for m in c["masini"]}

        def check(m):
            # each machine on its own: one that doesn't answer (20-35 s of timeouts) doesn't hold the others,
            # and one that throws doesn't stop the checks below (auto-stop, "Termin pe azi", replacements)
            try:
                st = self._machine_state(c, m)
            except Exception as e:  # noqa: BLE001
                print("placa", m.get("nume"), e, flush=True)
                return
            with self.lock:
                if m["id"] in {x["id"] for x in self.settings["masini"]}:
                    self.mstate[m["id"]] = st
        # (daemon threads, not a pool: closing the app never waits for a machine that doesn't answer)
        ths = [threading.Thread(target=check, args=(m,), daemon=True) for m in c["masini"]]
        for t in ths:
            t.start()
        end = time.time() + 90
        for t in ths:
            t.join(max(0.1, end - time.time()))
        with self.lock:
            for mid in list(self.mstate):
                if mid not in seen:
                    self.mstate.pop(mid, None)
        try:
            self._refresh_pods(c)
        except Exception as e:  # noqa: BLE001 - never kill the watcher
            print("pods:", e, flush=True)
        for fn in (self._auto_stop_check, self._finish_check, self._stuck_check, self._replace_tick,
                   self._steal_local):
            try:
                fn(c)
            except Exception as e:  # noqa: BLE001 - never kill the watcher
                print(fn.__name__, e, flush=True)
        try:
            self.idle_info()
        except Exception as e:  # noqa: BLE001
            print("idle_info", e, flush=True)
        self._ensure_threads()

    def _note_404(self, c, m, err):
        """The RunPod proxy answers 404 for a pod that no longer exists. Several in a row
        (not 524 / timeouts: those are a busy pod) = the pod is gone."""
        mid = m["id"]
        info = engine.rented_info(m)
        if mid == "local" or not info or info["provider"] != "runpod":
            return                       # (Vast.ai machines: _note_unreachable)
        with self.lock:
            if self.pods.get(mid, {}).get("busy"):
                self.http404[mid] = 0
                return
            n = self.http404[mid] = (self.http404.get(mid, 0) + 1) if "HTTP 404" in str(err or "") else 0
        if n < 3:
            return
        status, known = None, False
        if c.get("runpod_api_key"):
            try:
                status = engine.RunPod(c["runpod_api_key"]).status(engine.runpod_pod_id(m["adresa"])).get("status")
                known = True
            except Exception:  # noqa: BLE001 - can't ask RunPod now: try again at the next 404
                return
            if status == "RUNNING" and n < 12:
                return               # RunPod still says RUNNING: give it about a minute more
        with self.lock:
            self.http404[mid] = 0
        # the address is forgotten only when RunPod confirms the pod is gone (an existing pod
        # must stay reachable for "Opreste", or it would bill with nobody to delete it)
        self._pod_dead(mid, "pod-ul nu raspunde (RunPod da 404)", clear_address=known and status in (None, "TERMINATED"))

    def _note_unreachable(self, c, m, err):
        """A Vast.ai machine is reached on its raw IP:port (no proxy, no 404s): when it doesn't
        answer for a long time while Vast still says "running", it is dead all the same."""
        mid = m["id"]
        info = engine.rented_info(m)
        if mid == "local" or not info or info["provider"] != "vast" or not m.get("adresa"):
            self.unreach.pop(mid, None)
            return
        with self.lock:
            if self.pods.get(mid, {}).get("busy") or not err or "nu raspunde" not in str(err):
                self.unreach.pop(mid, None)
                return
            since = self.unreach.setdefault(mid, time.time())
        if time.time() - since >= self.VAST_UNREACH_S:
            with self.lock:
                self.unreach.pop(mid, None)
            self._vast_dead(mid, f"nu mai răspunde de {self.VAST_UNREACH_S // 60} minute ({err})")

    # ---------------------------------------------------------- heartbeat to the pods
    def _heartbeat_loop(self):
        """The pods delete themselves ~25 min after the last heartbeat (when idle), so they
        don't keep billing if this app is closed. One small request per pod every minute."""
        time.sleep(10)
        while True:
            try:
                with self.lock:
                    pods = [dict(m) for m in self.settings["masini"]
                            if m["id"] != "local" and engine.rented_info(m) and engine.worker_url(m)
                            and m["id"] not in self._hb_busy]
                    self._hb_busy.update(m["id"] for m in pods)
                for m in pods:
                    threading.Thread(target=self._heartbeat, args=(m,), daemon=True).start()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._ensure_threads()       # (the watcher itself, if it ever died)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(HEARTBEAT_S)

    def _heartbeat(self, m):
        try:
            r = engine.RemoteVideo(m)._req("GET", "/hb", tries=1, timeout=10)
            try:
                st = r.json()
            except ValueError:
                st = {}
            self._note_nvenc(m, st.get("ffmpeg") if isinstance(st, dict) else "")
        except Exception:  # noqa: BLE001 - silent: a busy / older pod simply misses one beat
            pass
        finally:
            with self.lock:
                self._hb_busy.discard(m["id"])

    def _note_nvenc(self, m, note):
        """Remember whether this Vast machine encodes video on its GPU (the next rentals prefer the ones
        that do: much faster video than on its processor)."""
        ok = engine.nvenc_from_note(note or "")
        info = engine.rented_info(m)
        if ok is None or not info or info["provider"] != "vast":
            return
        with self.lock:
            pod = self.pods.get(m["id"], {})
            mach, gpu = pod.get("machine_id"), pod.get("gpu")
            key = (m["id"], str(info["id"]))
            seen = self.__dict__.setdefault("_nv_seen", set())
            if mach is None or key in seen:
                return
            seen.add(key)
        engine.remember_nvenc(mach, ok, gpu)

    def emergency_stop_pods(self, limit=4.0):
        """The console window is being closed: delete every rented pod, quickly (best effort)."""
        c = self.snapshot()
        pods = [(m["id"], info["provider"], info["id"]) for m in c["masini"]
                for info in [engine.rented_info(m)] if m["id"] != "local" and info and self._key(info["provider"], c)]
        if not pods:
            return
        print(f"Inchid: sterg {len(pods)} GPU-uri inchiriate ca sa nu se mai plateasca...", flush=True)
        done = []

        def kill(mid, prov, pid):
            try:
                self._api(prov, c).terminate(pid)
                done.append(mid)
            except Exception:  # noqa: BLE001
                pass
        ts = [threading.Thread(target=kill, args=p, daemon=True) for p in pods]
        for t in ts:
            t.start()
        end = time.time() + limit
        for t in ts:
            t.join(max(0.0, end - time.time()))
        try:
            with self.lock:
                masini = [dict(m, **CLEAR_RENTAL, activ=False) if m["id"] in done else m
                          for m in self.settings["masini"]]
                self.settings = engine.save_settings({**self.settings, "masini": masini})
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------- money spent on rented GPUs
    def _load_spent(self):
        try:
            d = json.loads(SPENT_FILE.read_text(encoding="utf-8"))
            return {str(k): float(v) for k, v in d.items()} if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001 - missing / broken file: start from zero
            return {}

    def _save_spent(self, force=False):
        with self.lock:
            if not force and time.time() - self._spent_saved < 30:
                return
            self._spent_saved = time.time()
            days = dict(sorted(self.spent_days.items())[-90:])
        try:
            tmp = SPENT_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(days, indent=1), encoding="utf-8")
            tmp.replace(SPENT_FILE)
        except OSError:
            pass

    def _accrue(self, mid, st, err):
        """Running time x price per hour, counted at every status poll of the pod."""
        if err:
            return                       # no answer: count the gap at the next good poll
        now = time.time()
        with self.lock:
            prev = self.cost_tick.get(mid)
            try:
                cost = float(st.get("cost") or 0)
            except (TypeError, ValueError):
                cost = 0.0
            if st.get("status") == "RUNNING" and cost > 0:
                if prev:
                    usd = cost * min(now - prev, 300) / 3600
                    self.spent_session += usd
                    day = time.strftime("%Y-%m-%d")
                    self.spent_days[day] = self.spent_days.get(day, 0.0) + usd
                self.cost_tick[mid] = now
            else:
                self.cost_tick.pop(mid, None)
        self._save_spent()

    def _count_done(self):
        """One more clip finished (under self.lock)."""
        self.run_done += 1
        self.session_done += 1
        self._scan_cache = None

    def idle_info(self):
        """(running pods with nothing to do, minutes they have been idle - the shortest)."""
        now = time.time()
        idle = []
        with self.lock:
            for m in self.settings["masini"]:
                mid = m["id"]
                p = self.pods.get(mid, {})
                if mid == "local" or not engine.rented_info(m) or p.get("status") != "RUNNING" \
                        or p.get("busy"):
                    self.pod_idle_since.pop(mid, None)
                    continue
                bgv = self.bg.get(mid)
                busy = (mid in self.jobs or mid in self.prefetch or mid in self.rq_pods
                        or (bgv and bgv[0].is_alive())
                        or any(r.get("remote") == m["nume"] for r in self.renders.values()))
                if busy:
                    self.pod_idle_since.pop(mid, None)
                    continue
                idle.append(now - self.pod_idle_since.setdefault(mid, now))
        return len(idle), (min(idle) / 60 if idle else 0.0)

    # ---------------------------------------------------------- RunPod pods
    _last_pod_poll = 0.0

    VAST_SWEEP_S = 120            # how often the Vast account is checked for machines without a row
    VAST_ORPHAN_S = 240           # a "dublare-" machine without a row for this long is deleted

    def _vast_sweep(self, c):
        """Vast machines this app rented ("dublare-..." label) that no row knows any more - e.g.
        rented while Vast's list still lagged, so the app thought they were gone - are deleted:
        they would bill for nothing. Only after VAST_ORPHAN_S, and never while a row is renting."""
        now = time.time()
        if not self._key("vast", c) or now - getattr(self, "_vast_swept", 0) < self.VAST_SWEEP_S:
            return
        self._vast_swept = now
        try:
            api = engine.Vast(self._key("vast", c))
            mine = api.list_mine()
        except Exception:  # noqa: BLE001 - try again next time
            return
        with self.lock:
            known = {str(m.get("vast_id")) for m in self.settings["masini"] if m.get("vast_id")}
            creating = any(p.get("busy") and p.get("op") == "start" for p in self.pods.values())
            seen = self.__dict__.setdefault("_orphan_seen", {})
        ids = set()
        for inst in mine:
            if not inst["label"].startswith("dublare-") or inst["id"] in known:
                continue
            ids.add(inst["id"])
            first = seen.setdefault(inst["id"], now)
            if now - first < self.VAST_ORPHAN_S or creating:
                continue
            try:
                api.terminate(inst["id"])
                self.log(f"Am șters o mașină Vast.ai rămasă fără rând în listă (id {inst['id']}), "
                         "ca să nu plătești pentru ea degeaba.")
                seen.pop(inst["id"], None)
            except Exception as e:  # noqa: BLE001
                self.log(f"Nu am putut șterge mașina Vast.ai {inst['id']} rămasă fără rând ({e}); "
                         "șterge-o tu de pe cloud.vast.ai.")
        for k in list(seen):
            if k not in ids:
                seen.pop(k, None)

    def _refresh_pods(self, c, force=False):
        if not self._any_key(c) or (not force and time.time() - self._last_pod_poll < 15):
            return
        self._last_pod_poll = time.time()
        self._vast_sweep(c)
        apis = {}
        for m in c["masini"]:
            info = engine.rented_info(m)
            if not info or not self._key(info["provider"], c):
                continue
            prov, pid = info["provider"], info["id"]
            if prov not in apis:
                apis[prov] = self._api(prov, c)
            try:
                st = apis[prov].status(pid)
                err = None
            except Exception as e:  # noqa: BLE001
                st, err = {"status": None, "cost": None}, str(e)
            # which Vast host this rental runs on (also after a restart of the app): its video-on-GPU result
            # is remembered per host (gpu_nvenc.json)
            host = st.get("machine_id") if prov == "vast" and isinstance(st, dict) else None
            card = st.get("gpu") if prov == "vast" and isinstance(st, dict) else None
            st = {k: st.get(k) for k in ("status", "cost") if k in st}
            with self.lock:
                cur = self.pods.setdefault(m["id"], {})
                busy = cur.get("busy")
                if err and cur.get("status"):
                    st = {}              # no answer: keep the last known status
                cur.update(st)
                cur["error"] = err
                if host is not None:
                    cur["machine_id"] = host
                    if card and not cur.get("gpu"):
                        cur["gpu"] = card
                # the provider answered, but without the machine: deleted (twice in a row, to be sure)
                gone = self.gone[m["id"]] = (self.gone.get(m["id"], 0) + 1) \
                    if not err and not st.get("status") else 0
                off = self.offline[m["id"]] = (self.offline.get(m["id"], 0) + 1) \
                    if st.get("status") == "OFFLINE" else 0
            self._accrue(m["id"], st, err)
            if busy:
                continue
            status = st.get("status")
            if prov == "vast":
                if status == "TERMINATED" or gone >= 4:     # Vast's list lags: 4 polls (~1 min)
                    self._vast_dead(m["id"], "mașina a fost ștearsă pe Vast.ai", gone=True)
                elif status in ("EXITED", "ERROR"):
                    self._vast_dead(m["id"], "mașina s-a oprit pe Vast.ai")
                elif status == "OFFLINE" and off >= self.VAST_OFFLINE_POLLS:
                    self._vast_dead(m["id"], "gazda Vast.ai a picat (offline)")
            elif status == "TERMINATED" or gone >= 2:
                self._pod_dead(m["id"], "pod-ul a fost sters pe RunPod", clear_address=True)
            elif status == "EXITED":
                self._pod_dead(m["id"], "pod-ul s-a oprit pe RunPod")

    def _vast_dead(self, mid, reason, gone=False):
        """A Vast.ai machine died: its clips go back to the queue, the instance is destroyed (a
        stopped one still bills its disk; it never comes back by itself in time) and, while the
        queue still has clips, another one is rented in its place (never on RunPod)."""
        m = self._row(mid)
        info = engine.rented_info(m)
        if not info or info["provider"] != "vast":
            return
        self._pod_dead(mid, reason, clear_address=True, replace=True)
        if not gone:
            def destroy():
                try:
                    engine.Vast(self._key("vast")).terminate(info["id"])
                except Exception as e:  # noqa: BLE001
                    self.log(f"{m['nume']}: nu am putut șterge mașina veche de pe Vast.ai ({e}); "
                             "verific-o pe console.vast.ai.")
            threading.Thread(target=destroy, daemon=True).start()

    def _update_machine(self, mid, **fields):
        with self.lock:
            masini = [dict(m, **fields) if m["id"] == mid else m for m in self.settings["masini"]]
            self.settings = engine.save_settings({**self.settings, "masini": masini})
            return next((m for m in self.settings["masini"] if m["id"] == mid), None)

    def _set_active(self, mid, active):
        self._update_machine(mid, activ=active)

    def _pod_dead(self, mid, reason, clear_address=False, replace=False):
        """A pod died, was deleted or stopped (definitive: RunPod says so, or 404s from its
        proxy, or the user stopped it): stop sending it work and give its clips back now,
        instead of waiting minutes for timeouts. replace: a Vast.ai machine that died while the
        queue runs gets a replacement (see _schedule_replace)."""
        with self.lock:
            m = next((x for x in self.settings["masini"] if x["id"] == mid), None)
            if m is None or mid == "local":
                return
            job, pre = self.jobs.get(mid), self.prefetch.get(mid)
            bgv, rq = self.bg.get(mid), self.rq_pods.get(mid)
            bg_alive = bool(bgv and bgv[0].is_alive())
            had_work = bool(job or pre or bg_alive or rq)
            if not had_work and not m["activ"] and not (clear_address and (m["adresa"] or engine.rented_info(m))):
                return                   # nothing to do (already handled)
            was_running = mid in self.workers
            if had_work or was_running:
                self.dead.add(mid)
            if clear_address:
                self.cost_tick.pop(mid, None)
            self.fresh.pop(mid, None)
            self.dropped.pop(mid, None)          # (the next machine in this row starts with a clean slate)
            self.excused.pop(mid, None)
            in_run = bool(had_work or was_running or self.workers)
        fields = {"activ": False}
        if clear_address:
            fields.update(CLEAR_RENTAL)
        self._update_machine(mid, **fields)
        self.log(f"{m['nume']}: {reason}." + (" Clipurile lui trec pe celelalte placi." if had_work else ""))
        if replace and in_run:
            self._schedule_replace(mid, reason)
        for d in [x["dubber"] for x in (job, pre) if x and x.get("dubber")]:
            threading.Thread(target=d.abort, daemon=True).start()
        if bg_alive:
            bgv[1].cancel.set()          # its video goes to the PC queue (another GPU or this PC)
        if rq:
            rq["cancel"].set()
        if job or pre:
            threading.Thread(target=self._release_dead, args=(mid, job, pre, was_running), daemon=True).start()
        self.wake.set()

    def _release_dead(self, mid, job, pre, was_running, grace=10):
        """Give the worker of a dead pod a few seconds to let go of its clips by itself (so two
        workers never use the same work folder), then take them away from it anyway."""
        end = time.time() + grace
        while time.time() < end:
            with self.lock:
                if ((job is None or self.jobs.get(mid) is not job)
                        and (pre is None or self.prefetch.get(mid) is not pre)):
                    break
            time.sleep(0.25)
        with self.lock:
            if job is not None and self.jobs.get(mid) is job:
                self.jobs.pop(mid, None)
            if pre is not None and self.prefetch.get(mid) is pre:
                self.prefetch.pop(mid, None)
            go_on = (bool(self.workers) or was_running) and not self.stop_after
        self.wake.set()
        if go_on:
            self.start(resume=True)      # boards that had already emptied the queue take the clips back

    def pod_action(self, mid, action, prov=None):
        """Start / stop (delete) the rented machine of a row. prov: rent from this provider
        (automatic replacements); otherwise the row's own provider, or the one chosen in the settings."""
        c = self.snapshot()
        m = next((x for x in c["masini"] if x["id"] == mid), None)
        if not m or m["id"] == "local":
            return "placa asta nu e un GPU inchiriat"
        prov = prov or self._prov_of(m, c)
        if not self._key(prov, c):
            return f"pune intai cheia de API {engine.PROVIDER_NAME[prov]}"
        if action == "stop" and not engine.rented_info(m):
            with self.lock:
                p = self.pods.get(mid, {})
                if not (p.get("busy") and p.get("op") == "start"):
                    return "nu e niciun pod pornit aici"
        with self.lock:
            p = self.pods.setdefault(mid, {})
            if p.get("busy"):
                if action == "stop" and p.get("op") == "start":
                    p["cancel_start"] = True      # stop always wins over a start in progress
                else:
                    return "se lucreaza deja la placa asta"
            else:
                p["cancel_start"] = False
            p["busy"], p["op"] = True, action
            if action == "start":
                p["prov"] = prov
            else:
                # the user (or auto-stop / "Termin pe azi") stops it: no automatic replacement
                self.want_replace.pop(mid, None)
                self.resume_on_ready.discard(mid)
            p["msg"] = "Sterg pod-ul..." if action == "stop" else "Pornesc..."
        threading.Thread(target=self._pod_action, args=(mid, action), daemon=True).start()
        return None

    def _pod_action(self, mid, action, broken=None):
        """The work of pod_action (in its own thread; pods[mid]["busy"] is already set).
        broken=(info, why): the row's machine started broken: delete it and take another.
        Returns True when the machine is (re)started and ready / deleted."""
        name = next((x["nume"] for x in self.snapshot()["masini"] if x["id"] == mid), mid)

        def msg(text):
            with self.lock:
                self.pods.setdefault(mid, {})["msg"] = text

        def cancelled():
            with self.lock:
                return self.pods.get(mid, {}).get("cancel_start")
        try:
            if action == "stop":
                # its clip, background video and prepared clip go back to the queue right away
                self._pod_dead(mid, "opresc pod-ul")
                self._set_active(mid, False)
                # wait briefly for a start that is still creating the machine to hand over its id
                m = self._row(mid) or {}
                for _ in range(30):
                    m = self._row(mid) or {}
                    if engine.rented_info(m) or not self.pods.get(mid, {}).get("creating"):
                        break
                    time.sleep(2)
                info = engine.rented_info(m)
                if info:
                    self._api(info["provider"]).terminate(info["id"])
                self._update_machine(mid, **CLEAR_RENTAL)
                with self.lock:
                    self.pods[mid].update({"status": None, "cost": None, "error": None})
                    self.cost_tick.pop(mid, None)
                    self.fresh.pop(mid, None)
                    remove = self.pods[mid].get("remove_after")
                self.log(f"{name}: pod-ul a fost sters. Nu se mai plateste nimic pentru el.")
                msg("Sters. Nu se mai plateste nimic. Apasa Porneste ca sa faci unul nou.")
                if remove:
                    self._remove_row(mid)
                return True
            if broken is not None:
                info, why = broken
                if not self._broken_retry(mid, name, info, why, msg, cancelled):
                    return False
            return self._start_machine(mid, name, msg, cancelled)
        except Exception as e:  # noqa: BLE001
            text = str(e)
            msg(f"Nu a mers: {text}")
            with self.lock:
                self.pods.setdefault(mid, {}).pop("remove_after", None)   # "Sterge" failed: keep the row
                self.pods[mid]["last_error"] = text
            self.log(f"{name}: {'stergerea' if action == 'stop' else 'pornirea'} pod-ului a esuat: {text}")
            if action == "start":
                self._drop_failed_vast(mid, name)
            return False
        finally:
            with self.lock:
                p = self.pods.setdefault(mid, {})
                p["creating"] = False
                if not (action == "start" and p.get("op") == "stop"):
                    p["busy"] = False
            self._refresh_pods(self.snapshot(), force=True)

    def _drop_failed_vast(self, mid, name):
        """A Vast.ai machine whose start failed (row not switched on) is deleted at once: it would
        be billed while doing nothing, and a Vast machine is never restarted anyway."""
        m = self._row(mid) or {}
        info = engine.rented_info(m)
        if not info or info["provider"] != "vast" or m.get("activ"):
            return
        try:
            self._api("vast").terminate(info["id"])
        except Exception as e:  # noqa: BLE001
            self.log(f"{name}: nu am putut șterge mașina de pe Vast.ai ({e}); șterge-o tu de pe console.vast.ai.")
            return
        self._update_machine(mid, **CLEAR_RENTAL)
        with self.lock:
            self.pods.setdefault(mid, {}).update({"status": None, "cost": None, "machine_id": None})
            self.cost_tick.pop(mid, None)
            self.fresh.pop(mid, None)
        self.log(f"{name}: am șters mașina de pe Vast.ai, ca să nu plătești pentru ea degeaba. "
                 "Apasă din nou „Pornește GPU-ul” ca să iau alta.")

    def _start_machine(self, mid, name, msg, cancelled):
        """Reuse the row's machine when it can run, otherwise rent a new one; wait for VoiceStudio
        (a new machine that starts broken is deleted and replaced, at most MAX_REPLACE times),
        install the models, switch the row on. True = ready."""
        c = self.snapshot()
        m = self._row(mid)
        if m is None:
            return False
        prov = self._prov_of(m, c)
        api = self._api(prov, c)
        info = engine.rented_info(m)
        pid = info["id"] if info else None
        reused_runpod = False
        if pid and prov == "runpod":
            try:
                st = api.status(pid).get("status")
            except Exception:  # noqa: BLE001
                st = None
            if st == "EXITED":
                try:
                    api.start(pid)
                    msg("Pornesc pod-ul...")
                    reused_runpod = True
                except Exception:  # noqa: BLE001
                    msg("Placa veche nu mai e libera, fac un pod nou...")
                    self.log(f"{name}: placa veche nu mai e libera, fac un pod nou.")
                    try:
                        api.terminate(pid)
                    except Exception:  # noqa: BLE001
                        pass
                    pid = None
            elif st != "RUNNING":
                pid = None
            else:
                reused_runpod = True
        elif pid and prov == "vast":
            try:
                st = api.status(pid).get("status")
            except Exception:  # noqa: BLE001
                st = "?"
            if st not in ("RUNNING", "STARTING", "?"):
                # stopped / broken / gone: a Vast machine is not restarted, a new one is rented
                try:
                    api.terminate(pid)
                except Exception:  # noqa: BLE001
                    pass
                self._update_machine(mid, **CLEAR_RENTAL)
                pid = None
        created = time.time()
        while True:
            if pid is None:
                if cancelled():
                    return False
                made = self._create_machine(mid, name, prov, api, c, msg, cancelled)
                if made is None:
                    return False
                pid, created = made
                reused_runpod = False
            if reused_runpod:
                why = self._wait_reused(mid, msg, cancelled)
            else:
                why = self._wait_ready(mid, prov, api, pid, msg, cancelled, created)
            if why is None:
                return False          # cancelled (the stop that cancelled it deletes the machine)
            if why == "":
                break
            if reused_runpod:
                raise engine.TaskFailed(why)
            if not self._broken_retry(mid, name, {"provider": prov, "id": pid}, why, msg, cancelled):
                raise engine.TaskFailed(f"GPU-ul a pornit stricat ({why})")
            pid = None
        if cancelled():
            return False
        m = self._row(mid)
        msg("Pornit. Instalez modelele...")
        engine.Dubber(self.snapshot(), machine=m).install_models(msg)
        if cancelled():
            return False
        try:          # speakers told apart by voice (pyannote) here too; never blocks the machine
            engine.Dubber(self.snapshot(), machine=m).ensure_diarization(msg)
        except Exception:  # noqa: BLE001
            pass
        if cancelled():
            return False
        with self.lock:                          # a new machine in this row: a clean slate
            self.dropped.pop(mid, None)
            self.excused.pop(mid, None)
            self.machine_errors.pop(mid, None)
        self._set_active(mid, True)
        self.log(f"{name}: pornit si gata de dublat.")
        msg("Pornit si gata de dublat.")
        with self.lock:
            resume = mid in self.resume_on_ready and not self.stop_after and not self.finishing
            self.resume_on_ready.discard(mid)
        if self.running:
            self.wake.set()
            threading.Timer(3, self.start, kwargs={"join_only": True}).start()
        elif resume:
            # every board had stopped meanwhile (they all died / were replaced): carry on with the queue
            threading.Timer(3, self.start, kwargs={"resume": True}).start()
        return True

    def _create_machine(self, mid, name, prov, api, c, msg, cancelled):
        """Rent a new machine for the row. -> (id, created_at), or None when cancelled meanwhile."""
        m = self._row(mid) or {}
        vs_key = m.get("cheie") or ("dublare-" + secrets.token_urlsafe(18))
        env = {"OMNIVOICE_API_KEY": vs_key}
        tok = engine.hf_token(c)
        if tok:
            env["HF_TOKEN"] = tok          # the GPU's VoiceStudio: speaker detection by voice (pyannote)
        with self.lock:
            self.pods[mid]["creating"] = True
        if prov == "vast":
            msg(f"Caut o placă pe Vast.ai (cel mult {c['vast_pret_max']:.2f} $/oră)...")
            pid, gpu, cost = api.create(f"dublare-{mid}"[:60], c, env, log=msg)
            offer = getattr(api, "last_offer", None) or {}
            self._update_machine(mid, furnizor="vast", vast_id=str(pid), adresa="", lucrator="", cheie=vs_key)
            try:
                nv_guess = engine.nvenc_guess(offer, engine.nvenc_memory())
            except Exception:  # noqa: BLE001 - only a note in the log: never lose the rented machine for it
                nv_guess = None
            extra = {"status": "STARTING", "cloud": "VAST", "machine_id": offer.get("machine_id"),
                     "nv_guess": nv_guess}
        else:
            msg("Caut o placa libera...")
            pid, gpu, cost = api.create(f"dublare-{mid}"[:60], c["runpod_gpus"], env,
                                        c["runpod_community"], log=msg, worker=c.get("video_pe_gpu", True))
            self._update_machine(mid, furnizor="runpod", vast_id="", lucrator="",
                                 adresa=f"https://{pid}-3900.proxy.runpod.net", cheie=vs_key)
            extra = {"status": "RUNNING", "cloud": getattr(api, "last_cloud", None) or "SECURE"}
        created = time.time()
        with self.lock:
            self.pods[mid].update({"creating": False, "cost": cost, "gpu": gpu, **extra})
            if prov == "runpod":
                self.cost_tick[mid] = created      # billing starts now
            self.fresh[mid] = created
        where = "Vast.ai" if prov == "vast" else "RunPod"
        nv_note = ""
        if prov == "vast" and extra.get("nv_guess") == 0:
            nv_note = (" · mașină cu un singur GPU (sau verificată deja): video-ul ar trebui să se codeze "
                       "pe placă, rapid")
        self.log(f"{name}: {'pod nou' if prov == 'runpod' else 'mașină nouă pe ' + where} pe {gpu}"
                 + (f" ({float(cost):.2f} $/ora)" if cost else "") + nv_note + ".")
        if cancelled():
            api.terminate(pid)
            self._update_machine(mid, **CLEAR_RENTAL)
            return None
        return pid, created

    def _health(self, mid):
        """'ok' when VoiceStudio answers (key included); else the HTTP status code it got, or None."""
        m = self._row(mid)
        if not m or not m.get("adresa"):
            return None
        err = engine.Dubber(self.snapshot(), machine=m).check_access()
        if err is None:
            return "ok"
        mm = re.search(r"HTTP (\d{3})", str(err))
        return int(mm.group(1)) if mm else None

    def _wait_reused(self, mid, msg, cancelled):
        """An existing RunPod pod (re)started: wait for VoiceStudio as before (20 min)."""
        msg("Se incarca VoiceStudio pe pod (primul start dureaza 2-6 minute)...")
        deadline = time.time() + 20 * 60
        while self._health(mid) != "ok":
            if cancelled():
                return None
            if time.time() > deadline:
                return "pod-ul nu a pornit in 20 de minute"
            time.sleep(10)
        return ""

    def _wait_ready(self, mid, prov, api, pid, msg, cancelled, created):
        """A newly rented machine: '' once VoiceStudio answers, None when cancelled, otherwise why
        it is broken: not running after START_MAX_S, VoiceStudio not answering START_HEALTHY_S after
        the container is up, or VoiceStudio itself answering 5xx for ERR5XX_S."""
        with self.lock:
            cloud = self.pods.get(mid, {}).get("cloud")
        kind = "vast" if prov == "vast" else ("community" if cloud == "COMMUNITY" else "secure")
        limit = self.START_HEALTHY_S[kind]
        up_at, err_since, last_code, gone, next_poll = None, None, None, 0, 0.0
        msg("Se descarcă imaginea pe mașina Vast.ai..." if prov == "vast"
            else "Se incarca VoiceStudio pe pod (primul start dureaza 2-6 minute)...")
        while True:
            if cancelled():
                return None
            now = time.time()
            if up_at is None and now - created > self.START_MAX_S:
                return f"nu a pornit în {self.START_MAX_S // 60} minute"
            if up_at is None and now >= next_poll:
                next_poll = now + self.START_POLL_S
                if prov == "vast":
                    try:
                        st = api.status(pid)
                    except Exception:  # noqa: BLE001 - Vast didn't answer now: ask again
                        st = {"status": "?"}
                    s = st.get("status")
                    if s not in (None, "TERMINATED", "?"):
                        gone = 0                 # listed again: the "missing" clock restarts
                    with self.lock:
                        if s in ("STARTING", "RUNNING"):
                            self.pods[mid]["status"] = s
                    if s is None or s == "TERMINATED":
                        # a just-rented instance can be missing from Vast's API for a minute or
                        # two (it answered "not found" seconds after the rental): only a machine
                        # missing for VAST_GONE_S counts as gone
                        gone = gone or now
                        if now - gone >= self.VAST_GONE_S:
                            return "Vast.ai nu mai are mașina"
                    elif s == "ERROR":
                        return "Vast.ai: " + (st.get("msg") or "eroare la pornire")[:120]
                    elif s in ("EXITED", "OFFLINE"):
                        return f"mașina s-a oprit pe Vast.ai ({s.lower()})"
                    elif s == "RUNNING" and st.get("adresa"):
                        self._update_machine(mid, adresa=st["adresa"], lucrator=st.get("lucrator") or "")
                        up_at = now
                        msg("Pornită. Se încarcă VoiceStudio...")
                else:
                    started = api.started(pid) if hasattr(api, "started") else None
                    if started or (started is None and now - created >= self.PULL_ALLOW_S):
                        up_at = now
            if up_at is not None:
                code = self._health(mid)
                if code == "ok":
                    return ""
                if cancelled():
                    return None
                last_code = code or last_code
                if isinstance(code, int) and code >= 500 and code not in engine.RETRY_STATUSES:
                    # VoiceStudio itself answers with an error (not the proxy / a closed port)
                    err_since = err_since or now
                    if now - err_since >= self.ERR5XX_S:
                        return f"VoiceStudio răspunde cu eroare HTTP {code} de {int(now - err_since) // 60} minute"
                if now - up_at >= limit:
                    return (f"VoiceStudio nu răspunde după {limit // 60} minute"
                            + (f", ultima dată HTTP {last_code}" if last_code else ""))
            time.sleep(min(self.START_POLL_S, 2.0) if up_at is None else self.START_POLL_S)

    def _broken_retry(self, mid, name, info, why, msg, cancelled):
        """The row's new machine started broken: delete it; True = take another one now."""
        with self.lock:
            n = self.replaced[mid] = self.replaced.get(mid, 0) + 1
            self.fresh.pop(mid, None)
            mach = self.pods.get(mid, {}).get("machine_id")
            stop = bool(cancelled() or self.finishing)
        if info["provider"] == "vast" and mach is not None:
            engine.Vast.BAD_MACHINES.add(mach)        # never this host again in this session
        try:
            self._api(info["provider"]).terminate(info["id"])
        except Exception as e:  # noqa: BLE001
            self.log(f"{name}: nu am putut șterge mașina stricată ({e}); verific-o pe site-ul furnizorului.")
        self._update_machine(mid, **CLEAR_RENTAL, activ=False)
        with self.lock:
            self.cost_tick.pop(mid, None)
            self.pods[mid].update({"status": None, "cost": None, "machine_id": None})
        if stop:
            self.log(f"{name} pornit stricat ({why}); l-am șters.")
            return False
        if n > self.MAX_REPLACE:
            self.log(f"{name} pornit stricat ({why}); l-am șters. Am luat deja {self.MAX_REPLACE} GPU-uri "
                     "noi în locul lui și toate au pornit stricate, nu mai iau altul acum.")
            msg(f"Nu a mers: a pornit stricat de {n} ori.")
            return False
        self.log(f"{name} pornit stricat ({why}), îl șterg și iau altul ({n}/{self.MAX_REPLACE}).")
        msg("Pornit stricat. Îl șterg și iau alt GPU...")
        return True

    # A step that shows no sign of life (no new step, no progress, no log line of its clip) for longer than it
    # can take is stuck (a request the GPU never answers, a stream that only sends keepalives...): it is
    # aborted, so a card is never held forever. Generous limits: only real hangs reach them.
    # (taiere = ffmpeg on this PC; upload = this PC's internet, a whole video on a slow line can take long and
    # its POST can't be interrupted anyway; traducere = the agent on this PC: none of them is the card's fault)
    NOT_CARD_STAGES = ("taiere", "upload", "traducere")
    STAGE_STUCK_S = {"taiere": 30 * 60, "upload": 90 * 60, "transcriere": 30 * 60, "traducere": 90 * 60,
                     "voce": 45 * 60, "verificare": 45 * 60, "export": 30 * 60, "video": 40 * 60}
    STAGE_STUCK_DEFAULT_S = 45 * 60
    STUCK_BROKEN = 2              # clips stuck one after the other on the same card = the card is broken
    DROP_BROKEN = 2               # different clips in a row whose connection broke mid-step on the same rented
                                  # card, while it answers again right after = the card is broken
    DROP_SAME_TIME_S = 180        # ...unless other cards lost theirs within this long too (this PC's internet;
    DROP_GRACE_S = 60             # they may report it a little later, so the decision waits this long), at most
    DROP_EXCUSE_S = 30 * 60       # once per this long (two broken cards must not excuse each other forever)
    STAGE_LABEL = {"taiere": "tăiere pauze", "upload": "upload", "transcriere": "transcriere",
                   "traducere": "traducere", "voce": "voce", "verificare": "verificare", "export": "export",
                   "video": "video"}

    def _stuck_check(self, c=None):
        """(1) A new machine whose first transcription doesn't move: broken (old driver), replace it.
        (2) Any clip whose step stopped moving for longer than that step can take: the step is aborted, the
        clip goes back for one more try later (or only its video, when the voice was done) and the card
        takes the next clip (see _after_stuck)."""
        now = time.time()
        hung = []
        with self.lock:
            stuck = [mid for mid in self.fresh
                     if (self.jobs.get(mid) or {}).get("stage") == "transcriere"
                     and now - (self.jobs[mid].get("moved") or self.jobs[mid]["started"]) >= self.TRANSCRIBE_STUCK_S]
            for mid, j in self.jobs.items():
                d, stage = j.get("dubber"), j.get("stage") or ""
                if d is None or mid in stuck or (self.stuck.get(mid) or {}).get("dubber") is d:
                    continue
                if mid in self.fresh and stage == "transcriere":
                    continue                     # (1) above
                idle = now - (j.get("moved") or j.get("started") or now)
                if idle < self.STAGE_STUCK_S.get(stage, self.STAGE_STUCK_DEFAULT_S):
                    continue
                why = (f"pasul „{self.STAGE_LABEL.get(stage, stage)}” nu a mai înaintat de "
                       f"{int(idle // 60)} minute")
                self.stuck[mid] = {"dubber": d, "why": why, "stage": stage}
                hung.append((mid, d, j.get("clip"), why))
            # the next clip, prepared in the background: the same rule (its worker waits for it)
            for mid, p in self.prefetch.items():
                d, stage = p.get("dubber"), p.get("stage") or ""
                if d is None or p.get("stuck") or p.get("done") or "moved" not in p:
                    continue
                idle = now - p["moved"]
                if idle < self.STAGE_STUCK_S.get(stage, self.STAGE_STUCK_DEFAULT_S):
                    continue
                why = (f"pregătirea („{self.STAGE_LABEL.get(stage, stage)}”) nu a mai înaintat de "
                       f"{int(idle // 60)} minute")
                p["stuck"], p["stuck_at"] = {"dubber": d, "why": why, "stage": stage}, now
                hung.append((mid, d, p.get("clip"), why))
        for mid in stuck:
            self._replace_broken(mid, f"transcrierea nu înaintează de {self.TRANSCRIBE_STUCK_S // 60} minute")
        for mid, d, clip, why in hung:
            self.log(f"{clip}: {why}; îl opresc, iar placa trece mai departe (clipul se reia mai târziu).",
                     self._row(mid))
            threading.Thread(target=d.abort, daemon=True).start()

    def _replace_broken(self, mid, why):
        with self.lock:
            if mid not in self.fresh:
                return
            self.fresh.pop(mid, None)
            m = next((dict(x) for x in self.settings["masini"] if x["id"] == mid), None)
            info = engine.rented_info(m)
            p = self.pods.setdefault(mid, {})
            if not info or p.get("busy"):
                return
            p.update(busy=True, op="start", cancel_start=False, prov=info["provider"],
                     msg="Pornit stricat. Îl șterg și iau alt GPU...")
            if self.workers:
                self.resume_on_ready.add(mid)
        self._pod_dead(mid, f"pornit stricat ({why})")       # its clip goes back to the queue now
        threading.Thread(target=self._pod_action, args=(mid, "start"), kwargs={"broken": (info, why)},
                         daemon=True).start()

    # ---------------------------------------------------------- Vast.ai: replace a machine that died
    def _queue_left(self, c=None):
        """Clips still to dub (waiting, or being dubbed somewhere)."""
        c = c or self.snapshot()
        with self.lock:
            return bool(self.jobs) or bool(self._claimable(c, 1))

    def _schedule_replace(self, mid, why):
        m = self._row(mid)
        if not m:
            return
        with self.lock:
            if mid in self.want_replace:
                return
            if self.finishing or self.stop_after or not self._key("vast"):
                return
            n = self.death_repl.get(mid, 0)
            if n >= self.MAX_DEATH_REPLACE:
                too_many = True
            else:
                too_many = False
                self.death_repl[mid] = n + 1
                self.want_replace[mid] = {"next": time.time(), "why": why, "tries": 0}
                self.resume_on_ready.add(mid)
        if too_many:
            self.log(f"{m['nume']}: a picat deja de {self.MAX_DEATH_REPLACE} ori azi; nu mai iau alt GPU în locul lui.")
            return
        self.log(f"{m['nume']}: caut automat alt GPU pe Vast.ai în locul lui.")
        self.wake.set()

    def _replace_tick(self, c=None):
        """Rent the replacements that are due (watcher loop, every few seconds)."""
        now = time.time()
        with self.lock:
            due = [mid for mid, w in self.want_replace.items() if w["next"] <= now]
        if not due:
            return
        left = self._queue_left()
        for mid in due:
            with self.lock:
                w = self.want_replace.get(mid)
                row = next((x for x in self.settings["masini"] if x["id"] == mid), None)
                if w is None:
                    continue
                if row is None or self.finishing or self.stop_after or not left or not self._key("vast"):
                    self.want_replace.pop(mid, None)
                    self.resume_on_ready.discard(mid)
                    if row is not None and not left:
                        self.log(f"{row['nume']}: coada e gata, nu mai caut alt GPU.")
                    continue
                p = self.pods.setdefault(mid, {})
                if p.get("busy"):
                    w["next"] = now + 5
                    continue
                p.update(busy=True, op="start", cancel_start=False, prov="vast", msg="Caut alt GPU pe Vast.ai...")
                w["next"] = float("inf")             # in flight
            threading.Thread(target=self._death_replace, args=(mid,), daemon=True).start()

    def _death_replace(self, mid):
        m = self._row(mid) or {}
        info = engine.rented_info(m)
        if info:
            try:
                self._api(info["provider"]).terminate(info["id"])
            except Exception:  # noqa: BLE001
                pass
            self._update_machine(mid, **CLEAR_RENTAL)
        ok = self._pod_action(mid, "start")
        with self.lock:
            w = self.want_replace.get(mid)
            if w is None:
                return
            if ok:
                self.want_replace.pop(mid, None)
                return
            w["tries"] += 1
            w["next"] = time.time() + self.VAST_RETRY_S
            err = self.pods.get(mid, {}).get("last_error") or "fără ofertă"
        self.log(f"{m.get('nume', mid)}: nu am găsit acum alt GPU potrivit pe Vast.ai ({err}); "
                 f"mai încerc peste {self.VAST_RETRY_S // 60} minute.")

    def delete_machine(self, mid):
        """"Sterge" on a row: a row with a pod first deletes the pod (no orphan pod billing
        forever), then the row disappears. Returns (error, message)."""
        c = self.snapshot()
        m = next((x for x in c["masini"] if x["id"] == mid), None)
        if not m:
            return None, "Sters."
        if mid == "local":
            return "„Calculatorul meu” nu se poate sterge", None
        with self.lock:
            p = self.pods.get(mid, {})
            info = engine.rented_info(m)
            has_pod = bool(info) or (p.get("busy") and p.get("op") == "start")
            stopping = p.get("busy") and p.get("op") == "stop"
            self.want_replace.pop(mid, None)
        if has_pod or stopping:
            prov = self._prov_of(m, c)
            if not self._key(prov, c):
                if prov == "vast":
                    return ("are o mașină pe Vast.ai. Pune cheia de API Vast.ai ca s-o pot șterge, sau "
                            "șterge-o tu de pe console.vast.ai"), None
                return ("are un pod pe RunPod. Pune cheia de API RunPod ca sa-l pot sterge, sau sterge-l "
                        "tu de pe runpod.io, goleste adresa din rand si apoi apasa din nou Sterge"), None
            with self.lock:
                self.pods.setdefault(mid, {})["remove_after"] = True
            if not stopping:
                err = self.pod_action(mid, "stop")
                if err and "deja" not in err:
                    with self.lock:
                        self.pods.get(mid, {}).pop("remove_after", None)
                    return err, None
            return None, f"{m['nume']}: sterg pod-ul, apoi randul dispare singur."
        self._pod_dead(mid, "sters din lista")
        self._remove_row(mid)
        return None, "Sters."

    def _remove_row(self, mid):
        with self.lock:
            name = next((m["nume"] for m in self.settings["masini"] if m["id"] == mid), mid)
            masini = [m for m in self.settings["masini"] if m["id"] != mid]
            self.settings = engine.save_settings({**self.settings, "masini": masini})
            self.pods.pop(mid, None)
            self.mstate.pop(mid, None)
            self.installs.pop(mid, None)
            self.want_replace.pop(mid, None)
            self.fresh.pop(mid, None)
        self.log(f"{name}: randul a fost sters din lista.")

    def finish_day(self):
        """"Termin pe azi": no new clips; what already started (voice, videos in the background,
        renders) finishes; then every rented pod is deleted."""
        with self.lock:
            self.finishing = True
            self.stop_after = True
            self.want_replace.clear()           # no replacement for a GPU that dies now
            self.resume_on_ready.clear()
        self.log("Termin pe azi: nu mai iau clipuri noi, termin ce e inceput, apoi sterg GPU-urile inchiriate.")
        self.wake.set()

    def _finish_check(self, c=None):
        with self.lock:
            if not self.finishing:
                return
            # (a worker still stuck on a dead pod doesn't count: its clip went back to the queue)
            if ((self.workers - self.dead) or self.renders or self.bg or self.prefetch or self.rq_pods
                    or not self.render_q.empty() or not self.local_q.empty()):
                return
            self.finishing = False
        pods = [m for m in self.snapshot()["masini"] if m["id"] != "local"
                and (engine.rented_info(m) or self.pods.get(m["id"], {}).get("busy"))]
        if not pods:
            self.log("Gata pe azi. Nu e niciun GPU inchiriat pornit.")
            return
        err = self.stop_all_pods()
        self.log("Gata pe azi: clipurile incepute s-au terminat, " +
                 (f"dar nu am putut sterge toate GPU-urile ({err})." if err else
                  "sterg GPU-urile inchiriate. Nu se mai plateste nimic."))

    def _auto_stop_check(self, c):
        """(1) Delete the rented GPUs after N minutes with no clip to work on."""
        minutes = c.get("oprire_auto_min") or 0
        if not minutes or not self._any_key(c):
            self.idle_since = None
            return
        with self.lock:
            running_pods = [m for m in c["masini"] if m["id"] != "local"
                            and engine.rented_info(m)
                            and self.pods.get(m["id"], {}).get("status") == "RUNNING"]
            busy = (any(p.get("busy") for p in self.pods.values())
                    or any(j for mid, j in self.jobs.items() if mid != "local")
                    or any(r.get("remote") for r in self.renders.values()))
        if not running_pods or busy:
            self.idle_since = None
            return
        pending = self._pending_videos(c)
        if pending and self.workers:
            self.idle_since = None     # queue has work and is running: GPUs are about to take it
            return
        if self.idle_since is None:
            self.idle_since = time.time()
            return
        if time.time() - self.idle_since >= minutes * 60:
            self.idle_since = None
            self.log(f"Oprire automata: niciun clip de {minutes} min, sterg GPU-urile inchiriate "
                     "ca sa nu mai platesti.")
            self.stop_all_pods()

    def start_all_pods(self):
        errs, n = [], 0
        for m in self.snapshot()["masini"]:
            if m["id"] == "local":
                continue
            pod = self.pods.get(m["id"], {})
            if pod.get("busy") or (engine.rented_info(m) and pod.get("status") in ("RUNNING", "STARTING")):
                continue
            err = self.pod_action(m["id"], "start")
            if err:
                errs.append(f"{m['nume']}: {err}")
            else:
                n += 1
        if not n and not errs:
            return "toate GPU-urile sunt deja pornite"
        return "; ".join(errs) or None

    def add_new_pod(self):
        """(+ Adauga GPU inchiriat) New rented row + create its pod right away."""
        c = self.snapshot()
        prov = c["gpu_furnizor"]
        if not self._key(prov, c):
            return None, f"pune intai cheia de API {engine.PROVIDER_NAME[prov]}"
        with self.lock:
            masini = list(self.settings["masini"])
            rented = [m for m in masini if m["id"] != "local"]
            # reuse an empty row (no pod, not busy) instead of piling up rows
            free = next((m for m in rented if not engine.rented_info(m) and m["id"] not in self.want_replace
                         and not self.pods.get(m["id"], {}).get("busy")), None)
            if free is None:
                if len(rented) >= engine.MAX_MACHINES:
                    return None, f"ai deja {engine.MAX_MACHINES} GPU-uri, maximul"
                mid = "gpu" + secrets.token_hex(4)
                masini.append({"id": mid, "nume": "", "adresa": "", "cheie": "",
                               "activ": False, "managed": True})
                self.settings = engine.save_settings({**self.settings, "masini": masini})
            else:
                mid = free["id"]
        name = next((m["nume"] for m in self.snapshot()["masini"] if m["id"] == mid), mid)
        err = self.pod_action(mid, "start", prov=prov)
        if err:
            return None, err
        self.log(f"{name}: creez un pod nou pe RunPod..." if prov == "runpod"
                 else f"{name}: închiriez un GPU nou pe Vast.ai...")
        return name, None

    def stop_all_pods(self):
        errs = []
        with self.lock:
            self.want_replace.clear()            # stopped on purpose: nothing gets replaced
            self.resume_on_ready.clear()
        for m in self.snapshot()["masini"]:
            if m["id"] == "local":
                continue
            pod = self.pods.get(m["id"], {})
            if engine.rented_info(m) or (pod.get("busy") and pod.get("op") == "start"):
                err = self.pod_action(m["id"], "stop")
                if err and "deja" not in err:
                    errs.append(f"{m['nume']}: {err}")
        return "; ".join(errs) or None

    # ---------------------------------------------------------- queue
    # ---------------------------------------------------------- test: only cover the original subtitles
    def start_cover(self, which, name=""):
        if which != "all" and name and name in capcut_final_clips():
            return "Clipul ales e o traducere CapCut: verifică banda cu Previzualizează."
        c = self.snapshot()
        vids = [v for v in engine.list_videos(c) if not engine.is_audio(v)]
        if which != "all" and name:
            vids = [v for v in vids if v.name == name] or vids
        if not vids:
            return "Nu am clipuri video în folderul clipuri."
        if not randare.ffmpeg_ok():
            return "Lipsește ffmpeg."
        with self.lock:
            if self.cover["busy"]:
                return "Deja lucrează."
            if self.capcut["busy"]:
                return "Așteaptă să termine pregătirea pentru CapCut (mută clipurile din folder)."
            self.cover.update(busy=True, clip=None, percent=None, msg="Pornesc...")
            self.cover["stop"].clear()
        threading.Thread(target=self._cover_run, args=(vids[:1] if which == "first" else vids,),
                         daemon=True).start()
        return None

    def _cover_run(self, vids):
        done = 0
        stop = self.cover["stop"]
        try:
            for v in vids:
                if stop.is_set():
                    break
                c = self.snapshot()
                out = engine.folder(c["folder_dublate"]) / f"{v.stem}_fara_subtitrari.mp4"
                out.parent.mkdir(parents=True, exist_ok=True)

                def upd(**kw):
                    with self.lock:
                        self.cover.update(**kw)
                upd(clip=v.name, percent=None)
                c = dict(c, sterge_zona=engine.manual_zone(c, v.name))
                if c["sterge_zona_mod"] == "agent" and v.name not in c["sterge_zone_clip"]:
                    upd(msg=f"{v.name}: agentul caută subtitrările...")
                    try:
                        dur = randare.probe(v)["dur"] or 10.0
                        zone = engine.find_sub_zone(v, [dur * (i + 0.5) / 12 for i in range(12)], c, stop)
                        self.log(f"Test acoperire {v.name}: {engine.zone_text(zone)}")
                        if zone == "none":
                            continue
                        c = dict(c, sterge_zona=zone)
                    except engine.Cancelled:
                        break
                    except Exception as e:  # noqa: BLE001
                        self.log(f"Test acoperire {v.name}: agentul nu a mers ({e}); folosesc zona manuală")
                upd(msg=f"{v.name}: acopăr subtitrările...", percent=0.0)
                info = randare.cover_only(v, out, c, progress=lambda p: upd(percent=p), cancel=stop)
                done += 1
                if info.get("ai"):
                    self.log(f"Test acoperire {v.name}: {info['ai']}")
                self.log(f"Test acoperire {v.name}: gata ({info['encoder']}) -> {out.name}")
            msg = f"Gata: {done} clip(uri) în folderul dublate, cu numele ..._fara_subtitrari.mp4"
        except Exception as e:  # noqa: BLE001
            msg = f"Nu a mers: {e}"
            self.log(f"Test acoperire: {msg}")
        with self.lock:
            self.cover.update(busy=False, clip=None, percent=None, msg=msg)

    # ---------------------------------------------------------- tool: pauses out + blur, no dubbing
    def start_tool(self, which, name=""):
        c = self.snapshot()
        vids = [v for v in engine.list_videos(c) if not engine.is_audio(v)]
        if which != "all" and name:
            vids = [v for v in vids if v.name == name]
        if not vids:
            return "Nu am clipuri video în folderul clipuri." if which == "all" else "Alege un clip în lista „Zona pentru”."
        if not randare.ffmpeg_ok():
            return "Lipsește ffmpeg."
        with self.lock:
            if self.tool["busy"]:
                return "Deja lucrează."
            if self.capcut["busy"]:
                return "Așteaptă să termine pregătirea pentru CapCut (mută clipurile din folder)."
            self.tool.update(busy=True, clip=None, percent=None, msg="Pornesc...", done=0, total=len(vids))
            self.tool["stop"].clear()
        threading.Thread(target=self._tool_run, args=(vids,), daemon=True).start()
        return None

    def _tool_run(self, vids):
        import shutil as _sh
        import tempfile as _tf
        stop = self.tool["stop"]
        done, msg = 0, ""
        no_band = []

        def upd(**kw):
            with self.lock:
                self.tool.update(**kw)
        try:
            for n, v in enumerate(vids, 1):
                if stop.is_set():
                    break
                c = self.snapshot()
                out = engine.folder(c["folder_dublate"]) / f"{v.stem}_fara_pauze.mp4"
                out.parent.mkdir(parents=True, exist_ok=True)
                work = Path(_tf.mkdtemp(prefix="taiere-"))
                try:
                    cur, notes = v, []
                    # 1. pauses out of voice + picture (same settings as step 0 of the dub)
                    if c["taie_pauze"]:
                        upd(clip=v.name, percent=0.0, msg=f"{v.name} ({n}/{len(vids)}): tai pauzele...")
                        r = randare.remove_pauses(cur, work / "a.mp4", c, progress=lambda p: upd(percent=p * 0.5),
                                                  cancel=stop)
                        if r["cuts"]:
                            cur = work / "a.mp4"
                            notes.append(f"{r['cuts']} pauze, {r['before']:.0f}s -> {r['after']:.0f}s")
                    # 2. only the first N minutes
                    if c["durata_max_min"]:
                        upd(msg=f"{v.name} ({n}/{len(vids)}): scurtez la {c['durata_max_min']:g} min...")
                        r = randare.trim_to(cur, work / "b.mp4", c["durata_max_min"] * 60, c,
                                            progress=lambda p: upd(percent=50 + p * 0.1), cancel=stop)
                        if r["cut"]:
                            cur = work / "b.mp4"
                            notes.append(f"scurtat la {r['after'] / 60:.2f} min")
                    # 3. blur: the band drawn for this clip (or the common one)
                    own = c["sterge_zone_clip"].get(v.name)
                    band = own or (c["sterge_zona"] if c["tb_blur"] == "toate" else None)
                    if c["tb_blur"] == "nu":
                        band = None
                    elif not band:
                        no_band.append(v.name)
                        notes.append("FARA acoperire: clipul nu are banda lui (deseneaz-o la 7 → Zona pentru "
                                     "alegând clipul, sau alege „La toate”)")
                    up = randare.with_upscale(randare.probe(cur), c, cur)["pre"]
                    if band or up:
                        upd(msg=f"{v.name} ({n}/{len(vids)}): "
                                + ("pun blur" if band else "") + (" + " if band and up else "")
                                + ("export 1080p" if up else "") + "...", percent=60.0)
                        ci = randare.cover_only(cur, out, dict(c, sterge_zona=band),
                                                progress=lambda p: upd(percent=60 + p * 0.4), cancel=stop)
                        if band:
                            notes.append((ci.get("ai") or "blur") + ("" if own else " (banda comuna)"))
                        if up:
                            notes.append("1080p")
                    elif cur != v:
                        _sh.move(str(cur), str(out))
                    else:
                        _sh.copy2(str(v), str(out))
                        notes.append("nimic de taiat")
                finally:
                    _sh.rmtree(work, ignore_errors=True)
                done += 1
                upd(done=done)
                self.log(f"Taiere + blur {v.name}: " + (", ".join(notes) or "gata") + f" -> {out.name}")
            msg = (f"Gata: {done} clip(uri) în folderul dublate, cu numele ..._fara_pauze.mp4"
                   if not stop.is_set() else f"Oprit după {done} clip(uri).")
            if no_band:
                msg += (f" Atenție: {len(no_band)} clip(uri) fără bandă desenată au ieșit doar fără pauze "
                        f"({', '.join(no_band[:3])}{'…' if len(no_band) > 3 else ''}). Desenează banda la "
                        "7 → Zona pentru (alegând clipul) sau alege „La toate”.")
        except Exception as e:  # noqa: BLE001
            msg = f"Nu a mers: {e}"
            self.log(f"Taiere + blur: {msg}")
        upd(busy=False, clip=None, percent=None, msg=msg)

    # ---------------------------------------------------------- CapCut, fără dublare: „doar finalul” (scurtat la 4:00)
    #                                                            sau „ca înainte” (toate pauzele tăiate, apoi max 4:30)
    def _capcut_taken(self, v, c):
        """Clipul e (sau a fost) al dublării: în lucru, cu o eroare de dublare, dat unui cont sau cu dublaj gata.
        (sub lacăt)"""
        if v.name in self._busy_clips() or v.name in self.failed or v.name in self.retry_note:
            return True
        if engine.account_of(v.name):
            return True
        return any(engine.output_path(v, engine.lang_cfg(c, l)).exists() for l in engine.target_langs(c))

    def _capcut_candidates(self, c):
        """Clipurile descărcate NOI: video din folderul clipuri (nu din subfoldere), pe care dublarea nu le-a
        folosit niciodată (nu sunt în conturi_clipuri.json, n-au dublaj gata), nu sunt în lucru și nu au fost deja
        pregătite / traduse pentru CapCut. (sub lacăt)"""
        p = HERE / "conturi_clipuri.json"
        try:
            known = set(json.loads(p.read_text(encoding="utf-8")).get("clips") or {}) if p.exists() else set()
        except Exception:  # noqa: BLE001  (nu-l pot citi acum: nu risc să trimit la CapCut un clip dat unui cont)
            return []
        out = []
        for v in engine.list_videos(c):
            if engine.is_audio(v) or v.name in known:
                continue
            try:
                if (v.name, v.stat().st_size) in self._capcut_short or self._capcut_taken(v, c):
                    continue
            except Exception:  # noqa: BLE001
                continue
            if _capcut_known(v.stem):
                continue
            out.append(v)
        return out

    def capcut_new_count(self):
        now = time.time()
        if now - self._capcut_new[0] > 5:
            try:
                with self.lock:
                    n = len(self._capcut_candidates(self.snapshot()))
            except Exception:  # noqa: BLE001
                n = 0
            self._capcut_new = (now, n)
        return self._capcut_new[1]

    @staticmethod
    def _game_running():
        """League of Legends deschis? (pregătirea CapCut merge și atunci: codare doar pe procesor, cu prioritate mică)"""
        try:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq League of Legends.exe", "/NH"], capture_output=True,
                                 text=True, timeout=10, creationflags=0x08000000).stdout
            return "League of Legends.exe" in out
        except Exception:  # noqa: BLE001
            return False

    def start_capcut(self):
        if not randare.ffmpeg_ok():
            return "Lipsește ffmpeg."
        # (merge și cât rulează League of Legends: _capcut_run codează atunci doar pe procesor, cu prioritate mică)
        with self.lock:
            if self.capcut["busy"]:
                return "Deja lucrează."
            if self.tool["busy"] or self.cover["busy"]:
                return "Așteaptă să termine „Doar tăiere pauze + blur” / testul de acoperire (lucrează pe aceleași clipuri)."
            c = self.snapshot()
            lk = _capcut_lock(engine.folder(c["folder_clipuri"]) / "capcut")
            if lk is None:
                return "Altă fereastră a aplicației de dublare pregătește deja clipuri pentru CapCut."
            back = capcut_recover(c, held=True)   # o pregătire întreruptă: clipul ei se întoarce întâi în clipuri
            if back:
                self.log("CapCut pregătire: am pus înapoi în clipuri " + ", ".join(back) + " (pregătire întreruptă)")
            vids = self._capcut_candidates(c)
            if not vids:
                _capcut_unlock(lk)
                return ("Nu am clipuri noi: toate clipurile din folderul clipuri au fost deja dublate sau pregătite. "
                        "Descarcă clipuri noi cu Radarul.")
            self.capcut.update(busy=True, clip=None, percent=None, msg="Pornesc...", done=0, total=len(vids))
            self.capcut["stop"].clear()
        try:
            threading.Thread(target=self._capcut_run, args=(vids, lk), daemon=True).start()
        except Exception:  # noqa: BLE001
            _capcut_unlock(lk)
            with self.lock:
                self.capcut.update(busy=False, msg="")
            raise
        return None

    @staticmethod
    def _capcut_small(src: Path, out: Path, cancel, cpu_only=False):
        """Tăierea iese la calitate de lucru (~4 Mbit/s); pentru CapCut ajunge ~1,5 Mbit/s (cât au clipurile descărcate),
        aceeași rezoluție: de ~3 ori mai mic pe disc și pe telefon. Întoarce fișierul mic sau None (rămâne cel mare).
        cpu_only (cât rulează jocul): fără placa video și doar un sfert din fire."""
        try:
            if src.stat().st_size * 8 / max(1.0, randare.media_duration(src)) < 2_500_000:
                return None
        except Exception:  # noqa: BLE001
            return None
        ff = randare.find_ffmpeg()[0]
        x264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "24"] + \
            (["-threads", str(max(2, (os.cpu_count() or 4) // 4))] if cpu_only else [])
        for venc in ([] if cpu_only else [["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "26", "-b:v", "0"]]) \
                + [x264]:
            args = [ff, "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", *venc,
                    "-maxrate", "1500k", "-bufsize", "3000k", "-c:a", "copy", "-movflags", "+faststart", str(out)]
            pr = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  **randare._no_window())
            t0 = time.time()
            while pr.poll() is None:
                if cancel.is_set() or time.time() - t0 > 1800:
                    pr.kill()
                    pr.wait()
                    if cancel.is_set():
                        raise randare.RenderError("oprit")
                    break
                time.sleep(0.3)
            try:
                if (pr.returncode == 0 and out.stat().st_size < src.stat().st_size
                        and abs(randare.media_duration(out) - randare.media_duration(src)) < 1.0):
                    return out
            except Exception:  # noqa: BLE001
                pass
        return None

    _montaj_mod = None
    _montaj_mtime = None

    @classmethod
    def _capcut_montaj(cls):
        """tiktok_bot\\capcut\\montaj.py: pregătirea în cele două variante („doar finalul”: scurtat de la final la 4:00,
        terminat între două cuvinte; „ca înainte”: aceeași tăiere a golurilor ca la montaj, detector de voce + Whisper)
        și marcajul variantei în fișier (după el lucrează apoi montajul). None dacă nu se poate încărca.
        Dacă montaj.py se schimbă pe disc, îl încarc din nou (fără să repornești aplicația)."""
        p = TIKTOK_DIR.parent / "capcut" / "montaj.py"
        try:
            mt = p.stat().st_mtime
        except OSError:
            mt = None
        if cls._montaj_mod is None or (mt is not None and mt != cls._montaj_mtime):
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location("capcut_montaj", p)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                cls._montaj_mod = mod
            except Exception as e:  # noqa: BLE001
                print("montaj.py nu se poate încărca:", e, flush=True)
                if cls._montaj_mod is None:
                    cls._montaj_mod = False          # (o versiune veche care mergea rămâne folosită)
            cls._montaj_mtime = mt
        return cls._montaj_mod or None

    @staticmethod
    def _capcut_voice(src: Path, work: Path, cancel):
        """Clipurile cu muzică pe fundal nu au liniște între fraze (tăierea după liniște nu găsește nimic), așa că
        pauzele le caut cu detectorul de voce (Silero VAD din faster_whisper, pe procesor, ~1 s): în vorbire sunetul
        original rămâne neatins, între fraze pun liniște, iar remove_pauses taie apoi liniștea.
        Întoarce (voce.wav, secunde de vorbire) sau (None, 0) — atunci se taie după liniște, ca înainte."""
        try:
            import wave
            import numpy as np
            from faster_whisper.vad import VadOptions, get_speech_timestamps
        except Exception:  # noqa: BLE001
            return None, 0.0
        ff = randare.find_ffmpeg()[0]

        def dec(sr, ch):
            r = subprocess.run([ff, "-v", "error", "-i", str(src), "-map", "0:a:0", "-ac", str(ch), "-ar", str(sr),
                                "-f", "s16le", "-"], capture_output=True, timeout=900, creationflags=0x08000000)
            if r.returncode != 0 or not r.stdout:
                raise RuntimeError("nu pot citi sunetul")
            return np.frombuffer(r.stdout, np.int16).reshape(-1, ch)
        try:
            m16 = dec(16000, 1)[:, 0].astype(np.float32) / 32768
            if cancel.is_set():
                return None, 0.0
            # prag mic + margini largi: mai bine rămâne o pauză scurtă decât să tai o replică
            seg = get_speech_timestamps(m16, VadOptions(threshold=0.15, min_silence_duration_ms=400, speech_pad_ms=200))
            if not seg:
                return None, 0.0
            full = dec(48000, 2)
            gated = np.zeros_like(full)
            for s in seg:
                i, j = int(s["start"]) * 3, min(len(full), int(s["end"]) * 3)      # 16 kHz -> 48 kHz
                gated[i:j] = full[i:j]
            out = work / "voce.wav"
            with wave.open(str(out), "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(48000)
                w.writeframes(np.ascontiguousarray(gated).tobytes())
            return out, sum(int(s["end"]) - int(s["start"]) for s in seg) / 16000
        except Exception:  # noqa: BLE001
            return None, 0.0

    def _capcut_run(self, vids, lk=None):
        import tempfile as _tf
        stop = self.capcut["stop"]
        done, skipped, msg, blocked = 0, [], "", False

        def upd(**kw):
            with self.lock:
                self.capcut.update(**kw)

        def unique(p: Path) -> Path:
            if not p.exists():
                return p
            for i in range(2, 999):
                q = p.with_name(f"{p.stem}_{i}{p.suffix}")
                if not q.exists():
                    return q
            return p

        def journal(keep: Path, **d):
            """Clipul la care lucrez acum (capcut_recover îl pune înapoi dacă aplicația se oprește între timp)."""
            t = keep / (CAPCUT_JOURNAL + ".tmp")
            t.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            os.replace(t, keep / CAPCUT_JOURNAL)
        # varianta aleasă (aceeași pentru toate clipurile din rundă): „doar finalul” sau „ca înainte”
        vechi = self.snapshot().get("capcut_pregatire") == "ca_inainte"
        try:
            CAPCUT_IN.mkdir(parents=True, exist_ok=True)
            for n, v in enumerate(vids, 1):
                if stop.is_set():
                    break
                c = dict(self.snapshot(), taie_pauze=True) if vechi else self.snapshot()
                joc = self._game_running()       # cât rulează League of Legends: doar procesor, cu prioritate mică
                if hasattr(randare, "_tls"):
                    randare._tls.low = joc       # (firul ăsta: ffmpeg / ffprobe din randare și montaj.py)
                try:
                    d0 = randare.media_duration(v)
                except Exception:  # noqa: BLE001  (îl las pe tăiere să spună ce are)
                    d0 = None
                if d0 is not None and d0 < CAPCUT_MIN_S:     # nu-l mut și nu-l tai degeaba
                    skipped.append(f"{v.name} (are doar {d0:.1f} s)")
                    try:
                        self._capcut_short.add((v.name, v.stat().st_size))
                    except OSError:
                        pass
                    self.log(f"CapCut pregătire {v.name}: prea scurt pentru CapCut ({d0:.1f} s), rămâne în clipuri")
                    continue
                keep = v.parent / "capcut"
                # îl scot din coada de dublare (clipuri\capcut), sub lacăt: „Start” nu-l mai poate lua între timp.
                # Verific din nou tot: între timp dublarea poate să-l fi luat, dublat sau dat unui cont.
                with self.lock:
                    try:
                        taken = not v.exists() or self._capcut_taken(v, c)
                    except Exception:  # noqa: BLE001
                        taken = True
                    if taken:
                        skipped.append(f"{v.name} (l-a luat dublarea între timp)")
                        continue
                    if (keep / CAPCUT_JOURNAL).exists():
                        self.log("CapCut pregătire: mă opresc — un clip anterior nu a putut fi pus înapoi în clipuri; "
                                 "îl pune înapoi următoarea pornire")
                        blocked = True
                        break
                    try:
                        keep.mkdir(exist_ok=True)
                        src = unique(keep / v.name)
                        journal(keep, orig=v.name, moved=src.name)
                        os.replace(v, src)
                    except OSError as e:          # ex. deschis în alt program: sar doar clipul ăsta
                        skipped.append(f"{v.name} (e deschis în alt program)")
                        self.log(f"CapCut pregătire {v.name}: nu l-am putut muta ({e}), rămâne în clipuri")
                        try:
                            (keep / CAPCUT_JOURNAL).unlink(missing_ok=True)
                        except OSError:
                            pass
                        continue
                ok, short_key, work, tmp, notes = False, None, None, None, []
                if joc:
                    notes.append("în timpul jocului: doar procesor, prioritate mică")
                try:
                    work = Path(_tf.mkdtemp(prefix="capcut-"))
                    cur = src
                    upd(clip=v.name, percent=0.0, msg=f"{v.name} ({n}/{len(vids)}): " + (
                        "tai golurile din vorbire..." if vechi else "păstrez cel mult 4:00 (fără tăierea pauzelor)..."))
                    mj = self._capcut_montaj()
                    mrep = None
                    import inspect
                    params = inspect.signature(mj.prepare).parameters if mj is not None else {}
                    if mj is not None and (vechi or "mod" in params):   # (un montaj.py vechi știe doar „ca înainte”)
                        upd(percent=10.0)
                        kw = ({"progress": lambda p: upd(percent=10 + p * 0.85)}       # bara curge și în timpul tăierii
                              if "progress" in params else {})
                        if "mod" in params:
                            kw["mod"] = "ca_inainte" if vechi else "doar_final"
                        try:
                            mrep = mj.prepare(src, work / "m.mp4", cancel=stop, **kw)
                        except mj.MontajError as e:
                            if stop.is_set():
                                raise
                            if "traduce de la" in str(e):          # prea scurt pentru CapCut: rămâne în clipuri
                                short_key = (v.name, src.stat().st_size)
                                raise
                            notes.append(f"tăierea după voce nu a mers ({e}), tai după liniște" if vechi else
                                         f"pregătirea din montaj.py nu a mers ({e}), îl scurtez direct")
                        except Exception as e:  # noqa: BLE001
                            if stop.is_set():
                                raise
                            notes.append(f"tăierea după voce nu a mers ({type(e).__name__}), tai după liniște" if vechi
                                         else f"pregătirea din montaj.py nu a mers ({type(e).__name__}), îl scurtez direct")
                    if mrep and vechi:
                        cur = work / "m.mp4"
                        notes.append(f"{mrep['cuts']} goluri tăiate (detector de voce + Whisper), "
                                     f"{mrep['before']:.0f}s -> {mrep['after']:.0f}s"
                                     + (", apoi tăiat de la final la 4:30" if mrep.get("trimmed") else ""))
                        upd(percent=95.0)
                    elif mrep:
                        cur = work / "m.mp4"
                        notes.append((f"tăiat de la final, {mrep['before']:.0f}s -> {mrep['after']:.0f}s"
                                      if mrep.get("trimmed") else "netăiat") + " (pauzele rămân)")
                        upd(percent=95.0)
                    elif vechi:                                # rezerva „ca înainte” (fără montaj.py): după liniște / voce
                        voice, _sp = self._capcut_voice(src, work, stop)
                        r = randare.remove_pauses(cur, work / "a.mp4", c, progress=lambda p: upd(percent=p * 0.8),
                                                  cancel=stop, voice=voice, max_out=CAPCUT_MAX_S_VECHI + 20)
                        if voice:
                            notes.append("pauze găsite după voce")
                        if r["cuts"]:
                            cur = work / "a.mp4"
                            notes.append(f"{r['cuts']} pauze, {r['before']:.0f}s -> {r['after']:.0f}s")
                        upd(msg=f"{v.name} ({n}/{len(vids)}): păstrez cel mult 4:30...", percent=80.0)
                        r = randare.trim_to(cur, work / "b.mp4", CAPCUT_MAX_S_VECHI, c,
                                            progress=lambda p: upd(percent=80 + p * 0.15), cancel=stop)
                        if r["cut"]:
                            cur = work / "b.mp4"
                            notes.append(f"scurtat la {r['after'] / 60:.2f} min")
                        upd(msg=f"{v.name} ({n}/{len(vids)}): îl fac mai mic (aceeași rezoluție)...", percent=95.0)
                        small = self._capcut_small(cur, work / "c.mp4", stop, cpu_only=joc)
                        if small:
                            cur = small
                    else:                                      # rezerva „doar finalul”: doar scurtat de la final
                        upd(msg=f"{v.name} ({n}/{len(vids)}): păstrez cel mult 4:00...", percent=10.0)
                        r = randare.trim_to(cur, work / "b.mp4", CAPCUT_MAX_S, c,
                                            progress=lambda p: upd(percent=10 + p * 0.8), cancel=stop)
                        if r["cut"]:
                            cur = work / "b.mp4"
                            notes.append(f"scurtat de la final la {r['after'] / 60:.2f} min")
                        upd(msg=f"{v.name} ({n}/{len(vids)}): îl fac mai mic (aceeași rezoluție)...", percent=95.0)
                        small = self._capcut_small(cur, work / "c.mp4", stop, cpu_only=joc)
                        if small:
                            cur = small
                        # marcajul „doar finalul” (montajul de după CapCut nu-i va tăia golurile), fără recodare
                        tagged = work / "t.mp4"
                        r = subprocess.run([randare.find_ffmpeg()[0], "-v", "error", "-y", "-i", str(cur), "-map", "0:v:0",
                                            "-map", "0:a?", "-c", "copy", "-metadata", f"comment={CAPCUT_TAG_FINAL}",
                                            "-movflags", "+faststart", str(tagged)], capture_output=True, timeout=900,
                                           **randare._no_window())
                        if r.returncode != 0 or not tagged.is_file():
                            raise RuntimeError("nu pot pune marcajul „doar finalul” în clip")
                        cur = tagged
                    dur = randare.media_duration(cur)
                    if dur < CAPCUT_MIN_S:
                        skipped.append(f"{v.name} ({'fără pauze ' if vechi else ''}are doar {dur:.1f} s)")
                        notes.append("prea scurt pentru CapCut")
                        short_key = (v.name, src.stat().st_size)
                    else:
                        # .tmp întâi: telefoanele iau doar *.mp4, deci nu pot lua un fișier scris pe jumătate
                        final = unique(CAPCUT_IN / f"{v.stem}.mp4")
                        tmp = final.with_name(final.name + ".tmp")
                        journal(keep, orig=v.name, moved=src.name, final=final.name)
                        shutil.copyfile(cur, tmp)
                        size = tmp.stat().st_size
                        engine._replace_retry(tmp, final)
                        ok = True               # de aici fișierul e al telefoanelor: originalul rămâne în clipuri\capcut
                        notes.append(f"{dur / 60:.2f} min, {size / 1e6:.0f} MB -> capcut_de_tradus/{final.name}")
                except Exception as e:  # noqa: BLE001
                    if not stop.is_set():
                        skipped.append(f"{v.name} ({e})")
                    notes = [f"nu a mers: {e}"]
                finally:
                    if work:
                        shutil.rmtree(work, ignore_errors=True)
                    if not ok and tmp is not None:
                        try:
                            tmp.unlink(missing_ok=True)   # copia mea scrisă pe jumătate (ex. disc plin)
                        except OSError:
                            pass                          # o scoate reluarea (după jurnal)
                    try:
                        if not ok and src.exists() and not v.exists():
                            engine._replace_retry(src, v)  # nepregătit: înapoi în clipuri, ca înainte
                        (keep / CAPCUT_JOURNAL).unlink(missing_ok=True)
                    except OSError as e:          # jurnalul rămâne: următoarea pornire îl pune înapoi
                        self.log(f"CapCut pregătire {v.name}: nu l-am putut pune înapoi în clipuri ({e}) — "
                                 f"îl pun la următoarea pornire")
                if short_key:
                    self._capcut_short.add(short_key)
                if ok:
                    done += 1
                upd(done=done)
                self.log(f"CapCut pregătire {v.name}: " + (", ".join(notes) or "gata"))
            if blocked:
                msg = (f"M-am oprit după {done} clip(uri): un clip nu a putut fi pus înapoi în clipuri (ținut de alt "
                       f"program). Apasă din nou peste un minut — îl pun înapoi și continui.")
            elif stop.is_set():
                msg = f"Oprit după {done} clip(uri)."
            else:
                msg = (f"Gata: {done} clip(uri) în capcut_de_tradus ("
                       + ("fără nicio pauză, max 4:30" if vechi else "cu pauzele lor, cel mult 4:00")
                       + "). Acum, în aplicația de postare → fila CapCut → „Traduce pe toate”.")
            if skipped:
                msg += f" Sărite: {len(skipped)} — " + "; ".join(skipped[:3]) + ("…" if len(skipped) > 3 else "")
        except Exception as e:  # noqa: BLE001
            msg = f"Nu a mers: {e}"
            self.log(f"CapCut pregătire: {msg}")
        finally:
            _capcut_unlock(lk)
            if hasattr(randare, "_tls"):
                randare._tls.low = False
        self._capcut_new = (0.0, 0)
        upd(busy=False, clip=None, percent=None, msg=msg)

    # ---------------------------------------------------------- CapCut -> ca un dublaj (vocea CapCut, restul ca aplicația)
    @staticmethod
    def _capfin_registry() -> dict:
        try:
            d = json.loads(CAPCUT_DUBLATE.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _capfin_save(reg: dict):
        tmp = CAPCUT_DUBLATE.with_name(CAPCUT_DUBLATE.name + ".tmp")
        tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
        engine._replace_retry(tmp, CAPCUT_DUBLATE)

    def _capfin_candidates(self, c):
        """Montajele CapCut (capcut_final: imaginea + vocea română, tăiate după varianta pregătirii) netrimise încă la randare.
        -> [(nume clip, montaj, cheie)]. Sar clipurile date unui cont înainte de traducerea CapCut (dublate altfel)."""
        import re as _re
        reg = self._capfin_registry()
        mine = reg.get("_conturi") if isinstance(reg.get("_conturi"), dict) else {}   # conturi date de butonul ăsta
        busy = self._busy_clips()
        out, seen = [], set()
        rap = CAPCUT_FINAL / "rapoarte"
        try:
            reports = sorted(rap.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if rap.is_dir() else []
        except OSError:
            reports = []
        for r in reports:
            key = r.stem                                       # <nume>_ro / _ro_primele_4m59 / _ro_2
            m = _re.fullmatch(r"(.+?)_ro(?:_primele_4m59)?(?:_(\d+))?", key)
            final = CAPCUT_FINAL / f"{key}.mp4"
            if not m or not final.is_file():
                continue
            clip = f"{m.group(1)}.mp4"                         # _ro, _ro_primele_4m59, _ro_2... = același clip
            if clip in seen:                                   # o traducere mai nouă a lui e deja aleasă
                continue
            seen.add(clip)
            if key in reg or clip in busy:
                continue
            acc = engine.account_of(clip)
            if acc and clip not in mine:                       # dat unui cont ÎNAINTE de montajul CapCut = dublat altfel
                try:
                    if time.mktime(time.strptime(str(acc.get("ts")), "%Y-%m-%d %H:%M:%S")) < r.stat().st_mtime:
                        continue
                except (ValueError, OSError, OverflowError):
                    continue
            out.append((clip, final, key))
        return out

    def capfin_pending(self) -> list:
        """Numele clipurilor CapCut de finalizat (numărate cel mult o dată la 5 s)."""
        now = time.time()
        if now - self._capfin_new[0] > 5:
            try:
                with self.lock:
                    names = [clip for clip, _, _ in self._capfin_candidates(self.snapshot())]
            except Exception:  # noqa: BLE001
                names = []
            self._capfin_new = (now, names)
        return list(self._capfin_new[1])

    _capfin_wait_cache = (0.0, None)

    def _capfin_waiting(self) -> set:
        """Clipurile CapCut care chiar mai așteaptă video-ul: au o randare în așteptare (dublate\\.lucru\\*\\render.json
        sau în lucru acum). Registrul (capcut_finalizate.json) rămâne mereu „la randare”, chiar după ce video-ul s-a
        făcut și s-a postat — fără asta pagina le arăta „la coada video” la nesfârșit. (cel mult o dată la 10 s; doar
        citesc, nu șterg nimic)"""
        now = time.time()
        if now - self._capfin_wait_cache[0] < 10 and self._capfin_wait_cache[1] is not None:
            return set(self._capfin_wait_cache[1])
        with self.lock:
            waiting = set(self.renders)
        try:
            root = engine.folder(self.snapshot()["folder_dublate"]) / ".lucru"
            for p in (root.glob(f"*/{engine.RENDER_FILE}") if root.is_dir() else []):
                try:
                    job = json.loads(p.read_text(encoding="utf-8")).get("job") or {}
                    if job.get("clip") and not Path(str(job.get("out") or "")).exists():
                        waiting.add(str(job["clip"]))
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        self._capfin_wait_cache = (now, waiting)
        return set(waiting)

    def capfin_state(self) -> dict:
        names = self.capfin_pending()
        own = self.snapshot().get("sterge_zone_clip") or {}
        cc = capcut_final_clips()
        with self.lock:
            err = sorted(n for n in self.failed if n in cc)
            randari = {n: {"percent": r.get("percent"), "running": bool(r.get("running")), "remote": bool(r.get("remote"))}
                       for n, r in self.renders.items() if n in cc}      # procentul video-ului, pentru bara din pagină
        reg = self._capfin_registry()
        waiting = self._capfin_waiting()
        trimise = {v["clip"]: {"out": Path(str(v.get("out") or "")).name, "stare": v.get("stare")}
                   for k, v in reg.items() if k != "_conturi" and isinstance(v, dict) and v.get("clip")
                   and v["clip"] in waiting}
        return dict({k: v for k, v in self.capfin.items() if k != "stop"}, new=len(names), clipuri=names,
                    fara_banda=sum(1 for n in names if n not in own), erori=err, randari=randari, trimise=trimise,
                    oprite=sorted({n for _d, n in self._capfin_stopped_dirs()}))

    def _capfin_paused(self, job) -> bool:
        """Video-ul acestei traduceri CapCut a fost oprit („Oprește video-urile”) și așteaptă „Pornește iar”."""
        try:
            return bool(isinstance(job, dict) and job.get("capcut") and (Path(job["work"]) / CAPFIN_OPRIT).is_file())
        except (OSError, KeyError, TypeError):
            return False

    def _capfin_stopped_dirs(self) -> list:
        """[(folder de lucru, nume clip)] pentru video-urile CapCut oprite."""
        out = []
        try:
            root = engine.folder(self.snapshot()["folder_dublate"]) / ".lucru"
            for m in root.glob(f"*/{CAPFIN_OPRIT}"):
                try:
                    job = json.loads((m.parent / engine.RENDER_FILE).read_text(encoding="utf-8"))["job"]
                    out.append((m.parent, str(job["clip"])))
                except Exception:  # noqa: BLE001 - fără render.json: video-ul nu mai e de făcut
                    pass
        except Exception:  # noqa: BLE001
            pass
        return out

    def stop_capfin_videos(self) -> int:
        """„Oprește video-urile”: traducerile CapCut care așteaptă video-ul nu mai pornesc, iar cea în lucru pe PC se
        oprește (și Vmake-ul ei). Rămân în dublate\\.lucru cu render.json, deci nu se pierde nimic: „Pornește iar” le
        pune înapoi în coadă (cea oprită din mers se ia de la capăt). Pregătirea (subtitrările) se oprește și ea."""
        self.capfin["stop"].set()
        marked = set()
        try:
            found = engine.pending_renders(self.snapshot())
        except Exception as e:  # noqa: BLE001
            self.log(f"CapCut: nu pot citi coada video ({e})")
            found = []
        for name, job, _t, _i in found:
            if isinstance(job, dict) and job.get("capcut"):
                try:
                    (Path(job["work"]) / CAPFIN_OPRIT).write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
                    marked.add(name)
                except (OSError, KeyError, TypeError):
                    pass
        with self.lock:
            for q in (self.render_q, self.local_q):
                items = []
                while True:
                    try:
                        items.append(q.get_nowait())
                    except queue.Empty:
                        break
                for it in items:
                    if it[0] in marked or self._capfin_paused(it[1] if len(it) > 1 else None):
                        self.renders.pop(it[0], None)
                    else:
                        q.put(it)
            running = [ev for n, (j, ev) in self.render_cancel.items() if n in marked]
        for ev in running:
            ev.set()
        if marked:
            self.log(f"CapCut: am oprit {len(marked)} video-uri" + (" (unul era în lucru: se oprește acum)" if running else "")
                     + ". Rămân în dublate\\.lucru; „Pornește iar” le reia.")
        self.wake.set()
        return len(marked)

    def resume_capfin_videos(self) -> int:
        """„Pornește iar”: video-urile CapCut oprite înapoi în coada video."""
        names = set()
        for d, name in self._capfin_stopped_dirs():
            try:
                (d / CAPFIN_OPRIT).unlink()
                names.add(name)
            except OSError:
                pass
        got = self._resume_renders(only=names) if names else []
        if names:
            self.log(f"CapCut: pornesc iar {len(got)} video-uri")
        self.wake.set()
        return len(got)

    def start_capfin(self, unde):
        if not randare.ffmpeg_ok():
            return "Lipsește ffmpeg."
        # (merge și cât rulează League of Legends: subtitrările și video-ul de pe PC merg atunci doar pe procesor, cu
        #  prioritate mică — vezi _capfin_run și _local_render)
        if not (TIKTOK_DIR.parent / "capcut" / "montaj.py").is_file():
            return "Lipsește tiktok_bot\\capcut\\montaj.py (face subtitrările românești)."
        with self.lock:
            if self.capfin["busy"]:
                return "Deja lucrează."
            c = self.snapshot()
            if c.get("format") != "mp4":
                return "Formatul de ieșire trebuie să fie MP4 (Imagine și export)."
            items = self._capfin_candidates(c)
            if not items:
                return "Nu am traduceri CapCut noi de finalizat (toate au fost trimise deja la randare)."
            self.capfin.update(busy=True, msg="Pornesc...", done=0, total=len(items))
            self.capfin["stop"].clear()
        threading.Thread(target=self._capfin_run, args=(items, unde == "pc"), daemon=True).start()
        return None

    def _capfin_zone(self, final, segs, c, stop, clip):
        """Zona subtitrărilor originale ale montajului, găsită de agent prin API (setarea zona_api): cadre din momentele
        cu cele mai lungi replici (acolo apar subtitrările pe două rânduri). None = n-a mers → banda comună."""
        spoken = [s for s in segs if isinstance(s, dict) and float(s.get("end", 0)) - float(s.get("start", 0)) > 0.4]
        longest = sorted(spoken or [s for s in segs if isinstance(s, dict)],
                         key=lambda s: len(str(s.get("text") or "")), reverse=True)[:12]
        times = sorted((float(s["start"]) + float(s["end"])) / 2 for s in longest if "start" in s and "end" in s)
        if not times:
            dur = randare.probe(final)["dur"] or 10.0
            times = [dur * (i + 0.5) / 12 for i in range(12)]
        try:
            zone = engine.find_sub_zone(final, times, c, stop)
        except engine.Cancelled:
            raise RuntimeError("oprit") from None
        except Exception as e:  # noqa: BLE001
            self.log(f"CapCut → dublaj {clip}: agentul nu a găsit zona subtitrărilor ({e}) — folosesc banda comună")
            return None
        self.log(f"CapCut → dublaj {clip}: {engine.zone_text(zone)}")
        return zone

    def _capfin_run(self, items, doar_pc):
        """Pentru fiecare montaj CapCut: subtitrări românești (Whisper turbo, proces separat) → cont TikTok → folder de
        lucru cu imaginea + vocea → render.json → coada video a aplicației (aceeași randare ca la dublaje: acoperirea
        subtitrărilor originale, upscale, subtitrări, watermark, @cont_NNN în dublate)."""
        stop = self.capfin["stop"]
        sent, skipped, msg = 0, [], ""
        work0 = Path(tempfile.mkdtemp(prefix="capfin-"))

        def upd(**kw):
            with self.lock:
                self.capfin.update(**kw)
        try:
            joc = self._game_running()     # cât rulează League of Legends: Whisper pe procesor, cu prioritate mică
            upd(msg=f"Fac subtitrările românești pentru {len(items)} clip(uri) " +
                ("(Whisper pe procesor, cât rulează jocul: mai încet)..." if joc else "(Whisper, ~15 s pe clip)..."))
            if joc:
                self.log("CapCut → dublaj: rulează League of Legends — subtitrările pe procesor, cu prioritate mică")
            mj_dir = TIKTOK_DIR.parent / "capcut"
            out_json = work0 / "subtitrari.json"
            errf = work0 / "subtitrari.log"
            with open(errf, "wb") as ef:
                pr = subprocess.Popen([sys.executable, "-X", "utf8", str(mj_dir / "montaj.py"), "--subtitrari",
                                       str(out_json)] + [str(f) for _, f, _ in items], cwd=str(mj_dir),
                                      stdin=subprocess.DEVNULL, stdout=ef, stderr=subprocess.STDOUT,
                                      creationflags=0x08000000 | (0x00004000 if joc else 0))   # (sub normal)
                t0 = time.time()
                limit = 300 + (900 if joc else 180) * len(items)
                while pr.poll() is None:
                    if stop.is_set() or time.time() - t0 > limit:
                        pr.kill()
                        pr.wait()
                        raise RuntimeError("oprit" if stop.is_set() else "subtitrările au durat prea mult")
                    time.sleep(1)
            if not out_json.is_file():
                tail = errf.read_text(encoding="utf-8", errors="replace")[-300:]
                raise RuntimeError(f"subtitrările nu au mers: {tail}")
            subs = json.loads(out_json.read_text(encoding="utf-8"))
            dev = subs.pop("_device", "?")
            for n, (clip, final, key) in enumerate(items, 1):
                if stop.is_set():
                    break
                segs = subs.get(str(final))
                if not isinstance(segs, list):
                    why = segs.get("error") if isinstance(segs, dict) else "lipsă"
                    skipped.append(f"{clip} (subtitrări: {why})")
                    continue
                upd(msg=f"{clip} ({n}/{len(items)}): îl pun la coada video...")
                c = self.snapshot()
                work = engine.work_dir(Path(clip), c)
                video, audio = work / "capcut_video.mp4", work / "capcut_voce.m4a"

                def drop_files():                          # doar fișierele mele (folderul poate fi al unui dublaj normal)
                    for p_ in (video, audio):
                        try:
                            p_.unlink(missing_ok=True)       # (montajul din capcut_final rămâne neatins)
                        except OSError:
                            pass
                if (work / engine.RENDER_FILE).is_file():
                    skipped.append(f"{clip} (e deja la coada video)")
                    continue
                queued = False
                try:
                    work.mkdir(parents=True, exist_ok=True)
                    drop_files()                           # resturi de la o încercare întreruptă
                    try:
                        os.link(final, video)              # același disc: fără copie
                    except OSError:
                        shutil.copyfile(final, video)
                    r = subprocess.run([randare.find_ffmpeg()[0], "-v", "error", "-y", "-i", str(final), "-vn", "-map",
                                        "0:a:0", "-c:a", "copy", str(audio)], capture_output=True, timeout=300,
                                       creationflags=0x08000000)
                    if r.returncode != 0 or not audio.is_file():
                        raise RuntimeError("nu pot scoate vocea din montaj")
                    acc = engine.assign_account(Path(clip), c)       # contul abia când fișierele sunt gata
                    if acc:
                        reg = self._capfin_registry()
                        mine = reg.get("_conturi") if isinstance(reg.get("_conturi"), dict) else {}
                        mine[clip] = time.strftime("%Y-%m-%d %H:%M:%S")
                        reg["_conturi"] = mine
                        self._capfin_save(reg)
                    out = engine.output_path(Path(clip), c)
                    if out.exists():
                        drop_files()
                        skipped.append(f"{clip} (e deja în dublate: {out.name})")
                        reg = self._capfin_registry()
                        reg[key] = {"clip": clip, "out": str(out), "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "stare": "existent"}
                        self._capfin_save(reg)
                        continue
                    try:            # montaj „doar finalul” (după raportul lui): pauzele rămân și în video-ul final
                        fara = json.loads((CAPCUT_FINAL / "rapoarte" / f"{key}.json").read_text(
                            encoding="utf-8")).get("mod") == "doar_final"
                    except (OSError, ValueError, AttributeError):
                        fara = False
                    zona = (c.get("sterge_zone_clip") or {}).get(clip) or None
                    if zona is None and c.get("sterge_sub_orig") and c.get("zona_api"):
                        upd(msg=f"{clip} ({n}/{len(items)}): agentul caută subtitrările originale (API)...")
                        zona = self._capfin_zone(final, segs, c, stop, clip)
                    job = {"video": str(video), "audio": str(audio), "out": str(out), "work": str(work), "clip": clip,
                           "teaser_id": None, "zona": zona,
                           "segments": segs, "capcut": True, "fara_pauze": fara}
                    if doar_pc or engine.vmake_at_render(c):
                        # ales „Pe PC”, sau Vmake (AI-ul PC-ului întâi, apoi Vmake): nu-l dau unui GPU închiriat
                        job["doar_pc"] = True
                    engine.save_render_job(job, 0.0, {"translator": "CapCut"})
                    if not self._requeue_render(clip, job, 0.0):
                        engine.drop_render_job(work)
                        drop_files()
                        skipped.append(f"{clip} (e în lucru)")
                        continue
                    queued = True
                except Exception as e:  # noqa: BLE001 - doar clipul ăsta: restul merg mai departe
                    if not queued:
                        engine.drop_render_job(work)
                        drop_files()
                    skipped.append(f"{clip} ({str(e)[:120] or e.__class__.__name__})")
                    continue
                reg = self._capfin_registry()
                reg[key] = {"clip": clip, "cont": (acc or {}).get("cont"), "nr": (acc or {}).get("nr"), "out": str(out),
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "stare": "la randare", "unde": "pc" if doar_pc else "app"}
                self._capfin_save(reg)
                sent += 1
                upd(done=sent)
                self.log(f"CapCut → dublaj {clip}: {len(segs)} replici românești ({dev}), "
                         f"{'cont ' + acc['cont'] if acc else 'fără cont'} → {out.name}; video-ul "
                         f"{'pe PC' if doar_pc else 'ca în aplicație'}")
            msg = (f"Oprit după {sent} clip(uri)." if stop.is_set() else
                   f"Gata: {sent} clip(uri) trimise la randare (subtitrări, upscale, acoperire, watermark) → apar în "
                   f"dublate ca @cont_NNN.mp4 când e gata video-ul.")
            if skipped:
                msg += f" Sărite: {len(skipped)} — " + "; ".join(skipped[:3]) + ("…" if len(skipped) > 3 else "")
        except Exception as e:  # noqa: BLE001
            msg = f"Nu a mers: {e}"
            self.log(f"CapCut → dublaj: {msg}")
        finally:
            shutil.rmtree(work0, ignore_errors=True)
        self._capfin_new = (0.0, [])
        upd(busy=False, msg=msg)

    def _pick_render_pod(self, tried):
        """A rented GPU (with the video worker) for a clip waiting in the PC queue. The PC card is
        usually full (VoiceStudio + the local translator) and Real-ESRGAN there is very slow.
        None = no such GPU (this PC makes it); "wait" = they all have a PC-queue video already
        (one at a time per GPU; its own background video counts as load too)."""
        c = self.snapshot()
        if not c.get("video_pe_gpu"):
            return None
        now = time.time()
        with self.lock:
            cands = [dict(m) for m in self.settings["masini"]
                     if m["activ"] and m["id"] != "local" and engine.rented_info(m) and engine.worker_url(m)
                     and m["id"] not in tried and m["id"] not in self.dead
                     and now - self.no_worker.get(m["id"], 0) > 30 * 60]
            if not cands:
                return None
            free = [m for m in cands if m["id"] not in self.rq_pods]
            if not free:
                return "wait"

            def load(m):
                bgv = self.bg.get(m["id"])
                return (1 if bgv and bgv[0].is_alive() else 0) + \
                    sum(1 for r in self.renders.values() if r.get("remote") == m["nume"])
            free.sort(key=load)          # snapshot taken under the lock (renders changes all the time)
            m = free[0]
            self.rq_pods[m["id"]] = {"clip": None, "cancel": threading.Event()}
            return m

    def _remote_render(self, m, name, job, t_dub, tried):
        """One PC-queue video on one rented GPU, in its own thread (several GPUs in parallel)."""
        mid = m["id"]
        with self.lock:
            slot = self.rq_pods[mid]
            slot["clip"] = name
            self.renders[name] = {"percent": 0.0, "running": True, "remote": m["nume"]}

        def progress(p):
            with self.lock:
                if name in self.renders:
                    self.renders[name]["percent"] = p
        t0 = time.time()
        ok = False
        try:
            self.log(f"{name}: video-ul il face {m['nume']} (nu PC-ul)...")
            info = engine.RemoteVideo(m).render(job, self.snapshot(), progress=progress,
                                                cancel=slot["cancel"], log=lambda msg: self.log(msg, m))
            ok = True
            self._render_ok(name, job, info, t_dub, t0)
        except Exception as e:  # noqa: BLE001 - next GPU, or this PC
            if "nu raspunde" in str(e) or "refuzat" in str(e):
                self.no_worker[mid] = time.time()
            self.log(f"{name}: {m['nume']} nu a putut face video-ul ({str(e)[:200] or 'oprit'})")
            with self.lock:
                self.renders[name] = {"percent": None, "running": False}
            self.render_q.put((name, job, t_dub, set(tried) | {mid}))
        finally:
            with self.lock:
                if self.rq_pods.get(mid) is slot:
                    self.rq_pods.pop(mid, None)
                if ok:
                    self.renders.pop(name, None)
            self.rq_free.set()
            self.wake.set()
            if ok:
                self._maybe_done()

    @staticmethod
    def _voice_msg(vz):
        """vocea 205.6s -> fara pauze 171.2s -> 180.0s (viteza 1.00x: incape fara accelerare)"""
        vl = vz.get("voice_len")
        f = float(vz.get("factor") or 1.0)
        if vl is None:
            return f"vocea {vz['before']:.1f}s -> {vz['after']:.1f}s (viteza {f:.2f}x)"
        why = (f"accelerata {f:.2f}x, cu pitch" if f > 1.0005 else
               "incape fara accelerare, restul e liniste la final")
        return f"vocea {vz['before']:.1f}s -> fara pauze {vl:.1f}s -> {vz['after']:.1f}s, cat clipul ({why})"

    def _render_ok(self, name, job, info, t_dub, t0):
        with self.lock:
            self.render_info["encoder"] = info.get("encoder")
            self.times[name] = t_dub + (time.time() - t0)
            self._count_done()
        vz = info.get("voce")
        if vz:
            self.log(f"{name}: {self._voice_msg(vz)}")
        pz = info.get("pauze")
        if pz:
            self.log(f"{name}: dupa dublare am taiat {pz['cuts']} pauze: "
                     f"{pz['before']:.1f}s -> {pz['after']:.1f}s")
        if info.get("ai"):
            self.log(f"{name}: subtitrarile originale: {info['ai']}")
        if info.get("up"):
            self.log(f"{name}: marit la {info.get('w')}x{info.get('h')} cu {info['up']}")
        self.log(f"{name}: video gata in {time.time() - t0:.0f} s ({info.get('encoder')}) "
                 f"-> {Path(job['out']).name}")

    def _maybe_done(self):
        """"Gata." once everything (dubbing, videos on the GPUs, renders) is finished."""
        with self.lock:
            idle = (not self.workers and not self.renders and not self.bg and not self.rq_pods
                    and self.render_q.empty() and self.local_q.empty())
            if not idle or self._gata_logged == self.run_done:
                return
            self._gata_logged = n = self.run_done
        self.log(f"Gata. {n} clipuri dublate in sesiunea asta.")

    def _wait_machine(self, d, machine, limit):
        """A GPU that stops answering is often only busy (a long request blocks it for a
        few minutes, RunPod's proxy then gives 524). Wait for it before giving up."""
        t = time.time()

        def give_up():
            with self.lock:
                return self.stop_after or machine["id"] in self.dead
        while time.time() - t < limit:
            if give_up():
                return False
            if d.check_access() is None:
                return True
            if give_up():
                return False
            self.log(f"   placa e ocupata / nu raspunde, astept ({int(time.time() - t)}s din {limit}s)...", machine)
            for _ in range(20):          # wake up early when the pod is declared dead
                if give_up():
                    return False
                time.sleep(1)
        return not give_up() and d.check_access() is None

    def _remote_video(self, machine, mid, video, job, c, d, t0, background=False):
        """Final video on the rented GPU itself. False = could not: the PC makes it.
        background=True: the GPU already dubs its next clip meanwhile, so this shows up in the
        render list (not as the machine's stage) and a failure hands the clip to the PC queue."""
        name = video.name
        with self.lock:
            self.renders[name] = {"percent": 0.0, "running": True, "remote": machine["nume"],
                                  "inline": not background}

        def progress(p):
            with self.lock:
                if name in self.renders:
                    self.renders[name]["percent"] = p
            if not background:
                self._stage(mid, "video", p)
        t1 = time.time()
        try:
            rv = engine.RemoteVideo(machine)
            info = rv.render(job, c, progress=progress, cancel=d.cancel, log=lambda m: self.log(m, machine))
        except engine.Cancelled:
            with self.lock:
                self.renders.pop(name, None)
                if not background and mid in self.dead:
                    # the GPU died / was stopped while making the video: the voice is done -> PC queue
                    self.renders[name] = {"percent": None, "running": False}
                    self.render_q.put((name, job, time.time() - t0, {mid}))
                    return True
            raise
        except Exception as e:  # noqa: BLE001
            with self.lock:
                if background:
                    # straight into the PC queue (no moment where another GPU could grab the clip)
                    self.renders[name] = {"percent": None, "running": False}
                    self.render_q.put((name, job, time.time() - t0))
                else:
                    self.renders.pop(name, None)
            if "nu raspunde" in str(e) or "refuzat" in str(e):
                # no video worker on this pod (made by an older version / without it): don't retry every clip
                self.no_worker[mid] = time.time()
                self.log(f"   {machine['nume']} nu are lucratorul video ({e}). Video-ul clipurilor lui se face "
                         "pe calculatorul tau. Ca sa-l aiba: Opreste (sterge pod-ul) si porneste-l din nou.", machine)
            else:
                self.log(f"   video-ul nu s-a putut face pe GPU ({e}); il fac pe calculatorul tau.", machine)
            return False
        with self.lock:
            self.renders.pop(name, None)
            self.times[name] = time.time() - t0
            self._count_done()
            self.results.setdefault(name, {})
        for k, label in (("ai", "subtitrari originale / marire"),):
            if info.get(k):
                self.log(f"   {label}: {info[k]}", machine)
        vz = info.get("voce")
        if vz:
            self.log(f"   {self._voice_msg(vz)}", machine)
        self.log(f"   GATA in {(time.time() - t0) / 60:.1f} min (video pe GPU in {(time.time() - t1) / 60:.1f} min, "
                 f"{info.get('up') or 'fara marire'}) -> {Path(job['out']).name}", machine)
        return True

    def _wait_bg(self, mid):
        """At most one video waiting per GPU: wait for the previous clip's video on this card. (It can't wait
        forever: a video job on the GPU that stops moving is given up by RemoteVideo.render.)"""
        with self.lock:
            cur = self.bg.get(mid)
        if not (cur and cur[0].is_alive()):
            return
        self.log("   astept sa se termine video-ul clipului anterior pe placa asta...", self._row(mid))
        while cur[0].is_alive():
            cur[0].join(2)
            with self.lock:
                if mid in self.dead:
                    return
                j = self.jobs.get(mid)
                if j is not None:
                    j["moved"] = time.time()     # waiting for the card is not a stuck step

    def _bg_video(self, machine, mid, video, job, c, d, t0):
        try:
            self._remote_video(machine, mid, video, job, c, d, t0, background=True)
        except engine.Cancelled:
            with self.lock:
                dead = mid in self.dead
                if dead and video.name not in self.renders:
                    # the GPU died / was stopped: the voice is done, only the video is left -> PC queue
                    self.renders[video.name] = {"percent": None, "running": False}
                    self.render_q.put((video.name, job, time.time() - t0, {mid}))
            self.log(f"   {video.name}: " + ("video-ul trece pe alta placa / pe PC" if dead else "video anulat"),
                     machine)
        except Exception as e:  # noqa: BLE001
            self.log(f"   {video.name}: video-ul pe GPU a esuat ({e})", machine)
            with self.lock:
                if video.name not in self.renders:
                    self.renders[video.name] = {"percent": None, "running": False}
                    self.render_q.put((video.name, job, time.time() - t0))
        finally:
            with self.lock:
                if self.bg.get(mid, (None,))[0] is threading.current_thread():
                    self.bg.pop(mid, None)
            self.wake.set()
            self._maybe_done()

    def _busy_clips(self):
        return ({j["clip"] for j in self.jobs.values() if j.get("clip")} | set(self.renders)
                | {p["clip"] for p in self.prefetch.values()})

    @staticmethod
    def _lang_tag(c):
        """' (EN)' when the clips are dubbed into more than one language, else ''."""
        return f" ({c['limba'].upper()})" if len(engine.target_langs(c)) > 1 else ""

    def _start_prefetch(self, machine, mid, c, voice_id):
        """While this GPU makes the voice of the current clip: cut, upload, transcribe and translate
        the next one (the translation runs on this PC, the GPU would otherwise wait for it)."""
        if not c.get("pregatire_urmator", True) or not engine.rented_info(machine):
            return None
        with self.lock:
            if self.stop_after or mid in self.dead:
                return None
            need = max(1, len(self.workers))
            free = self._claimable(c, need)
            # Near the end of the queue don't hold a clip for later: a board that becomes free
            # meanwhile (or this PC) should take it now. Prefetch only when there are at least
            # as many free clips as boards working.
            if len(free) < need:
                return None
            nxt, nlang = free[0]
            c2 = engine.lang_cfg(c, nlang)
            engine.assign_account(nxt, c2)       # its TikTok account (watermark, file name), decided once
            tag = self._lang_tag(c2)
            # (its steps and log lines are signs of life too: a prefetch that stops moving is aborted)
            d2 = engine.Dubber(c2, log=lambda m: self._pre_log(mid, d2, f"   [{nxt.name}{tag}] {m.strip()}", machine),
                               stage=lambda st, pct=None: self._pre_stage(mid, d2, st, pct), machine=machine)
            now = time.time()
            self.prefetch[mid] = {"clip": nxt.name, "dubber": d2, "stage": "upload", "percent": None,
                                  "started": now, "moved": now}
            self.retry_note.pop(nxt.name, None)      # (its second try starts now)
        box = {"t0": time.time(), "c": c2}

        def run():
            try:
                box["state"] = d2.prepare(nxt, engine.output_path(nxt, c2), voice_id)
            except Exception as e:  # noqa: BLE001 - the worker then prepares it the normal way
                box["err"] = str(e) or e.__class__.__name__
                if engine.is_machine_error(e):
                    self._note_link_error(mid, time.time())
            finally:
                with self.lock:
                    p = self.prefetch.get(mid)
                    if p and p.get("dubber") is d2:
                        p["done"] = True        # waits for the card now: not a stuck step
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.log(f"   pregatesc in paralel clipul urmator: {nxt.name}{tag}", machine)
        return nxt, t, box, d2

    def _pre_stage(self, mid, d2, stage, percent=None):
        with self.lock:
            p = self.prefetch.get(mid)
            if not p or p.get("dubber") is not d2:
                return
            if stage is not None and stage != p.get("stage"):
                p["stage"], p["percent"], p["moved"] = stage, None, time.time()
            if percent is not None and percent != p.get("percent"):
                p["percent"], p["moved"] = percent, time.time()

    def _pre_log(self, mid, d2, msg, machine):
        with self.lock:
            p = self.prefetch.get(mid)
            if p and p.get("dubber") is d2:
                p["moved"] = time.time()
        self.log(msg, machine)

    # ---------------------------------------------------------- MP4 render queue
    def _render_loop(self):
        """Hands out the clips waiting for their video: to a free rented GPU (each in its own
        thread, one at a time per GPU), else to this PC (_local_render_loop, one at a time)."""
        while True:
            item = self.render_q.get()
            try:
                name, job, t_dub = item[:3]
                tried = set(item[3]) if len(item) > 3 else set()
            except Exception as e:  # noqa: BLE001 - never kill this loop
                print("render item:", e, flush=True)
                continue
            if self._capfin_paused(job):                         # oprit cu „Oprește video-urile CapCut”
                with self.lock:
                    self.renders.pop(name, None)
                continue
            if isinstance(job, dict) and job.get("doar_pc"):     # ales „Pe PC” (traducerile CapCut)
                self.local_q.put((name, job, t_dub))
                continue
            try:
                while True:
                    m = self._pick_render_pod(tried)
                    if m != "wait":
                        break
                    self.rq_free.wait(2)
                    self.rq_free.clear()
                if m is None:
                    self.local_q.put((name, job, t_dub))
                else:
                    threading.Thread(target=self._remote_render, args=(m, name, job, t_dub, tried),
                                     daemon=True).start()
            except Exception as e:  # noqa: BLE001 - never lose a clip: this PC makes it
                print("render dispatch:", e, flush=True)
                self.local_q.put((name, job, t_dub))

    def _steal_local(self, c=None):
        """Videos waiting for this PC while a rented GPU is free: that GPU takes them. (They were queued for the PC
        when no GPU could take them - e.g. before the GPUs were rented - and would otherwise wait for the slow PC
        one by one while the GPUs sit idle.)"""
        held = []                                  # cele alese „Pe PC”: rămân pentru PC
        try:
            while not self.local_q.empty():
                m = self._pick_render_pod(set())
                if not isinstance(m, dict):
                    return
                try:
                    name, job, t_dub = self.local_q.get_nowait()
                except queue.Empty:
                    with self.lock:
                        self.rq_pods.pop(m["id"], None)
                    return
                if isinstance(job, dict) and job.get("doar_pc"):
                    held.append((name, job, t_dub))
                    with self.lock:
                        self.rq_pods.pop(m["id"], None)
                    continue
                threading.Thread(target=self._remote_render, args=(m, name, job, t_dub, set()), daemon=True).start()
        finally:
            for it in held:
                self.local_q.put(it)

    def _local_render_loop(self):
        """One clip at a time on this PC (ffmpeg), while the GPUs already dub the next ones."""
        while True:
            name, job, t_dub = self.local_q.get()
            if self._capfin_paused(job):                         # oprit cu „Oprește video-urile CapCut”
                with self.lock:
                    self.renders.pop(name, None)
                continue
            m = None if (isinstance(job, dict) and job.get("doar_pc")) else \
                self._pick_render_pod(set())          # a GPU got free meanwhile: it takes this one
            if isinstance(m, dict):
                threading.Thread(target=self._remote_render, args=(m, name, job, t_dub, set()), daemon=True).start()
                continue
            try:
                self._local_render(name, job, t_dub)
            except Exception as e:  # noqa: BLE001 - never kill this loop (the next videos wait for it)
                print("video pe PC:", e, flush=True)

    def _local_render(self, name, job, t_dub):
        stop = threading.Event()                  # „Oprește video-urile CapCut” îl oprește (și Vmake-ul lui)
        with self.lock:
            self.renders[name] = {"percent": 0.0, "running": True}
            self.render_cancel[name] = (job, stop)

        def progress(p):
            with self.lock:
                if name in self.renders:
                    self.renders[name]["percent"] = p
        t0 = time.time()
        again = False
        # o traducere CapCut cât rulează League of Legends: fără placa video (procesor, prioritate mică)
        joc = isinstance(job, dict) and bool(job.get("capcut")) and self._game_running()
        if hasattr(randare, "_tls"):
            randare._tls.low = joc
        self.log(f"{name}: randez video-ul (voce + subtitrari + watermark)"
                 + (" — rulează jocul: pe procesor, cu prioritate mică" if joc else "") + "...")
        try:
            info = engine.finish_render(job, self.snapshot(), progress=progress, cancel=stop,
                                        log=lambda m: self.log(f"{name}: {m}"))
            self._render_ok(name, job, info, t_dub, t0)
        except Exception as e:  # noqa: BLE001
            if stop.is_set():                     # oprit de tine: nu e o eroare, rămâne de reluat
                self.log(f"{name}: video oprit — rămâne în dublate\\.lucru; „Pornește iar” îl reia")
            else:
                again = self._render_failed(name, f"randarea video a esuat: {e}")
        finally:
            if hasattr(randare, "_tls"):
                randare._tls.low = False
            with self.lock:
                self.render_cancel.pop(name, None)
                if again:
                    # once more: on a rented GPU when there is one (another encoder), else this PC again
                    self.renders[name] = {"percent": None, "running": False}
                    self.render_q.put((name, job, t_dub))
                else:
                    self.renders.pop(name, None)
            self.wake.set()
            self._maybe_done()

    RETRY_FAILED = 1              # a clip that fails gets this many more tries in the session
    NO_RETRY = ("nu s-a gasit vorbire",)    # the clip itself: another try gives the same result

    def _render_failed(self, name, msg):
        """The video of a clip whose voice is done failed: once more, then the clip is marked (its voice
        stays in its folder: "Reîncearcă" then makes only the video). True = try again now."""
        with self.lock:
            n = self.attempts[("video", name)] = self.attempts.get(("video", name), 0) + 1
            again = n <= self.RETRY_FAILED
            if not again:
                self.failed[name] = msg
        self.log(f"{name}: EROARE {msg}" + (". Mai încerc o dată video-ul." if again else ""))
        engine.log_error(self.snapshot(), name, msg + (" (se reincearca)" if again else ""))
        return again

    def _clip_failed(self, name, msg, c, machine=None):
        """A clip failed: the first time it goes to the end of the queue for one more try (most failures
        are a moment's trouble: the network, a GPU, the agent); the second time it keeps its error."""
        with self.lock:
            n = self.attempts[("dub", name)] = self.attempts.get(("dub", name), 0) + 1
            again = n <= self.RETRY_FAILED and not any(k in msg for k in self.NO_RETRY)
            if again:
                self.retry_note[name] = msg
            else:
                self.retry_note.pop(name, None)
                self.failed[name] = msg
        if again:
            self.log(f"   EROARE: {msg}. Îl mai încerc o dată, la sfârșitul cozii.", machine)
        else:
            self.log(f"   EROARE: {msg}" + (" (și a doua oară)" if n > 1 else ""), machine)
        engine.log_error(c, name, msg + (" (se reincearca la final)" if again else ""))

    def _requeue_render(self, name, job, t_dub, avoid=None):
        """The voice of this clip is done (its files are on this PC): only its video is left, to another GPU
        or this PC. False when another card has taken the clip meanwhile (it dubs it again)."""
        with self.lock:
            others = ({j.get("clip") for k, j in self.jobs.items() if k != avoid}
                      | {p.get("clip") for k, p in self.prefetch.items() if k != avoid} | set(self.renders))
            if name in others:
                return False
            self.renders[name] = {"percent": None, "running": False}
            self.render_q.put((name, job, t_dub, {avoid} if avoid else set()))
        self.wake.set()
        return True

    def retry(self, name=None):
        """"Reîncearcă": the clip (or every clip with an error) gets its tries back; a clip whose voice was
        already made only gets its video made again, not the whole dub."""
        with self.lock:
            names = [name] if name else list(self.failed)
            for n in names:
                self.failed.pop(n, None)
                self.attempts.pop(("dub", n), None)
                self.attempts.pop(("video", n), None)
                self.retry_note.pop(n, None)
        if names:
            self._resume_renders(only=set(names))
        self.wake.set()
        return len(names)

    def _claimable(self, c, limit=None):
        """(clip, language) pairs nobody is working on, in queue order (under lock): every clip in
        the main language first, then every clip in each extra language. A clip is never worked on
        in two languages at once (same working folder). Stops after `limit`."""
        busy = self._busy_clips()
        out, later, taken = [], [], set()
        vids = engine.list_videos(c)
        for lang in engine.target_langs(c):
            cl = engine.lang_cfg(c, lang)
            for v in vids:
                if v.name in busy or v.name in taken or v.name in self.failed or engine.output_path(v, cl).exists():
                    continue
                taken.add(v.name)
                if v.name in self.retry_note:
                    later.append((v, lang))      # failed once: its second try after all the others
                    continue
                out.append((v, lang))
                if limit and len(out) >= limit:
                    return out
        out += later
        return out[:limit] if limit else out

    def _claim(self, c, avoid=()):
        """Next (clip, language) nobody is working on (under lock). avoid: clips whose connection just broke
        on this card - they go to another card first (this one takes them only when nothing else is left), so
        its next clip tells whether the card or the clip is the problem."""
        free = self._claimable(c, 1 + len(avoid))
        return next((f for f in free if f[0].name not in avoid), free[0] if free else None)

    def _pending_videos(self, c):
        """Clips that still miss at least one language (not failed)."""
        return [v for v in engine.list_videos(c)
                if v.name not in self.failed and not engine.all_outputs_done(v, c)]

    def _scan(self):
        """Folder scan for the page (files, sizes, finished outputs), reused for ~2 s: the page
        asks every second and the folder can hold hundreds of clips."""
        c = self.snapshot()
        key = (c["folder_clipuri"], c["folder_dublate"], tuple(engine.target_langs(c)), c["format"])
        with self.lock:
            sc = self._scan_cache
        if sc and sc[0] == key and time.time() - sc[1] < 2:
            return sc[2], sc[3]
        files = []
        for v in engine.list_videos(c):
            try:
                size = v.stat().st_size
            except OSError:
                continue  # file vanished / still locked by the copy
            langs = engine.target_langs(c)
            outs = [engine.output_path(v, engine.lang_cfg(c, lg)) for lg in langs]
            have = [o.exists() for o in outs]
            files.append({"name": v.name, "size": size, "audio": engine.is_audio(v),
                          "out": outs[0].name if all(have) else None,
                          "langs": [[lg, ok] for lg, ok in zip(langs, have)] if len(langs) > 1 else None})
        ignored = engine.list_ignored(c)
        with self.lock:
            self._scan_cache = (key, time.time(), files, ignored)
        return files, ignored

    def clip_rows(self):
        files, ignored = self._scan()
        with self.lock:
            names = {m["id"]: m["nume"] for m in self.settings["masini"]}
            where = {j["clip"]: names.get(mid, mid) for mid, j in self.jobs.items() if j.get("clip")}
            rows = []
            for f in files:
                n = f["name"]
                render = self.renders.get(n)
                if n in where:
                    st = "running"
                elif render:
                    st = "rendering"
                elif f["out"]:
                    st = "done"
                elif n in self.failed:
                    st = "failed"
                else:
                    st = "waiting"
                res = self.results.get(n) or {}
                rows.append({
                    "name": n, "size": f["size"], "audio": f["audio"], "status": st,
                    "retry": self.retry_note.get(n) if st == "waiting" else None,
                    "machine": where.get(n), "error": self.failed.get(n), "secs": self.times.get(n),
                    "qc": res.get("qc"), "translator": res.get("translator"),
                    "output": f["out"], "render": dict(render) if render else None,
                    "langs": f.get("langs"),
                })
        return rows, ignored

    def start(self, join_only=False, resume=False):
        """Start the queue. join_only: only add boards switched on meanwhile to a queue that is
        already running (never a new start). resume: like a start, but keep the session counters
        (clips given back by a dead pod after the other boards had finished)."""
        c = self.snapshot()
        if c["traducator"] == "agent" and not engine.find_claude():
            self.log("EROARE: nu gasesc Claude Code pe calculator. Instaleaza-l si logheaza-te "
                     "(ruleaza 'claude' o data in terminal), apoi apasa Start din nou.")
            return False
        machines = [m for m in self.active_machines()
                    if self.mstate.get(m["id"], {}).get("online")]
        if not machines:
            if not (join_only or resume):
                self.log("Nicio placa pregatita pentru modul ales la „Unde se dubleaza”. "
                         "Porneste o placa sau schimba modul.")
            return False
        with self.lock:
            if self.workers:
                # Already running: just add machines that were switched on meanwhile.
                if self.stop_after:
                    return False
                machines = [m for m in machines if m["id"] not in self.workers]
                if not machines:
                    return False
            elif join_only or (resume and self.stop_after):
                return False
            elif resume:
                self.log("Reiau clipurile intoarse pe: " + ", ".join(m["nume"] for m in machines))
            else:
                self.stop_after, self.run_done, self.alert = False, 0, None
                self.finishing, self._gata_logged = False, None
                self.log("Pornit pe: " + ", ".join(m["nume"] for m in machines))
            for m in machines:
                self.workers.add(m["id"])
                self.dead.discard(m["id"])
        if not join_only:
            # clips whose voice is already done (an earlier stop, a GPU that died after the voice): only
            # their video is left; marked before the cards start, so none of them is dubbed again
            self._resume_renders(c)
        for m in machines:
            threading.Thread(target=self._worker, args=(m,), daemon=True).start()
        return True

    def _worker(self, machine):
        mid = machine["id"]
        pending = None     # (clip, thread, box, dubber): the next clip, prepared in the background
        try:
            while True:
                with self.lock:
                    if mid in self.dead:
                        break
                    if self.stop_after and not (self.finishing and pending is not None
                                                and not pending[3].cancel.is_set()):
                        break            # ("Termin pe azi" still finishes the clip prepared already)
                    live = next((m for m in self.settings["masini"] if m["id"] == mid), None)
                if not live or not live["activ"]:
                    self.log("oprita din lista, nu mai primeste clipuri.", machine)
                    break
                if not engine.machine_allowed(live, self.snapshot()["sursa"]):
                    self.log("nu e folosita in modul ales la „Unde se dubleaza”; nu mai ia clipuri noi.", machine)
                    break
                machine = live
                c = self.snapshot()
                # (every log line of this clip also counts as a sign of life for the stuck-step check)
                d = engine.Dubber(c, log=lambda msg: self._job_log(mid, d, msg, machine),
                                  stage=lambda st, pct=None: self._stage(mid, st, pct), machine=machine)
                err = d.check_access()
                if err:
                    self.log(f"{machine['nume']}: {err}. Clipurile trec pe celelalte placi.")
                    break
                try:
                    voice_id = d.voice_id()
                except Exception as e:  # noqa: BLE001
                    self.log(f"EROARE: {e} pe {machine['nume']}. Vocea salvata trebuie sa existe "
                             "pe fiecare placa, sau alege Auto (clona din clip).")
                    break
                state = None
                if pending is not None:
                    video, pth, pbox, d2p = pending
                    pending = None
                    c = pbox.get("c") or c
                    d.c = c
                    if pth.is_alive():
                        self.log(f"   astept pregatirea lui {video.name}...", machine)
                    while pth.is_alive():
                        pth.join(2)
                        with self.lock:
                            p = self.prefetch.get(mid) or {}
                            if mid in self.dead or (p.get("stuck") and time.time() - p["stuck_at"] > 120):
                                break    # (aborted, but stuck in a call that ignores it: it is let go)
                            stopping = self.stop_after and not self.finishing
                        if stopping:
                            # stopped meanwhile: the prepared clip won't be used, its GPU work stops now
                            threading.Thread(target=d2p.abort, daemon=True).start()
                            break
                    with self.lock:
                        if mid in self.dead:
                            break        # the pod died: the clip was given back already
                        if self.stop_after and (not self.finishing or d2p.cancel.is_set()):
                            break        # stopped / cancelled meanwhile: the clip goes back to the queue
                        pre = self.prefetch.pop(mid, None) or {}
                        stuck_pre = pre.get("stuck") if pbox.get("state") is None else None
                        if not stuck_pre:
                            self.jobs[mid] = {"clip": video.name, "stage": "voce", "percent": None,
                                              "started": pbox["t0"], "moved": time.time(), "dubber": d,
                                              "lang": c["limba"]}
                    if stuck_pre:
                        # its preparation stopped moving and was aborted (_stuck_check): one more try later
                        if self._after_stuck(mid, machine, video, c, stuck_pre, None, pbox["t0"]):
                            break
                        continue
                    state = pbox.get("state")
                    t0 = pbox["t0"]
                    if state is None:
                        self.log(f"   pregatirea in paralel a lui {video.name} nu a mers ({pbox.get('err')}); "
                                 "il iau de la capat", machine)
                        t0 = time.time()
                else:
                    with self.lock:
                        got = None if mid in self.dead else self._claim(c, [k for k, _ in self.dropped.get(mid, ())])
                        if got is None:
                            break
                        video, lang = got
                        c = engine.lang_cfg(c, lang)
                        engine.assign_account(video, c)    # its TikTok account (watermark, file name), decided once
                        d.c = c
                        self.jobs[mid] = {"clip": video.name, "stage": "upload", "percent": None,
                                          "started": time.time(), "dubber": d, "lang": lang}
                        self.retry_note.pop(video.name, None)     # (its second try starts now)
                    t0 = time.time()
                self.log(f"{video.name}{self._lang_tag(c)}" + (" (transcris si tradus deja)" if state else ""), machine)
                saved = None       # this clip's video job once its voice is done (kept on disk: render.json)
                try:
                    if state is None:
                        state = d.prepare(video, engine.output_path(video, c), voice_id)
                    self._check_dead(mid)
                    pending = self._start_prefetch(machine, mid, c, voice_id)
                    info = d.finish(state) or {}
                    job = info.pop("render", None)
                    if job:
                        # from here on only the video is left: a crash / restart / dead GPU won't cost the voice
                        engine.save_render_job(job, time.time() - t0, info)
                        saved = job
                    self._check_dead(mid)
                    if d.cancel.is_set():
                        raise engine.Cancelled()     # stopped right as its last step ended (stuck check, Anuleaza)
                    with self.lock:
                        self.stuck_n[mid] = 0        # this card got a clip through: it works
                    if (job and c.get("video_pe_gpu") and engine.rented_info(machine) and engine.worker_url(machine)
                            and time.time() - self.no_worker.get(mid, 0) > 30 * 60):
                        # option B: this GPU also makes the final video; the PC only downloads it
                        with self.lock:
                            self.results[video.name] = info
                            self.machine_errors[mid] = 0
                            self.dropped.pop(mid, None)
                        if c.get("video_in_fundal", True):
                            # the video runs on this GPU in the background while it dubs the next clip
                            self._wait_bg(mid)           # at most one video waiting per GPU
                            with self.lock:
                                self.renders[video.name] = {"percent": 0.0, "running": True,
                                                            "remote": machine["nume"]}
                            # (its own cancel: a late abort of this clip's step must not stop its video)
                            bgd = types.SimpleNamespace(cancel=threading.Event())
                            t = threading.Thread(target=self._bg_video, args=(machine, mid, video, job, c, bgd, t0),
                                                 daemon=True)
                            with self.lock:
                                self.bg[mid] = (t, bgd)
                            t.start()
                            self.log("   vocea e gata; video-ul se face pe GPU in timp ce trec la clipul urmator",
                                     machine)
                            continue
                        if self._remote_video(machine, mid, video, job, c, d, t0):
                            job = None
                            continue
                    with self.lock:
                        self.times[video.name] = time.time() - t0
                        self.results[video.name] = info
                        if job:
                            # hand over to the local ffmpeg queue; this GPU takes the next clip
                            self.renders[video.name] = {"percent": None, "running": False}
                            self.render_q.put((video.name, job, time.time() - t0))
                        else:
                            self._count_done()
                    self.log(f"   GATA in {(time.time() - t0) / 60:.1f} min"
                             + (" (urmeaza randarea video pe calculatorul tau)" if job else ""), machine)
                    with self.lock:
                        self.machine_errors[mid] = 0
                        self.dropped.pop(mid, None)
                except engine.AgentUnavailable as e:
                    # Not the clip's fault: leave it waiting and pause the whole queue.
                    with self.lock:
                        self.alert = str(e)
                        self.stop_after = True
                    self.log(f"   OPRIT: {e}", machine)
                    self.log("Coada e pe pauza. Clipul ramane in asteptare si se reia de la inceput.")
                except engine.Cancelled:
                    stuck = self._take_stuck(mid, d)
                    with self.lock:
                        dead_now = mid in self.dead
                    if stuck and not dead_now:          # (a dead card: its clips went back already)
                        if self._after_stuck(mid, machine, video, c, stuck, saved, t0):
                            break
                        continue
                    with self.lock:
                        machine_only = mid in self.machine_stop or mid in self.dead
                        self.machine_stop.discard(mid)
                        if not machine_only:
                            self.stop_after = True
                    if machine_only:
                        if saved is not None and self._requeue_render(video.name, saved, time.time() - t0, mid):
                            self.log("   Placa a fost oprita dupa voce; video-ul se face in alta parte.", machine)
                        else:
                            self.log("   Placa a fost oprita; clipul se intoarce in coada.", machine)
                        break
                    self.log("   Anulat.", machine)
                except Exception as e:  # noqa: BLE001 - one bad clip must not stop the rest
                    stuck = self._take_stuck(mid, d)
                    with self.lock:
                        dead_now = mid in self.dead
                    if stuck and not dead_now:
                        if self._after_stuck(mid, machine, video, c, stuck, saved, t0):
                            break
                        continue
                    msg = str(e) or e.__class__.__name__
                    with self.lock:
                        dead = mid in self.dead
                    if dead:
                        # the pod died: not the clip's fault, and another board may already have it
                        if saved is not None and self._requeue_render(video.name, saved, time.time() - t0, mid):
                            self.log("   Placa nu mai exista, dar vocea e gata; video-ul se face in alta parte.",
                                     machine)
                        else:
                            self.log("   Placa nu mai exista; clipul se intoarce in coada.", machine)
                        break
                    if "model_missing" in msg:
                        # the GPU has no models (its install at start failed): not the clip's fault
                        with self.lock:
                            self.jobs.pop(mid, None)
                            tries = self.machine_errors[mid] = self.machine_errors.get(mid, 0) + 1
                        if tries > 2:
                            self.log("   placa tot nu are modelele; nu mai ia clipuri. Apasa „Instaleaza modelele”.",
                                     machine)
                            break
                        self.log("   placa nu are modelele instalate; le instalez acum, clipul asteapta...", machine)
                        try:
                            d.install_models(progress=lambda m: self._stage(mid, None))
                            self.log("   modele instalate, reiau clipul.", machine)
                        except Exception as e2:  # noqa: BLE001
                            self.log(f"   instalarea modelelor nu a mers ({e2}); placa nu mai ia clipuri.", machine)
                            break
                        continue
                    if "No space left on device" in msg or "Errno 28" in msg:
                        with self.lock:
                            self.alert = ("Discul calculatorului e plin (No space left on device). Fa loc pe disc, "
                                          "apoi apasa Start din nou; clipurile se reiau.")
                            self.stop_after = True
                        self.log("   OPRIT: discul e plin. Clipul ramane in asteptare.", machine)
                        engine.cleanup_work(c)
                        break
                    if engine.is_machine_error(e):
                        t_err = time.time()
                        self._note_link_error(mid, t_err)
                        with self.lock:
                            stage = (self.jobs.get(mid) or {}).get("stage")
                            self.jobs.pop(mid, None)
                            tries = self.machine_errors[mid] = self.machine_errors.get(mid, 0) + 1
                        self.wake.set()
                        answers = self._wait_machine(d, machine, 300 if tries <= 3 else 60)
                        if answers and self._dropped_clip(mid, machine, video.name, stage, msg, t_err):
                            # it answers, yet keeps breaking clips: deleted (a Vast.ai one is replaced). Its clip is
                            # back in the queue: cards that had already run out of clips take it
                            with self.lock:
                                go_on = not self.stop_after
                            if go_on:
                                threading.Timer(3, self.start, kwargs={"resume": True}).start()
                            break
                        if tries <= 3 and answers:
                            self.log(f"   Legatura cu placa s-a intrerupt ({msg[:120]}). Placa raspunde, "
                                     f"reiau clipul ({tries}/3).", machine)
                            continue
                        if answers:
                            self.log(f"   Legatura cu placa s-a intrerupt de {tries} ori la rand ({msg[:120]}). "
                                     "Clipul se intoarce in coada.", machine)
                        else:
                            self.log(f"   {machine['nume']} nu mai raspunde ({msg[:120]}). "
                                     "Clipul se intoarce in coada.", machine)
                        break
                    if saved is not None and self._requeue_render(video.name, saved, time.time() - t0, mid):
                        self.log(f"   EROARE dupa voce ({msg[:200]}); vocea e gata, video-ul se face in alta parte.",
                                 machine)
                        continue
                    shutil.rmtree(engine.work_dir(video, c), ignore_errors=True)   # no half files filling the disk
                    self._clip_failed(video.name, msg, c, machine)
                finally:
                    with self.lock:
                        if self.jobs.get(mid, {}).get("dubber") is d:
                            self.jobs.pop(mid, None)
                        if (self.stuck.get(mid) or {}).get("dubber") is d:
                            self.stuck.pop(mid, None)    # (its step ended by itself right as it was aborted)
        finally:
            if pending is not None:
                # this GPU stops: the prepared clip goes back to the queue
                pending[3].abort()
            with self.lock:
                self.prefetch.pop(mid, None)
                self.workers.discard(mid)
                was_dead = mid in self.dead
                self.dead.discard(mid)
                last = not self.workers
                stopped = self.stop_after
                live = next((m for m in self.settings["masini"] if m["id"] == mid), None)
            self.wake.set()
            if was_dead and live and live["activ"] and not stopped and not last:
                # the pod was restarted while this worker was still stuck on the old one
                threading.Timer(3, self.start, kwargs={"join_only": True}).start()
            if last:
                with self.lock:
                    rendering = len(self.renders)
                if stopped:
                    self.log("Oprit." + (f" Se termina randarea a {rendering} clipuri deja dublate."
                                         if rendering else ""))
                elif rendering:
                    self.log(f"Dublarea s-a terminat; se mai randeaza {rendering} clip(uri) video.")
                else:
                    left = len([v for v in self._pending_videos(self.snapshot())
                                if v.name not in self.renders])
                    if left:
                        self.log(f"Oprit: mai sunt {left} clipuri, dar nicio placa nu le mai poate lua.")
                    else:
                        self._maybe_done()

    def _check_dead(self, mid):
        with self.lock:
            if mid in self.dead:
                raise engine.Cancelled()

    def _job_log(self, mid, d, msg, machine):
        """A log line of the clip a card works on: also a sign of life for the stuck-step check."""
        with self.lock:
            j = self.jobs.get(mid)
            if j is not None and j.get("dubber") is d:
                j["moved"] = time.time()
        self.log(msg, machine)

    def _take_stuck(self, mid, d):
        """The stuck-step record of this card's current clip (and it is consumed), or None."""
        with self.lock:
            s = self.stuck.get(mid)
            if s and s.get("dubber") is d:
                return self.stuck.pop(mid)
        return None

    def _after_stuck(self, mid, machine, video, c, stuck, saved, t0):
        """A step of this clip stopped moving and was aborted (_stuck_check). The clip: only its video when
        the voice was done, else one more try later. The card: STUCK_BROKEN clips in a row stuck on it = it is
        broken (a rented one is deleted, and replaced on Vast.ai). True = this card's worker stops."""
        name = video.name
        with self.lock:
            # (a card stuck for a long time may have had its clip taken away and given to another one)
            elsewhere = (any(j.get("clip") == name for k, j in self.jobs.items() if k != mid)
                         or any(p.get("clip") == name for k, p in self.prefetch.items() if k != mid)
                         or name in self.renders)
        if saved is not None and self._requeue_render(name, saved, time.time() - t0, mid):
            self.log(f"   {stuck['why']}; vocea era gata, video-ul se face in alta parte.", machine)
        elif not elsewhere:
            if saved is None:
                shutil.rmtree(engine.work_dir(video, c), ignore_errors=True)
            self._clip_failed(name, stuck["why"], c, machine)
        if stuck.get("stage") in self.NOT_CARD_STAGES:
            return False             # done on this PC / its network, not on the card: not the card's fault
        with self.lock:
            n = self.stuck_n[mid] = self.stuck_n.get(mid, 0) + 1
        if n < self.STUCK_BROKEN:
            return False
        self._stuck_machine(mid, machine, stuck["why"])
        return True

    def _note_link_error(self, mid, t):
        """The link to a rented card broke (in any step, counted against it or not): several cards losing it
        around the same time means this PC's internet, not the card (see _dropped_clip)."""
        if mid == "local":
            return
        with self.lock:
            self.link_errs[:] = [x for x in self.link_errs if t - x[0] < 900] + [(t, mid)]

    def _dropped_clip(self, mid, machine, clip, stage, msg, t_err):
        """The card answers again, but the link to it broke in the middle of a clip (its VoiceStudio crashes in
        that step, a bad host's network...). The same on DROP_BROKEN different clips in a row: the card is
        broken. A rented one is deleted (and replaced on Vast.ai, never on the same host) instead of taking a
        clip every minute, giving it back and billing in between. The same clip again counts once (it may be the
        clip); other cards losing their link at the same times = this PC's internet (once per DROP_EXCUSE_S).
        True = this card's worker stops."""
        if mid == "local" or not engine.rented_info(self._row(mid)):
            return False
        # (a stream from the card broke: the card's side, even while the step is still called "upload")
        card_stream = msg.startswith(("legatura cu placa s-a intrerupt", "conexiunea s-a inchis"))
        if not card_stream and (stage in self.NOT_CARD_STAGES or re.search(r"\bHTTP \d{3}\b", msg)):
            return False             # this PC's upload / its internet, or a proxy answer (524: only busy)
        with self.lock:
            seen = self.dropped.setdefault(mid, [])
            seen.append((clip, t_err))
            if len({c for c, _ in seen}) < self.DROP_BROKEN:
                return False
            times = [t for _, t in seen]
            n = len({c for c, _ in seen})
            may_excuse = time.time() - self.excused.get(mid, 0) > self.DROP_EXCUSE_S
        # the cards that lost their link at the same moment may only notice a little later
        end = time.time() + (self.DROP_GRACE_S if may_excuse else 0)
        while time.time() < end:
            with self.lock:
                if self.stop_after or mid in self.dead:
                    return False
            time.sleep(1)
        with self.lock:
            others = [t for t, m in self.link_errs if m != mid]
            excuse = may_excuse and any(abs(t - d) <= self.DROP_SAME_TIME_S for d in times for t in others)
            self.dropped.pop(mid, None)
            if excuse:
                self.excused[mid] = time.time()
            else:
                self.machine_errors.pop(mid, None)
                host = self.pods.get(mid, {}).get("machine_id")
        if excuse:
            self.log("   Legatura a picat in acelasi timp si la alte placi: e internetul calculatorului, nu placa.",
                     machine)
            return False
        if host is not None and (engine.rented_info(self._row(mid)) or {}).get("provider") == "vast":
            engine.Vast.BAD_MACHINES.add(host)      # its replacement is never rented on this host again
        self._stuck_machine(mid, machine, msg, reason=f"îi pică legătura în mijlocul clipului, la {n} clipuri "
                                                      f"diferite la rând ({msg[:100]})")
        return True

    def _stuck_machine(self, mid, machine, why, reason=None):
        with self.lock:
            self.stuck_n.pop(mid, None)
        name = (machine or {}).get("nume") or mid
        reason = reason or f"s-a blocat la {self.STUCK_BROKEN} clipuri la rând (ultimul: {why})"
        info = engine.rented_info(self._row(mid))
        if mid == "local" or not info:
            with self.lock:
                self.alert = (f"{name} {reason}. Nu mai primește clipuri: repornește VoiceStudio (sau "
                              "calculatorul), apoi apasă Start.")
            self.log(f"{name}: {reason}. Nu mai primește clipuri.")
            return
        if info["provider"] == "vast":
            self._vast_dead(mid, f"{reason}; o șterg")        # and another one is rented while the queue runs
            return
        self._pod_dead(mid, f"{reason}; îl șterg")
        if not engine.rented_info(self._row(mid)):
            return
        err = self.pod_action(mid, "stop")                   # a broken pod would bill for nothing
        if err and "deja" not in err and "niciun pod" not in err:
            self.log(f"{name}: nu am putut șterge pod-ul blocat ({err}); șterge-l tu de pe runpod.io.")

    def _stage(self, mid, stage, percent=None):
        with self.lock:
            j = self.jobs.get(mid)
            if not j:
                return
            if stage is not None:
                if stage != j.get("stage"):
                    j["moved"] = time.time()       # a new step started: the "stuck" clock restarts
                    if j.get("stage") == "transcriere":
                        self.fresh.pop(mid, None)  # its first transcription went through: not "broken at start"
                j["stage"], j["percent"] = stage, None
            if percent is not None:
                pct = max(0.0, min(100.0, percent))
                if pct != j.get("percent"):
                    j["moved"] = time.time()       # progress
                j["percent"] = pct

    def cancel_now(self):
        with self.lock:
            self.stop_after = True
            dubbers = ([j["dubber"] for j in self.jobs.values() if j.get("dubber")]
                       + [p["dubber"] for p in self.prefetch.values() if p.get("dubber")])
            bg = [d for _, d in self.bg.values()]
        for d in dubbers:
            threading.Thread(target=d.abort, daemon=True).start()
        for d in bg:
            d.cancel.set()

    def install_models(self, mid):
        with self.lock:
            m = next((x for x in self.settings["masini"] if x["id"] == mid), None)
            if not m or self.installs.get(mid, {}).get("busy"):
                return False
            self.installs[mid] = {"busy": True, "msg": "Pornesc descarcarea...", "ok": None}

        def run():
            def progress(msg):
                with self.lock:
                    self.installs[mid]["msg"] = msg
            try:
                engine.Dubber(self.snapshot(), machine=m).install_models(progress)
                with self.lock:
                    self.installs[mid] = {"busy": False, "ok": True,
                                          "msg": "Modelele sunt instalate. Placa e gata de dublat."}
                    if mid in self.pods and "Nu a mers" in str(self.pods[mid].get("msg") or ""):
                        self.pods[mid]["msg"] = "Pornit și gata de dublat."   # old install error is stale now
                self.log(f"{m['nume']}: modelele sunt instalate.")
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.installs[mid] = {"busy": False, "ok": False, "msg": f"Nu a mers: {e}"}
                self.log(f"{m['nume']}: instalarea modelelor a esuat: {e}")
        threading.Thread(target=run, daemon=True).start()
        return True

    def test_machine(self, m):
        m = engine.clean_machines([m], "")[0]
        if not m["adresa"]:
            return "lipseste adresa"
        return engine.Dubber(self.snapshot(), machine=m).check_access()

    def state(self, log_since=None, rev=None):
        """Everything the page shows. log_since: only the log lines after it; rev: the settings
        are left out when the page already has this version of them."""
        try:
            self._ensure_threads()
        except Exception:  # noqa: BLE001
            pass
        try:
            rows, ignored = self.clip_rows()
            folder_error = None
        except Exception as e:  # noqa: BLE001 - never let the page go blank
            rows, ignored, folder_error = [], [], str(e)
        cur_rev = self.settings_rev()
        settings = self.public_settings() if rev != cur_rev else None
        logs, log_seq, log_reset = self.logs_since(log_since)
        idle_n, idle_min = self.idle_info()
        with self.lock:
            done_times = [t for t in self.times.values() if t]
            machines = []
            voices = []
            pods_running = 0
            for m in self.settings["masini"]:
                st = self.mstate.get(m["id"], {})
                j = self.jobs.get(m["id"])
                pod = self.pods.get(m["id"], {})
                if st.get("online") and not voices:
                    voices = st.get("voices") or []
                info = engine.rented_info(m)
                if m["id"] != "local" and ((info and pod.get("status") not in ("TERMINATED", "EXITED"))
                                           or (pod.get("busy") and pod.get("op") == "start")):
                    pods_running += 1
                prov = None if m["id"] == "local" else self._prov_of(m)
                wr = self.want_replace.get(m["id"])
                machines.append({
                    "id": m["id"], "nume": m["nume"], "adresa": m["adresa"], "activ": m["activ"],
                    "has_key": bool(m["cheie"]),
                    "online": st.get("online"), "error": st.get("error"),
                    "working": m["id"] in self.workers,
                    "install": self.installs.get(m["id"]),
                    "local": m["id"] == "local",
                    "pod_id": info["id"] if info else None,
                    "rented": bool(info),
                    "furnizor": prov,                 # runpod / vast (where it is, or would be, rented)
                    "key_ok": bool(prov and self._key(prov)),
                    "pret": pod.get("cost") if info else None,
                    "replacing": ({"why": wr["why"], "in": max(0, int(wr["next"] - time.time()))
                                   if wr["next"] != float("inf") else 0} if wr else None),
                    "pod": {k: v for k, v in pod.items()},
                    "job": {"clip": j["clip"], "stage": j["stage"], "percent": j["percent"],
                            "lang": j.get("lang") if len(engine.target_langs(self.settings)) > 1 else None,
                            "elapsed": time.time() - j["started"]} if j else None,
                })
            online_active = [x for x in machines if x["activ"] and x["adresa"] and x["online"]
                             and engine.machine_allowed({"id": x["id"]}, self.settings["sursa"])]
            today = self.spent_days.get(time.strftime("%Y-%m-%d"), 0.0)
            return {
                "settings": settings,
                "settings_rev": cur_rev,
                "online": bool(online_active),
                "machines": machines,
                "voices": voices,
                "agent": self.agent,
                "alert": self.alert,
                "fallback": engine.fallback_state(),
                "auto_stop_in": (max(0, int(self.settings["oprire_auto_min"] * 60 - (time.time() - self.idle_since)))
                                 if self.idle_since and self.settings["oprire_auto_min"] else None),
                "version": VERSION,
                "running": bool(self.workers),
                "rendering": len(self.renders),
                "finishing": self.finishing,
                "pods_running": pods_running,
                "spent_today": round(today, 4),
                "spent_session": round(self.spent_session, 4),
                "cost_per_clip": round(self.spent_session / self.session_done, 4) if self.session_done else None,
                "clips_session": self.session_done,
                "idle_gpus": idle_n,
                "idle_minutes": round(idle_min, 1),
                "ollama": dict(self.ollama, pull=dict(self.ollama_pull)),
                "ai": dict(self.ai),
                "cover": {k: v for k, v in self.cover.items() if k != "stop"},
                "tool": {k: v for k, v in self.tool.items() if k != "stop"},
                "capcut": dict({k: v for k, v in self.capcut.items() if k != "stop"}, new=self.capcut_new_count()),
                "capfin": self.capfin_state(),
                "ffmpeg": {"ok": randare.ffmpeg_ok(), "encoder": self.render_info["encoder"],
                           "logo": randare.LOGO_FILE.exists(),
                           "logo_v": int(randare.LOGO_FILE.stat().st_mtime) if randare.LOGO_FILE.exists() else 0},
                "stop_after": self.stop_after,
                "avg_secs": sum(done_times) / len(done_times) if done_times else None,
                "parallel": max(1, len(online_active)),
                "clips": rows,
                "ignored": ignored,
                "folder_error": folder_error,
                "logs": logs,
                "log_seq": log_seq,
                "log_reset": log_reset,
                "folders": {
                    "clipuri": str(engine.folder(self.settings["folder_clipuri"])),
                    "dublate": str(engine.folder(self.settings["folder_dublate"])),
                },
            }


APP = None
APP_URL = None          # this app's address once the server runs (a click on a Radar notification opens it)
RADAR = None
RADAR_LOCK = threading.Lock()
RADAR_FILE = HERE / "radar.html"


def radar():
    """The TikTok radar (radar.py), started on first use."""
    global RADAR
    with RADAR_LOCK:
        if RADAR is None:
            def clips_dir():
                c = APP.snapshot() if APP else engine.load_settings()
                return engine.folder(c["folder_clipuri"])

            def ffmpeg():
                return randare.find_ffmpeg()[0] if randare.ffmpeg_ok() else None
            RADAR = radar_mod.Radar(HERE, clips_dir, log=(APP.log if APP else print), ffmpeg=ffmpeg,
                                    open_url=(APP_URL + "radar") if APP_URL else None)
        return RADAR


def start_radar_if_used():
    """The Radar keeps checking (and announces the clips that explode) as soon as the app runs, not only after
    its page was opened - when there is a list of accounts."""
    try:
        if (HERE / "radar.json").exists():
            radar()
    except Exception as e:  # noqa: BLE001
        print("radar:", e, flush=True)


def open_folder(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])





# ======================== POSTARE TIKTOK ========================
# TIKTOK_INTEGRATION_V4
# Butonul „Postare TikTok" din pagină pornește aplicația de postat (tiktok_bot/studio/web.py)
# dacă nu rulează deja și o deschide în aceeași fereastră.
TIKTOK_DIR = Path(os.environ.get("TIKTOK_STUDIO_DIR") or
                  (Path.home() / ".openclaw" / "workspace" / "tiktok_bot" / "studio"))
TIKTOK_PORT = 5055                 # portul implicit; cel real e în studio/server.json
_tiktok_lock = threading.Lock()


def _tiktok_port():
    try:
        return int(json.loads((TIKTOK_DIR / "server.json").read_text(encoding="utf-8"))["port"])
    except Exception:  # noqa: BLE001
        return TIKTOK_PORT
_tiktok_proc = None


def _tiktok_stamp():
    files = list(TIKTOK_DIR.glob("*.py")) + [TIKTOK_DIR / "static" / "index.html"]
    return max((f.stat().st_mtime for f in files if f.exists()), default=0.0)


def _tiktok_get(path, method="GET"):
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(f"http://127.0.0.1:{_tiktok_port()}{path}", method=method,
                                 data=b"{}" if method == "POST" else None,
                                 headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=3) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _tiktok_kill_port():
    """Oprește procesul vechi care ascultă pe portul aplicației de postat (Windows)."""
    if not sys.platform.startswith("win"):
        return
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{_tiktok_port()}") and parts[3].upper() == "LISTENING":
                subprocess.run(["taskkill", "/PID", parts[4], "/F"], capture_output=True)
    except Exception:  # noqa: BLE001
        pass


def _tiktok_outdated():
    """True dacă serverul pornit rulează cod mai vechi decât cel de pe disc."""
    try:
        v = _tiktok_get("/api/version")
    except Exception:  # noqa: BLE001  (server vechi, fără /api/version)
        return True
    if v.get("running"):
        return False                   # nu-l opresc în mijlocul unui run
    return v.get("code", 0) + 1 < _tiktok_stamp()


def _tiktok_restart():
    try:
        _tiktok_get("/api/shutdown", "POST")
    except Exception:  # noqa: BLE001
        _tiktok_kill_port()
    for _ in range(20):
        time.sleep(0.3)
        if not _tiktok_alive():
            return
    _tiktok_kill_port()
    time.sleep(1)


def _tiktok_alive():
    import urllib.request
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{_tiktok_port()}/api/version", timeout=1.5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


# Bara „Traduceri pe telefoane” din pagină: starea telefoanelor din aplicația de postare (GET /api/capcut), fără
# jurnale și liste. Cerută cel mult o dată la 5 s, oricâte ferestre sunt deschise; nu pornește aplicația de postare.
# Câmpurile noi (azi, eta, faza, ramase…) trec neschimbate: pagina le folosește doar dacă există.
_CCTEL_SETARI = ("capcut_reluare_auto", "capcut_min_liber_mb", "capcut_clipuri_pe_cont", "agent_reparare",
                 "agent_buget_zi_usd")
_cctel = {"t": 0.0, "ok_t": 0.0, "busy": False, "d": {"ok": False}}
_cctel_lock = threading.Lock()


def _cctel_cut(v, n=300):
    """Texte scurte și liste mici (mesajele de eroare pot fi lungi, jurnalele nu trec deloc)."""
    if isinstance(v, str):
        return v[:n]
    if isinstance(v, dict):
        return {str(k): _cctel_cut(x, n) for k, x in list(v.items())[:40]}
    if isinstance(v, list):
        return [_cctel_cut(x, n) for x in v[:20]]
    return v


# limita zilnică vs. schimbarea de cont eșuată: aceleași reguli ca fila CapCut (ccQuota din static/index.html)
_CCT_QMSG = re.compile(r"nu mai (are|sunt) traduceri gratuite|și-au folosit traducerile")
_CCT_QALL = re.compile(r"celelalte \d+ conturi|toate (cele \d+ )?conturile? CapCut|și-au folosit traducerile")


def _cctel_tel(p: dict) -> dict:
    """Un telefon, fără jurnal; din jurnal rămân doar: limita_zi (oprit pentru că toate conturile CapCut și-au folosit
    traducerile, nu o schimbare de cont eșuată) și doar_reluare (jobul de acum termină doar clipul început)."""
    q = {k: _cctel_cut(v) for k, v in p.items() if k != "log"}
    lg = [str(x) for x in p["log"]] if isinstance(p.get("log"), list) else []
    m = str(p.get("message") or "")
    if _CCT_QMSG.search(m):                                # (alt mesaj: nici pagina nu-l ia drept limită)
        q["limita_zi"] = bool(not p.get("running") and p.get("state") == "error" and (
            _CCT_QALL.search(m) or not any("nu am schimbat contul CapCut" in x for x in lg[-4:])))
    if p.get("running") and "doar_reluare" not in p:
        pornit = [x for x in lg if "━━ pornit pe " in x]    # primul rând al jobului de acum (jurnalul e doar al lui)
        if pornit:
            q["doar_reluare"] = "doar clipul început" in pornit[-1]
    return q


def capcut_rezumat(d) -> dict:
    """Răspunsul /api/capcut, micșorat: listele de sus devin numărul lor (coada: 6), telefoanele fără jurnal."""
    if not isinstance(d, dict) or not d.get("ok"):
        return {"ok": False}
    out = {}
    for k, v in d.items():
        if k in ("telefoane", "setari", "motive"):
            continue
        out[k] = len(v) if isinstance(v, list) else _cctel_cut(v)
    tel = d.get("telefoane") if isinstance(d.get("telefoane"), dict) else {}
    out["telefoane"] = {str(s): _cctel_tel(p) for s, p in tel.items() if isinstance(p, dict)}
    st = d.get("setari") if isinstance(d.get("setari"), dict) else {}
    out["setari"] = {k: st[k] for k in _CCTEL_SETARI if k in st}
    return out


def _cctel_refused(e) -> bool:
    """Aplicația de postare nu ascultă deloc (închisă), nu doar întârzie."""
    return isinstance(e, ConnectionRefusedError) or isinstance(getattr(e, "reason", None), ConnectionRefusedError)


def capcut_telefoane() -> dict:
    now = time.time()
    with _cctel_lock:
        if now - _cctel["t"] < 5 or _cctel["busy"]:
            return _cctel["d"]
        _cctel["busy"] = True
    d, fresh = None, False
    try:
        try:
            d = capcut_rezumat(_tiktok_get("/api/capcut"))
            fresh = True
        except Exception as e:  # noqa: BLE001
            if _cctel_refused(e):
                try:
                    n = sum(1 for f in CAPCUT_IN.glob("*.mp4") if f.is_file())
                except OSError:
                    n = 0
                d = {"ok": False, "postare_oprita": True, "coada": n}
            elif now - _cctel["ok_t"] < 30:
                d = _cctel["d"]            # a răspuns greu acum: rămâne starea de acum câteva secunde
            else:
                d = {"ok": False}
    finally:
        with _cctel_lock:
            if d is not None:
                _cctel["d"] = d
                if fresh and d.get("ok"):
                    _cctel["ok_t"] = now
            _cctel["t"] = now
            _cctel["busy"] = False
    return _cctel["d"]


def tiktok_open():
    """Pornește serverul de postare (o singură dată). Întoarce (url, eroare)."""
    global _tiktok_proc
    with _tiktok_lock:
        if _tiktok_alive():
            if not _tiktok_outdated():
                return f"http://127.0.0.1:{_tiktok_port()}/", None
            APP.log("Postare TikTok: am găsit o versiune nouă, repornesc aplicația de postat…")
            _tiktok_restart()
        web = TIKTOK_DIR / "web.py"
        if not web.exists():
            return None, f"Nu găsesc aplicația de postat în {TIKTOK_DIR}"
        import importlib.util
        missing = []
        for m in ("flask", "ruamel.yaml", "selenium"):
            try:
                if importlib.util.find_spec(m) is None:
                    missing.append(m)
            except Exception:  # noqa: BLE001  (ex. lipsește pachetul părinte „ruamel")
                missing.append(m)
        if missing:
            APP.log("Postare TikTok: instalez " + ", ".join(missing) + " (o singură dată)...")
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "-r",
                                str(TIKTOK_DIR / "requirements.txt")], capture_output=True, text=True)
            if r.returncode != 0:
                return None, "Nu am putut instala pachetele: " + (r.stderr or r.stdout)[-300:]
        _tiktok_kill_port()               # copii vechi / agățate care țin portul ocupat
        time.sleep(0.5)
        try:
            (TIKTOK_DIR / "server.json").unlink()      # serverul nou scrie portul pe care l-a primit
        except OSError:
            pass
        log = open(TIKTOK_DIR / "web.log", "a", encoding="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        _tiktok_proc = subprocess.Popen([sys.executable, "web.py", "--no-browser"], cwd=str(TIKTOK_DIR),
                                        stdout=log, stderr=log, creationflags=flags, env=env)
        for _ in range(40):
            time.sleep(0.5)
            if (TIKTOK_DIR / "server.json").exists() and _tiktok_alive():
                APP.log(f"Postare TikTok pornită (port {_tiktok_port()}).")
                return f"http://127.0.0.1:{_tiktok_port()}/", None
            if _tiktok_proc.poll() is not None:
                break
        try:
            tail = (TIKTOK_DIR / "web.log").read_text(encoding="utf-8", errors="replace")[-400:]
        except OSError:
            tail = ""
        return None, "Aplicația de postat nu a pornit. " + tail


# ------------------------------------------------------------------
#  Acces de pe un site (CORS + parolă) - OPȚIONAL.
#  Implicit totul e GOL => aplicația merge EXACT ca înainte: doar
#  pagina locală (același calculator) poate comanda motorul.
#  Ca să poți deschide interfața de pe un site public și tot de pe
#  calculatorul tău să ruleze, pui în setari.json două câmpuri:
#     web_parola   ->  o parolă secretă aleasă de tine
#     web_origini  ->  adresa site-ului tău (una sau mai multe, separate prin virgulă)
#  (sau prin variabilele de mediu WEB_PAROLA / WEB_ORIGINI).
#  Doar site-urile din listă, și doar cu parola corectă, pot comanda
#  motorul — ca să nu poată un site străin să pună mâna pe calculator.
# ------------------------------------------------------------------
_WEB_CFG = {"mtime": None, "parola": "", "origini": []}


def _web_config():
    """(parola, [origini]) din setari.json / mediu, cu cache pe data fișierului."""
    p = HERE / "setari.json"
    try:
        m = p.stat().st_mtime
    except OSError:
        m = None
    if m != _WEB_CFG["mtime"]:
        parola, origini = "", []
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            parola = str(d.get("web_parola") or "").strip()
            origini = [o.strip().rstrip("/") for o in re.split(r"[,\s]+", str(d.get("web_origini") or "")) if o.strip()]
        except Exception:  # noqa: BLE001 - setari.json lipsă sau stricat: rămânem pe gol (doar local)
            pass
        parola = (os.environ.get("WEB_PAROLA") or parola).strip()
        env_o = os.environ.get("WEB_ORIGINI")
        if env_o:
            origini = [o.strip().rstrip("/") for o in re.split(r"[,\s]+", env_o) if o.strip()]
        _WEB_CFG.update(mtime=m, parola=parola, origini=origini)
    return _WEB_CFG["parola"], _WEB_CFG["origini"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    # --- CORS pentru acces de pe un site (vezi _web_config mai sus) ---
    def _emit_cors(self):
        """Adaugă antetul care permite site-ului configurat (și cu parola corectă) să
        citească răspunsul. Fără el, browserul blochează orice alt site — deci implicit
        (parolă goală) nimic din afară nu are acces."""
        origin = (self.headers.get("Origin") or "").strip().rstrip("/")
        if not origin:
            return
        parola, origini = _web_config()
        if not (parola and origin in origini):
            return
        if self.command == "OPTIONS" or (self.headers.get("X-Parola") or "") == parola:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def end_headers(self):
        try:
            self._emit_cors()
        except Exception:  # noqa: BLE001 - CORS e bonus; nu stricăm un răspuns dacă pică
            pass
        super().end_headers()

    def do_OPTIONS(self):
        # cererea „preflight” pe care o trimite browserul înainte de un POST de pe alt site
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Parola, Range")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, inline=False):
        """Un fișier de pe disc, pe bucăți; cu Range (clipul se poate derula în pagină)."""
        ctype = {".mp4": "video/mp4", ".srt": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
                 ".wav": "audio/wav"}.get(path.suffix.lower(), "application/octet-stream")
        size = path.stat().st_size
        a, b = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                a = int(m.group(1))
                b = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                a = max(0, size - int(m.group(2)))
            if a > b:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition", ("inline" if inline else "attachment")
                         + f"; filename*=UTF-8''{quote(path.name)}")
        self.send_header("Content-Length", str(b - a + 1))
        self.end_headers()
        try:
            with open(path, "rb") as fh:
                fh.seek(a)
                left = b - a + 1
                while left > 0:
                    chunk = fh.read(min(left, 1024 * 1024))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, OSError):
            pass                                 # pagina a închis cererea (derulare în clip)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _host_ok(self):
        """The Host must be this computer: a web page that points its own name at 127.0.0.1 (DNS rebinding)
        would otherwise look "same-origin" to the checks below."""
        host = (self.headers.get("Host") or "").strip().lower()
        name = re.sub(r":\d+$", "", host)
        if name in ("", "127.0.0.1", "localhost", "[::1]"):
            return True
        self._json({"error": "forbidden"}, 403)
        return False

    def _html(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # no other website may show these pages inside a frame (and trick a click on them)
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _local_only(self):
        # Refuse requests coming from other websites (only this page may drive it).
        if not self._host_ok():
            return False
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if not origin or urlparse(origin).netloc == host:
            return True                      # pagina locală, de pe același calculator (ca până acum)
        # Cerere de pe alt site: permisă DOAR dacă e site-ul configurat ȘI vine cu parola corectă.
        parola, origini = _web_config()
        o = origin.strip().rstrip("/")
        if parola and o in origini and (self.headers.get("X-Parola") or "") == parola:
            return True
        self._json({"error": "forbidden"}, 403)
        return False

    def do_GET(self):
        if not self._host_ok():
            return
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            self._html(UI_FILE.read_bytes())
        elif p == "/api/state":
            q = parse_qs(urlparse(self.path).query)

            def num(k):
                try:
                    return int(q[k][0])
                except (KeyError, ValueError, IndexError):
                    return None
            self._json(APP.state(log_since=num("log_since"), rev=num("rev")))
        elif p == "/api/capcut-telefoane":
            # bara „Traduceri pe telefoane”: telefoanele din aplicația de postare (fila CapCut), pe scurt
            self._json(capcut_telefoane())
        elif p == "/api/film":
            self._json(APP.film.state())
        elif p == "/api/film-muzica-fisier":
            # o melodie din povestiri\_muzica, ca s-o asculți în pagină înainte s-o pui sub voce
            f = APP.film.muzica_path(parse_qs(urlparse(self.path).query).get("name", [""])[0])
            if not f:
                return self._json({"error": "nu există"}, 404)
            self._send_file(f, inline=True)
        elif p == "/api/film-voci":
            # vocile salvate în VoiceStudio, pentru „Vocea care povestește” (cerut doar când se deschide secțiunea)
            self._json(povestire.voci_voicestudio(APP.snapshot()))
        elif p == "/api/film-fisier":
            # SRT-ul / povestirea / clipul unei lucrări „Film → povestire” (descărcare sau redare în pagină)
            q = parse_qs(urlparse(self.path).query)
            f = APP.film.file_of(q.get("id", [""])[0], q.get("f", [""])[0])
            if not f:
                return self._json({"error": "nu există"}, 404)
            self._send_file(f, inline=q.get("inline", [""])[0] == "1")
        elif p == "/api/conturi-watermark":
            # the TikTok accounts that take the clips in turn (watermark = their name) and the next one
            self._json(engine.accounts_overview(APP.snapshot()))
        elif p == "/api/pronuntie-sugestii":
            # words the voice still says wrong in the newest clips, minus those already in the dictionary
            c = APP.snapshot()
            known = [a for a, _ in engine.parse_lexicon(c.get("pronuntie_dictionar"))]
            self._json({"items": engine.pron_suggestions(known)})
        elif p == "/api/frame":
            c = APP.snapshot()
            q = parse_qs(urlparse(self.path).query)
            want = q.get("name", [""])[0]
            ccf = capcut_final_clips().get(want) if want else None     # traducere CapCut: cadrul din montajul ei
            vids = [ccf] if ccf else [v for v in engine.list_videos(c) if not engine.is_audio(v)]
            if not vids or not randare.ffmpeg_ok():
                return self._json({"error": "Pune un clip video în folderul clipuri." if randare.ffmpeg_ok()
                                   else "Lipseste ffmpeg."}, 404)
            if not ccf:
                vids = [v for v in vids if v.name == want] or vids
            try:
                at = float(q.get("at", ["3"])[0])
            except ValueError:
                at = 3.0
            out = Path(tempfile.gettempdir()) / f"cadru-{os.getpid()}-{threading.get_ident()}.jpg"
            try:
                info = randare.frame(vids[0], at, out)
                body = out.read_bytes()
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 500)
            finally:
                out.unlink(missing_ok=True)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Duration", f"{info['dur']:.2f}")
            self.send_header("X-Clip", quote(want if ccf else vids[0].name))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == "/api/logo":
            if not randare.LOGO_FILE.exists():
                return self._json({"error": "no logo"}, 404)
            self._bytes(randare.LOGO_FILE.read_bytes(), "image/png")
        elif p == "/radar":
            self._html(RADAR_FILE.read_bytes())
        elif p == "/api/radar":
            q = parse_qs(urlparse(self.path).query)
            try:
                since = int(q["rev"][0])
            except (KeyError, ValueError, IndexError):
                since = None
            st = radar().state(since=since, live=q.get("live", [""])[0] == "1")
            st["app_version"] = VERSION
            self._json(st)
        else:
            self._json({"error": "not found"}, 404)

    def _radar_action(self, data):
        r = radar()
        act = data.get("action")
        if act == "add":
            err = r.add(str(data.get("user") or ""))
            return {"ok": err is None, "error": err}
        if act == "add_many":
            names = radar_mod.users_from_text(str(data.get("text") or ""))
            if not names:
                return {"ok": False, "error": "Nu am găsit niciun cont în ce ai lipit (ex. @nume, unul pe rând)."}
            source = str(data.get("source") or "").strip()
            if source and data.get("replace"):
                try:
                    res = r.sync(source, [(u, None) for u in names], replace=True)
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                return {"ok": True, "sync": res, "added": res["added"], "removed": res["removed"], "found": len(names)}
            added, already = r.add_many(names, source=source or None)
            return {"ok": True, "added": added, "already": already, "found": len(names)}
        if act == "sync":
            items = []
            if not data.get("users") and data.get("text"):
                data = dict(data, users=radar_mod.users_from_text(str(data.get("text"))))
            for x in (data.get("users") or [])[:20000]:
                if isinstance(x, (list, tuple)) and x:
                    u, f = x[0], (x[1] if len(x) > 1 else None)
                elif isinstance(x, dict):
                    u, f = x.get("u") or x.get("user"), x.get("f")
                else:
                    u, f = x, None
                good = isinstance(f, (int, float)) and not isinstance(f, bool) and f >= 0
                items.append((str(u or ""), int(f) if good else None))
            try:
                res = r.sync(str(data.get("source") or ""), items, replace=data.get("replace") is not False,
                             dry=bool(data.get("dry")))
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "sync": res}
        if act == "remove":
            r.remove(str(data.get("user") or ""), source=data.get("source") or None)
            return {"ok": True}
        if act == "remove_all":
            r.remove_all(source=data.get("source") or None)
            return {"ok": True}
        if act == "check":
            n = r.check_now(data.get("user") or None, only_errors=bool(data.get("only_errors")))
            return {"ok": True, "queued": n}
        if act == "settings":
            r.settings(interval_h=data.get("interval_h"), per_account=data.get("per_account"),
                       workers=data.get("workers"), hot_per_hour=data.get("hot_per_hour"),
                       only_new=data.get("only_new"))
            return {"ok": True}
        if act == "hot_seen":
            r.hot_seen(data.get("upto"))
            return {"ok": True}
        if act == "notify_test":
            err = r.notify_test()
            return {"ok": err is None, "error": err}
        ids = data.get("ids")
        ids = [str(i) for i in ids][:5000] if isinstance(ids, list) else None
        if act == "download":
            return {"ok": True, "queued": r.download(ids or [])}
        if act == "dl_cancel":
            return {"ok": True, "cancelled": r.cancel_downloads()}
        if act == "hide":
            return {"ok": True, "n": r.hide(ids or [], hidden=data.get("hidden") is not False)}
        if act == "mark_seen":
            return {"ok": True, "n": r.mark_seen(ids)}
        if act == "open_clips":
            try:
                open_folder(Path(r.clips_dir()))
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"Nu pot deschide folderul: {e}"}
            return {"ok": True}
        return {"ok": False, "error": "acțiune necunoscută"}

    def do_POST(self):
        p0 = urlparse(self.path).path
        if not self._local_only():
            return
        if p0 == "/api/radar-import":
            # TikTok's "Download your data" file (.zip / .json): the accounts you follow
            q = parse_qs(urlparse(self.path).query)
            try:
                names, owner = radar_mod.parse_export(self._body(), unquote(q.get("name", [""])[0]))
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": f"Nu pot citi fișierul: {e}"})
            if not names:
                return self._json({"ok": False, "error": "Nu am găsit lista de urmăriri în fișier. Alege fișierul "
                                                         ".zip primit de la TikTok (format JSON)."})
            if owner and owner.lower() != "manual":
                res = radar().sync(owner, [(u, None) for u in names], replace=False)
                return self._json({"ok": True, "found": len(names), "added": res["added"],
                                   "already": len(names) - res["added"], "source": res["source"]})
            added, already = radar().add_many(names)
            return self._json({"ok": True, "found": len(names), "added": added, "already": already})
        p = urlparse(self.path).path
        try:
            data = json.loads(self._body() or b"{}")
        except json.JSONDecodeError:
            data = {}
        if p == "/api/radar":
            return self._json(self._radar_action(data if isinstance(data, dict) else {}))
        if p == "/api/settings":
            if not isinstance(data, dict):
                return self._json({"error": "bad settings"}, 400)
            APP.save_from_page(data)
            APP._last_pod_poll = 0
            APP.wake.set()
            if APP.running:
                # A GPU switched on mid-run joins the queue once its status is refreshed.
                threading.Timer(3, APP.start, kwargs={"join_only": True}).start()
            self._json({"ok": True, "settings": APP.public_settings(), "settings_rev": APP.settings_rev()})
        elif p == "/api/machine-delete":
            err, msg = APP.delete_machine(str(data.get("id") or ""))
            self._json({"ok": err is None, "error": err, "msg": msg,
                        "settings": APP.public_settings(), "settings_rev": APP.settings_rev()})
        elif p == "/api/finish-day":
            APP.finish_day()
            self._json({"ok": True})
        elif p == "/api/pod":
            action = data.get("action")
            if action not in ("stop", "start"):
                return self._json({"error": "bad action"}, 400)
            err = APP.pod_action(str(data.get("id") or ""), action)
            self._json({"ok": err is None, "error": err})
        elif p == "/api/preview":
            if not randare.ffmpeg_ok():
                return self._json({"error": "Lipseste ffmpeg. Inchide si porneste din nou "
                                            "„Porneste dublarea.bat”, il instaleaza singur."}, 400)
            c = APP.snapshot()
            ccf = capcut_final_clips().get(data.get("name") or "")     # traducere CapCut: din montajul ei
            if ccf:
                vids = [ccf]
                c = dict(c, sterge_zona=engine.manual_zone(c, data.get("name")))
            else:
                vids = [v for v in engine.list_videos(c) if not engine.is_audio(v)]
                vids = [v for v in vids if v.name == data.get("name")] or vids
                if vids:
                    c = dict(c, sterge_zona=engine.manual_zone(c, vids[0].name))
            out = Path(tempfile.gettempdir()) / f"previzualizare-{os.getpid()}.jpg"
            try:
                randare.preview(vids[0] if vids else None, c, out)
                body = out.read_bytes()
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e)}, 500)
            finally:
                out.unlink(missing_ok=True)
            self._bytes(body, "image/jpeg")
        elif p == "/api/detect-zone":
            c = APP.snapshot()
            want = data.get("name") or ""
            ccf = capcut_final_clips().get(want) if want else None     # traducere CapCut: pe montajul ei
            vids = [ccf] if ccf else [v for v in engine.list_videos(c) if not engine.is_audio(v)]
            if not vids or not randare.ffmpeg_ok():
                return self._json({"error": "Pune un clip video în folderul clipuri." if randare.ffmpeg_ok()
                                   else "Lipseste ffmpeg."}, 400)
            if not ccf:
                vids = [v for v in vids if v.name == want] or vids
            try:
                dur = randare.probe(vids[0])["dur"] or 10.0
                zone = engine.find_sub_zone(vids[0], [dur * (i + 0.5) / 12 for i in range(12)], c,
                                            threading.Event())
            except Exception as e:  # noqa: BLE001
                return self._json({"error": str(e) or e.__class__.__name__}, 500)
            shown = want if ccf else vids[0].name
            APP.log(f"Test agent pe {shown}: {engine.zone_text(zone)}")
            self._json({"ok": True, "zona": zone, "text": engine.zone_text(zone), "clip": shown})
        elif p == "/api/tool":
            if data.get("stop"):
                APP.tool["stop"].set()
                return self._json({"ok": True})
            err = APP.start_tool("all" if data.get("which") == "all" else "one", str(data.get("name") or ""))
            self._json({"ok": err is None, "error": err})
        elif p == "/api/capcut":
            if data.get("stop"):
                APP.capcut["stop"].set()
                return self._json({"ok": True})
            err = APP.start_capcut()
            self._json({"ok": err is None, "error": err})
        elif p == "/api/capcut-final":
            if data.get("stop_video"):        # „Oprește video-urile”: și coada video (Vmake / randarea), nu doar pregătirea
                return self._json({"ok": True, "n": APP.stop_capfin_videos()})
            if data.get("reia_video"):
                return self._json({"ok": True, "n": APP.resume_capfin_videos()})
            if data.get("stop"):
                APP.capfin["stop"].set()
                return self._json({"ok": True})
            err = APP.start_capfin("pc" if data.get("unde") != "app" else "app")
            self._json({"ok": err is None, "error": err})
        elif p == "/api/cover-test":
            if data.get("stop"):
                APP.cover["stop"].set()
                return self._json({"ok": True})
            err = APP.start_cover("all" if data.get("which") == "all" else "first", str(data.get("name") or ""))
            self._json({"ok": err is None, "error": err})
        elif p == "/api/ai-install":
            self._json({"ok": APP.install_ai()})
        elif p == "/api/ollama-pull":
            model = str(data.get("model") or APP.snapshot()["ollama_model"]).strip()
            self._json({"ok": APP.pull_model(model)})
        elif p == "/api/logo-delete":
            randare.LOGO_FILE.unlink(missing_ok=True)
            self._json({"ok": True})
        elif p == "/api/fallback-reset":
            engine.reset_fallback()
            APP.log("Revin la traducerea cu agentul (Claude Code).")
            self._json({"ok": True})
        elif p == "/api/pod-new":
            name, err = APP.add_new_pod()
            self._json({"ok": err is None, "error": err, "name": name})
        elif p == "/api/pods-start-all":
            err = APP.start_all_pods()
            self._json({"ok": err is None, "error": err})
        elif p == "/api/pods-stop-all":
            err = APP.stop_all_pods()
            self._json({"ok": err is None, "error": err})
        elif p == "/api/install-models":
            self._json({"ok": APP.install_models(str(data.get("id") or ""))})
        elif p == "/api/test-machine":
            m = dict(data)
            if m.get("cheie") == "********":
                with APP.lock:
                    m["cheie"] = next((x["cheie"] for x in APP.settings["masini"]
                                       if x["id"] == m.get("id")), "")
            err = APP.test_machine(m)
            self._json({"ok": err is None, "error": err})
        elif p == "/api/start":
            self._json({"ok": APP.start()})
        elif p == "/api/stop":
            with APP.lock:
                APP.stop_after = True
            self._json({"ok": True})
        elif p == "/api/cancel":
            APP.cancel_now()
            self._json({"ok": True})
        elif p == "/api/retry":
            APP.retry(str(data["name"]) if data.get("name") else None)
            self._json({"ok": True})
        elif p == "/api/tiktok-open":
            url, err = tiktok_open()
            self._json({"ok": err is None, "url": url, "error": err})
        elif p == "/api/film":
            act, jid = data.get("action"), str(data.get("id") or "")
            if act == "start":
                jid, err = APP.film.start(str(data.get("film") or ""), str(data.get("lungime") or "normal"),
                                          str(data.get("voce") or "auto"), str(data.get("limba") or "ro"),
                                          data.get("blur") is not False, data.get("captions") is not False,
                                          str(data.get("mod") or "parte"), int(data.get("durata") or 180),
                                          str(data.get("voce_f") or ""), str(data.get("voce_m") or ""),
                                          str(data.get("muzica") or ""),
                                          povestire.muzica_db(data.get("muzica_db")), str(data.get("nr") or "3"))
                return self._json({"ok": err is None, "error": err, "id": jid})
            if act in ("stop", "retry"):
                err = APP.film.stop(jid) if act == "stop" else APP.film.retry(jid)
                return self._json({"ok": err is None, "error": err})
            if act == "parte":                   # „Mai multe recap-uri”: fă și recap-ul pentru partea asta din analiză
                try:
                    err = APP.film.fa_parte(jid, int(data.get("i")),
                                            str(data["muzica"]) if data.get("muzica") is not None else None,
                                            data.get("db"))
                except (TypeError, ValueError):
                    err = "parte necunoscută"
                return self._json({"ok": err is None, "error": err})
            if act == "muzica":                  # altă melodie / alt volum pe un clip gata (doar sunetul se reface)
                err = APP.film.schimba_muzica(jid, str(data.get("muzica") or ""), povestire.muzica_db(data.get("db")))
                return self._json({"ok": err is None, "error": err})
            if act == "exemple":                 # folderul cu scripturile-exemplu pentru recap-ul la persoana I
                open_folder(povestire.EXEMPLE)
                return self._json({"ok": True})
            if act == "open":
                f = APP.film.file_of(jid, "mp4") or APP.film.file_of(jid, "srt")
                with APP.film.lock:
                    folder = (APP.film.jobs.get(jid) or {}).get("folder")
                open_folder(Path(folder) if folder else povestire.FOLDER)
                return self._json({"ok": True, "file": str(f or "")})
            self._json({"error": "bad action"}, 400)
        elif p == "/api/open":
            which = data.get("which")
            if which == "capcut":
                open_folder(CAPCUT_IN)
                return self._json({"ok": True})
            if which not in ("clipuri", "dublate"):
                return self._json({"error": "bad folder"}, 400)
            key = "folder_clipuri" if which == "clipuri" else "folder_dublate"
            open_folder(engine.folder(APP.snapshot()[key]))
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def do_PUT(self):
        if not self._local_only():
            return
        u = urlparse(self.path)
        if u.path == "/api/logo":
            return self._put_logo()
        film = u.path == "/api/film-upload"           # un film pentru „Film → povestire”: în povestiri\_filme
        muz = u.path == "/api/film-muzica"            # o melodie pentru povestiri: în povestiri\_muzica
        if u.path != "/api/upload" and not film and not muz:
            return self._json({"error": "not found"}, 404)
        name = Path(unquote(parse_qs(u.query).get("name", [""])[0])).name
        if muz:
            if not name or Path(name).suffix.lower() not in povestire.AUDIO_EXTS:
                return self._json({"error": "Doar melodii (mp3, m4a, wav, aac, ogg, flac)."}, 400)
        elif film:
            if not name or Path(name).suffix.lower() not in povestire.VIDEO_EXTS:
                return self._json({"error": "Doar fișiere video (mp4, mkv, mov, avi, webm...)."}, 400)
        elif not name or Path(name).suffix.lower() not in engine.MEDIA_EXTS:
            return self._json({"error": "Doar fisiere video sau audio (mp4, mkv, mov, mp3, wav, m4a...)."}, 400)
        dest = povestire.MUZICA if muz else povestire.FILME if film else engine.folder(APP.snapshot()["folder_clipuri"])
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / name
        tmp = dest / (name + ".upload")
        remaining = int(self.headers.get("Content-Length") or 0)
        try:
            with open(tmp, "wb") as fh:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)
        except OSError:                           # pagina a închis cererea pe drum
            tmp.unlink(missing_ok=True)
            return
        if remaining:
            tmp.unlink(missing_ok=True)
            return self._json({"error": "upload incomplet"}, 400)
        try:
            tmp.replace(target)
        except OSError:                           # același nume, deschis acum (ascultat în pagină / folosit la un clip)
            tmp.unlink(missing_ok=True)
            return self._json({"error": f"{name} e folosit chiar acum (ascultat în pagină sau într-un clip în lucru). "
                                        "Oprește-l și încearcă din nou."}, 409)
        APP.log(f"Adaugat: {name}" + (" (film pentru povestire)" if film else " (melodie pentru povestiri)" if muz else ""))
        self._json({"ok": True, "name": name})


def _put_logo(self):
    n = int(self.headers.get("Content-Length") or 0)
    if not 0 < n <= 20 * 1024 * 1024:
        return self._json({"error": "Imaginea trebuie sa aiba sub 20 MB."}, 400)
    data = self.rfile.read(n)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        randare.LOGO_FILE.write_bytes(data)
    else:
        # JPG / WEBP / ... -> PNG with ffmpeg
        ff = randare.find_ffmpeg()[0]
        if not ff:
            return self._json({"error": "Pune logo-ul ca PNG (fara ffmpeg nu pot converti alte formate)."}, 400)
        tmp = Path(tempfile.gettempdir()) / f"logo-{os.getpid()}.img"
        tmp.write_bytes(data)
        try:
            code, _, err = randare._run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(tmp),
                                         "-frames:v", "1", str(randare.LOGO_FILE)], timeout=60)
        finally:
            tmp.unlink(missing_ok=True)
        if code != 0:
            return self._json({"error": "Nu pot citi imaginea. Incearca un PNG."}, 400)
    APP.log("Logo-ul pentru watermark a fost salvat.")
    self._json({"ok": True})


Handler._put_logo = _put_logo


_CLOSE_HANDLER = None


def install_close_handler(app):
    """Windows: closing the black console window deletes the rented pods first (best effort,
    Windows gives a handler about 5 s), so they don't keep billing."""
    global _CLOSE_HANDLER
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        def handler(event):
            # 2 = CTRL_CLOSE_EVENT (window closed), 5 = logoff, 6 = shutdown; Ctrl+C keeps its usual meaning
            # only when the user turned it on: closing the window is also how the app gets
            # restarted, and deleting the GPUs then means paying their setup again
            if event in (2, 5, 6) and app.settings.get("sterge_gpu_la_inchidere"):
                try:
                    app.emergency_stop_pods(4.0)
                except Exception:  # noqa: BLE001
                    pass
                return True
            return False
        _CLOSE_HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(handler)   # keep a reference
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_CLOSE_HANDLER, True)
    except Exception as e:  # noqa: BLE001
        print("Nu am putut pune oprirea GPU-urilor la inchiderea ferestrei:", e, flush=True)


# ---------- o singură copie a aplicației ----------
# Pe Windows SO_REUSEADDR (pus implicit de ThreadingHTTPServer) lasă a doua copie să asculte pe același port
# fără nicio eroare: pagina rămânea la copia veche, iar a doua își pornea în ascuns buclele (radar, GPU-uri,
# reluări). Acum portul se ține exclusiv, iar o copie care îl găsește ocupat de aplicația de dublare îi
# deschide pagina și iese, înainte de App().
_WIN = sys.platform.startswith("win")
_LACAT_NUME = "Local\\DublareBatch_" + re.sub(r"[^0-9a-z]+", "_", str(HERE).lower())[-120:]
_LACAT = None                       # mutexul ținut cât trăiește procesul (nu se închide)


class _Server(ThreadingHTTPServer):
    """Pe Windows portul se ține exclusiv: a doua copie primește eroare la bind (și trece la portul
    următor sau iese), nu „împarte” portul în tăcere."""
    allow_reuse_address = not _WIN

    def server_bind(self):
        if _WIN and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass        # și fără ea, fără SO_REUSEADDR nu se leagă peste un port ocupat
        super().server_bind()


def _dublare_pornita(port, timeout=5.0):
    """Adresa aplicației de dublare care răspunde deja pe portul ăsta (GET /api/state), altfel None."""
    import urllib.request
    url = f"http://127.0.0.1:{port}/"
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(url + "api/state", timeout=timeout) as r:
            st = json.loads(r.read().decode("utf-8"))
        if isinstance(st, dict) and ("logs" in st or "clips" in st):
            return url
    except Exception:  # noqa: BLE001 - refuzat, prea lent sau alt program: nu e ea
        pass
    return None


def _lacat_o_copie(nume=None):
    """Windows: un mutex cu nume, ținut cât trăiește procesul. False = îl ține deja altă copie a aplicației
    din același folder. True și când nu se poate verifica (nu blochează pornirea)."""
    global _LACAT
    if not _WIN:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.CreateMutexW(None, False, nume or _LACAT_NUME)
        err = ctypes.get_last_error()
        if not h:
            return True
        if err == 183:              # ERROR_ALREADY_EXISTS
            k32.CloseHandle(h)
            return False
        _LACAT = h
        return True
    except Exception:  # noqa: BLE001
        return True


def _leaga_port(port0=None, incercari=20):
    """(server, None) pe primul port liber de la port0; (None, url) când pe un port ocupat răspunde deja
    aplicația de dublare; (None, None) când nu e niciun port liber."""
    port0 = PORT if port0 is None else port0
    for port in range(port0, port0 + incercari):
        try:
            return _Server(("127.0.0.1", port), Handler), None
        except OSError:
            alta = _dublare_pornita(port)
            if alta:
                return None, alta
    return None, None


def _port_ocupat(port):
    """Ascultă ceva pe port? (bind de probă, pe loc; pe Windows o conexiune refuzată durează ~2 s)"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _alta_copie(port0=None, incercari=20):
    """Copia care răspunde pe unul din porturile aplicației (doar porturile ocupate; fiecare cel mult 2 s)."""
    port0 = PORT if port0 is None else port0
    for port in range(port0, port0 + incercari):
        url = _port_ocupat(port) and _dublare_pornita(port, timeout=2.0)
        if url:
            return url
    return None


def main():
    global APP, APP_URL
    # întâi lacătul și portul, abia apoi App(): a doua copie nu-și mai pornește buclele din fundal
    singura = _lacat_o_copie()
    server, alta = _leaga_port()
    if not alta and not singura:
        # lacătul îl ține altă copie, dar n-a răspuns pe portul încercat: o caut pe toate; a doua nu pornesc
        if server is not None:
            server.server_close()
            server = None
        alta = _alta_copie()
        if not alta:
            print("Aplicatia de dublare ruleaza deja in alta fereastra 'Dublare batch', dar nu raspunde acum. "
                  "Inchide TOATE ferestrele 'Dublare batch', apoi porneste din nou Porneste dublarea.bat.",
                  flush=True)
            return
    if alta:
        print(f"Aplicatia de dublare ruleaza deja la {alta} - nu pornesc a doua copie, deschid pagina ei.",
              flush=True)
        print("Ca s-o repornesti (de ex. dupa o actualizare): inchide TOATE ferestrele 'Dublare batch', "
              "apoi porneste din nou Porneste dublarea.bat.", flush=True)
        if "--no-browser" not in sys.argv and not _web_config()[1]:
            webbrowser.open(alta)
        return
    if server is None:
        sys.exit("Nu am gasit un port liber.")
    APP = APP or App()
    install_close_handler(APP)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    APP_URL = url
    print(f"Interfata ruleaza la {url}  (inchide fereastra asta ca sa opresti)", flush=True)
    threading.Timer(8, start_radar_if_used).start()
    if "--no-browser" not in sys.argv and not _web_config()[1]:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    elif _web_config()[1]:
        print("Folosesti site-ul (web_origini e setat): nu deschid pagina locala. "
              "Deschide adresa site-ului in browser.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
