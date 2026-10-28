"""
radar.py - Radar TikTok: watches a list of TikTok accounts and lists their recent clips with the
date and hour they were posted, views, likes, comments and shares, so the ones worth dubbing are
easy to spot. A clip can be downloaded straight into the "clipuri" folder.

Uses yt-dlp (installed / updated by itself on first use, in the same Python as the app), always in a
separate process: TikTok changes often and yt-dlp is updated without restarting the app.
Everything is kept in radar.json next to the app.
"""
import base64
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

INTERVALS_H = (1, 2, 4, 8)
PER_ACCOUNT = (10, 15, 30)     # newest clips asked for at each check (15 = one request to TikTok)
KEEP_S = 24 * 3600             # only clips posted in the last 24 hours are kept: older ones are of no use
MAX_PER_ACCOUNT = 120          # clips remembered per account
HOT_CHOICES = (0, 10000, 20000, 50000, 100000)   # views per hour that make a clip "explode" (0 = no alerts)
HOT_GAP_S = 60                 # at most one Windows notification a minute (the clips meanwhile are gathered)
HOT_AVG_MAX_H = 6              # the average since posting counts as "exploding" only for clips this young
HOT_AGE_MAX_H = 22             # older clips leave the 24 h list soon: no alert for them any more
UPDATE_EVERY_S = 2 * 86400     # yt-dlp: TikTok changes often, newer versions follow
FORCED_UPDATE_GAP_S = 6 * 3600  # an extraction error may mean "TikTok changed": update, but not more often than this
SAVE_GAP_S = 45                # check results reach radar.json at most this often (user actions: at once)
HIST_KEEP = 6                  # [time, views] pairs kept per clip, for "views per hour between checks"
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov")
WORKERS = (1, 2, 3, 4)         # accounts checked at the same time
PAUSE_S = (1.0, 3.0)           # pause of each worker between two accounts (slows down by itself, see _slow)
SLOW_MAX = 4.0                 # TikTok refusing: the pause grows up to this many times (and comes back by itself)
SOFT_RETRY_S = 600             # TikTok refused for now: try again after 10, 20, 40... minutes (up to the interval)
HARD_RETRY_S = 12 * 3600       # private / no clips / no such account: look again only after 12 hours
RECENT_HOLD_H = 6              # "only the new accounts": the others, checked less than this long ago, wait until then
DL_WORKERS = 2                 # clips downloaded at the same time
DIAG_FILE = "radar_diagnostic.log"
DIAG_MAX = 1_500_000
NO_WINDOW = 0x08000000 if os.name == "nt" else 0       # CREATE_NO_WINDOW

# yt-dlp, run through this: TikTok often answers its clip-list request (api/creator/item_list) with an empty
# page. The request is retried here like a real browser would make it (impersonation), then plainly, then with a
# fresh device id; each try is reported on stderr ("RADAR ...") for radar_diagnostic.log.
# It also reports the account's follower count ("RADAR followers N"), which TikTok sends anyway with the
# profile page yt-dlp reads and with the clip list: the Radar keeps it up to date at every check.
YTDLP_RUNNER = r"""
import random, sys, time
import yt_dlp


def _followers(o):
    # followerCount in a TikTok stats block: a number in "stats", a string in "statsV2"
    if not isinstance(o, dict):
        return None
    for key in ("stats", "statsV2", "authorStats", "authorStatsV2"):
        s = o.get(key)
        if isinstance(s, dict):
            try:
                n = int(s.get("followerCount"))
            except (TypeError, ValueError):
                continue
            if n >= 0:
                return n
    return None


def _say_followers(n):
    if n is not None:
        sys.stderr.write("RADAR followers %d\n" % n)
        sys.stderr.flush()


try:
    from yt_dlp.extractor import tiktok as _tt
    from yt_dlp.utils import ExtractorError as _EE
    _orig = _tt.TikTokUserIE._download_json

    def _dj(self, url_or_request, *a, **k):
        if not (isinstance(url_or_request, str) and "/api/creator/item_list" in url_or_request):
            return _orig(self, url_or_request, *a, **k)
        last = None
        tries = ((True, False), (False, False), (True, True))
        for i, (imp, new_dev) in enumerate(tries):
            kw = dict(k)
            if imp:
                kw["impersonate"] = True
            if new_dev and isinstance(kw.get("query"), dict):
                kw["query"] = dict(kw["query"], device_id=str(random.randint(7250000000000000000, 7325099899999994577)))
            try:
                res = _orig(self, url_or_request, *a, **kw)
                sys.stderr.write("RADAR item_list ok try=%d imp=%d\n" % (i, imp))
                sys.stderr.flush()
                try:
                    for it in (res or {}).get("itemList") or []:
                        n = _followers(it)
                        if n is not None:
                            _say_followers(n)
                            break
                except Exception:
                    pass
                return res
            except _EE as e:
                last = e
                sys.stderr.write("RADAR item_list fail try=%d imp=%d %s\n" % (i, imp, str(e)[:150].replace("\n", " ")))
                sys.stderr.flush()
                if i + 1 < len(tries):
                    time.sleep(1.0 + 1.5 * i)
        raise last

    _tt.TikTokUserIE._download_json = _dj
except Exception as e:
    sys.stderr.write("RADAR patch off %r\n" % (e,))
try:
    _ogud = _tt.TikTokUserIE._get_universal_data

    def _gud(self, webpage, display_id, *a, **k):
        d = _ogud(self, webpage, display_id, *a, **k)
        try:
            _say_followers(_followers(((d or {}).get("webapp.user-detail") or {}).get("userInfo")))
        except Exception:
            pass
        return d

    _tt.TikTokUserIE._get_universal_data = _gud
except Exception as e:
    sys.stderr.write("RADAR followers off %r\n" % (e,))
yt_dlp.main(sys.argv[1:])
"""
TOOL_PROBE = r"""
import yt_dlp
try:
    y = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    t = y._get_available_impersonate_targets()
    print(yt_dlp.version.__version__, "impersonare:", len(t), ", ".join(str(x[0]) for x in t[:3]))
except Exception as e:
    print(yt_dlp.version.__version__, "impersonare: ?", repr(e)[:120])
"""


def clean_user(text):
    """'@nume', 'nume', 'https://www.tiktok.com/@nume', '.../@nume/video/123?x' -> 'nume' (or None)."""
    t = (text or "").strip()
    m = re.search(r"tiktok\.com/@([\w.\-]+)", t)
    if m:
        t = m.group(1)
    t = t.lstrip("@").strip().strip("/")
    return t if re.fullmatch(r"[\w.\-]{2,40}", t) else None


def _dedupe(names):
    out, seen = [], set()
    for u in names:
        u = (u or "").strip().strip(".").lstrip("@")
        if u and u.lower() not in seen and re.fullmatch(r"[\w.\-]{2,40}", u):
            seen.add(u.lower())
            out.append(u)
    return out


def users_from_text(text):
    """Every TikTok account in pasted text: '@nume', tiktok.com/@nume links, or one name per line."""
    found = [m.group(1) or m.group(2) for m in
             re.finditer(r"tiktok\.com/@([\w.\-]+)|(?<![\w@])@([\w.\-]{2,40})", text or "")]
    if not found:
        found = [u for u in (clean_user(line) for line in (text or "").splitlines()) if u]
    return _dedupe(found)


def _following_from_json(obj):
    """Usernames under any "Following..." part of TikTok's data export (not "Follower...")."""
    out = []

    def walk(o, inside):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower()
                flag = inside or ("following" in kl and "follower" not in kl)
                if "follower" in kl or "block" in kl:
                    flag = False
                if isinstance(v, (dict, list)):
                    walk(v, flag)
                elif flag and kl.replace(" ", "").replace("_", "") in ("username", "uniqueid") and isinstance(v, str):
                    out.append(v)
        elif isinstance(o, list):
            for item in o:
                walk(item, inside)
    walk(obj, False)
    return out


PROFILE_KEYS = ("biodescription", "likesreceived", "birthdate", "emailaddress", "profilephoto", "followingcount",
                "followercount", "telephonenumber")


def _owner_from_json(obj):
    """The export owner's own username: the dict holding "userName" next to profile fields
    (Profile -> Profile Info -> ProfileMap in TikTok's export)."""
    stack = [(obj, 0)]
    while stack:
        o, depth = stack.pop()
        if depth > 10:
            continue
        if isinstance(o, dict):
            keys = {str(k).lower().replace(" ", "").replace("_", ""): v for k, v in o.items()}
            name = keys.get("username")
            if isinstance(name, str) and any(k in keys for k in PROFILE_KEYS):
                return clean_user(name)
            stack.extend((v, depth + 1) for v in o.values() if isinstance(v, (dict, list)))
        elif isinstance(o, list):
            stack.extend((v, depth + 1) for v in o[:200] if isinstance(v, (dict, list)))
    return None


def parse_export(data: bytes, name: str = ""):
    """TikTok's "Download your data" file (.zip, .json or .txt) -> (accounts you follow, your own username)."""
    import io
    import zipfile
    files = []
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                if n.lower().endswith((".json", ".txt")) and z.getinfo(n).file_size < 300 << 20:
                    files.append((n, z.read(n).decode("utf-8", "replace")))
    else:
        files.append((name or "export", data.decode("utf-8-sig", "replace")))
    users, owner = [], None
    for fname, text in files:
        low = Path(fname).name.lower()
        if text.lstrip().startswith(("{", "[")):
            try:
                obj = json.loads(text)
            except ValueError:
                continue
            users += _following_from_json(obj)
            owner = owner or _owner_from_json(obj)
        elif "following" in low and "follower" not in low:
            users += re.findall(r"(?im)^\s*user\s*name\s*:\s*@?([\w.\-]+)", text)
        elif low in ("profile info.txt", "profile information.txt") and not owner:
            m = re.search(r"(?im)^\s*user\s*name\s*:\s*@?([\w.\-]+)", text)
            owner = clean_user(m.group(1)) if m else None
    users = _dedupe(users)
    if owner:
        users = [u for u in users if u.lower() != owner.lower()]
    return users, owner


def users_from_export(data: bytes, name: str = ""):
    """The accounts you follow, from TikTok's "Download your data" file (.zip, .json or .txt)."""
    return parse_export(data, name)[0]


def friendly_error(err):
    e = (err or "").lower()
    if "all of their videos are private" in e:
        return "contul nu are clipuri publice"
    if "private" in e:
        return "contul e privat"
    if "does not have any videos" in e:
        return "contul nu are clipuri"
    if "unable to extract secondary user id" in e:
        # yt-dlp says this for a wrong / deleted name, but also when TikTok blocks the check for a while
        return "nu găsesc contul (nume greșit sau cont șters) sau TikTok a blocat verificarea"
    if "failed to parse json" in e or "expecting value" in e or "unable to download json" in e:
        return "TikTok n-a dat lista de clipuri de data asta"
    if "429" in e or "too many requests" in e or "rate limit" in e:
        return "TikTok cere o pauză (prea multe cereri)"
    if "404" in e or "not found" in e or "doesn't exist" in e:
        return "nu găsesc contul (verifică numele)"
    if "timed out" in e or "timeout" in e or "connection" in e or "network" in e or "getaddrinfo" in e:
        return "fără internet sau TikTok nu răspunde"
    if "keeps sending the same page" in e:
        return "TikTok a trimis aceeași pagină de mai multe ori"
    lines = [ln for ln in (err or "").strip().splitlines() if ln.strip() and not ln.startswith("RADAR ")]
    last = lines[-1] if lines else "eroare necunoscută"
    return re.sub(r"^ERROR: (\[[^\]]+\] )?", "", last)[:160]


def error_kind(err):
    """"hard": the account itself (private, no clips, no such name) - look again only after hours.
    "soft": TikTok said no for now - try again in a few minutes."""
    e = (err or "").lower()
    if ("private" in e or "does not have any videos" in e
            or (("404" in e or "not found" in e or "doesn't exist" in e) and "json" not in e)):
        return "hard"
    return "soft"


def clip_rate_info(v, now):
    """(views per hour, measured between checks?) like the page shows it: gained between the last two checks at
    least 30 min apart, else the average since it was posted."""
    h = v.get("hist") or []
    for older in reversed(h[:-1]):
        if h[-1][0] - older[0] >= 1800:
            return (h[-1][1] - older[1]) / ((h[-1][0] - older[0]) / 3600), True
    age_h = max(0.25, (now - (v.get("ts") or now)) / 3600)
    return (v.get("views") or 0) / age_h, False


def clip_rate(v, now):
    return clip_rate_info(v, now)[0]


def short_num(n):
    n = float(n or 0)
    if n >= 1e6:
        return f"{n / 1e6:.1f}".replace(".", ",").replace(",0", "") + "M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return str(int(n))


def _ps_str(s):
    return "'" + str(s).replace("'", "''") + "'"


def windows_toast(title, text, url=None):
    """A Windows notification (bottom right, with its sound) through PowerShell; a click opens `url`.
    -> None, or why it could not be shown."""
    if os.name != "nt":
        return "notificările merg doar pe Windows"
    from xml.sax.saxutils import escape, quoteattr
    launch = f" launch={quoteattr(url)} activationType=\"protocol\"" if url else ""
    xml = (f"<toast{launch}><visual><binding template=\"ToastGeneric\"><text>{escape(title)}</text>"
           f"<text>{escape(text)}</text></binding></visual><audio src=\"ms-winsoundevent:Notification.Default\"/></toast>")
    ps = ("try{$ErrorActionPreference='Stop';"
          "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
          "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]|Out-Null;"
          "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
          f"$x.LoadXml({_ps_str(xml)});"
          "$t=[Windows.UI.Notifications.ToastNotification]::new($x);"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
          + _ps_str(r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe") + ").Show($t)"
          "}catch{[Console]::Error.WriteLine($_.Exception.Message);exit 1}")
    exe = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    exe = str(exe) if exe.exists() else "powershell.exe"
    try:
        r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand",
                            base64.b64encode(ps.encode("utf-16-le")).decode("ascii")],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW)
    except Exception as e:  # noqa: BLE001
        return str(e)[:200] or e.__class__.__name__
    if r.returncode:
        out = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()
        return out[-300:] or f"cod {r.returncode}"
    return None


class Radar:
    def __init__(self, app_dir: Path, clips_dir, log=print, ffmpeg=None, python=None, notify=None, open_url=None):
        self.file = Path(app_dir) / "radar.json"
        self.notify = notify                  # (title, text, url) -> error or None; default: windows_toast
        self.open_url = open_url              # where a click on the notification leads (the Radar page)
        self.clips_dir = clips_dir            # callable -> Path of the "clipuri" folder
        self.log = log
        self.ffmpeg = ffmpeg                  # callable -> ffmpeg path or None
        self.py = python or sys.executable
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.tool = {"state": "unknown", "version": "", "msg": ""}
        self.active = set()                   # accounts being checked right now
        self.dl_active = 0
        self.downloads = {}                   # video id -> {"state", "msg"}
        self.rev = 1                          # goes up on every change: the page only re-reads what changed
        self.tool_lock = threading.Lock()     # one yt-dlp install / update at a time
        self.diag_lock = threading.Lock()
        self._forced_update = 0.0
        self._dirty = False
        self._written = 0.0
        self.data = self._load()
        self._dl_queue = []
        self._dl_wake = threading.Event()
        self._checks_done = deque(maxlen=120)  # when the last checks ended (how fast the search goes now)
        self._new_log = deque(maxlen=2000)     # (time, new clips) per check, for "clipuri noi în ultima oră"
        self._hot_pending = []                 # clips that started to explode, not announced yet
        self._hot_sent = 0.0
        self._hot_last = max([v.get("hot_at") or 0 for v in self.data["videos"].values()] or [0])
        threading.Thread(target=self._loop, daemon=True).start()
        for _ in range(DL_WORKERS):
            threading.Thread(target=self._dl_loop, daemon=True).start()

    # ------------------------------------------------------------ storage
    def _load(self):
        d = {}
        try:
            d = json.loads(self.file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            d = {}
        d.setdefault("accounts", [])
        d.setdefault("videos", {})
        d.setdefault("interval_h", 2)
        d.setdefault("per_account", 15)
        if not d.get("per_v2"):           # (2026-10-05: 15 newest clips is one request; only the last day counts)
            if d["per_account"] not in PER_ACCOUNT or d["per_account"] == 30:
                d["per_account"] = 15
            d["per_v2"] = True
        d.setdefault("hot_per_hour", 20000)
        d.setdefault("hot_seen_at", 0)
        d.setdefault("tool_updated", 0)
        d.setdefault("syncs", {})
        d.setdefault("workers", 2)
        d.setdefault("only_new", True)           # new accounts added: only they are checked if the rest is recent
        if not d.get("fast_v1"):                 # one time: the faster radar starts with 4 checks at once
            d["fast_v1"] = True
            if d.get("workers") in (None, 2, 3):
                d["workers"] = 4
        for a in d["accounts"]:
            a["sources"] = [str(x).lower() for x in (a.get("sources") or ["manual"])]   # sections, lower-case
            if a.get("error") and "retry_at" not in a:
                a["queued"] = True            # failed with an older version: try again now, the new way
        return d

    def _save(self, force=True):
        """Call with self.lock held. The page sees the change at once (rev); the file is written at once for
        what you do, and at most every SAVE_GAP_S for check results (many accounts = a big file)."""
        self.rev += 1
        if not force and time.time() - self._written < SAVE_GAP_S:
            self._dirty = True
            return
        self._write()

    def _write(self):
        self._dirty = False
        self._written = time.time()
        tmp = self.file.with_name(self.file.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.file)
        except OSError:
            pass

    def flush(self):
        """Writes check results still waiting (nothing changes for the page)."""
        with self.lock:
            if self._dirty:
                self._write()

    # ------------------------------------------------------------ public API (thread-safe)
    def state(self, since=None, live=False):
        """Everything the page shows. live=True, or since = the rev the page already has and nothing changed:
        only the small live part (rev, what is being checked now, yt-dlp status)."""
        now = time.time()
        if live or (since is not None and since == self.rev):
            with self.lock:
                prog = self._progress(now)
            return {"same": since is not None and since == self.rev, "live": True, "rev": self.rev,
                    "busy": sorted(self.active), "tool": dict(self.tool), "now": now, "dl": self._dl_counts(),
                    "progress": prog, "hot_last": self._hot_last}
        with self.lock:
            rev = self.rev
            prog = self._progress(now)
            hot_last = self._hot_last
            accs = [dict(a) for a in self.data["accounts"]]
            vids = [dict(v) for v in self.data["videos"].values()]
            interval = self.data["interval_h"]
            per = self.data["per_account"]
            workers = self.data.get("workers") or 2
            syncs = json.loads(json.dumps(self.data["syncs"]))
        sections = {}
        for a in accs:
            for src in a.get("sources") or ["manual"]:
                sections[src] = sections.get(src, 0) + 1
        src_of = {a["user"].lower(): a.get("sources") or ["manual"] for a in accs}
        for a in accs:
            a["next"] = max(now, self._due_at(a, interval * 3600)) if a.get("last_check") else now
            if (a.get("hold_until") or 0) <= now:
                a.pop("hold_until", None)
        for v in vids:
            age_h = max(0.25, (now - (v.get("ts") or now)) / 3600)
            v["per_hour"] = round((v.get("views") or 0) / age_h)
            h = v.get("hist") or []
            # views gained per hour between the last two checks at least 30 min apart
            v["recent"] = None
            for older in reversed(h[:-1]):
                if h[-1][0] - older[0] >= 1800:
                    v["recent"] = round((h[-1][1] - older[1]) / ((h[-1][0] - older[0]) / 3600))
                    break
            v.pop("hist", None)
            dl = self.downloads.get(v["id"])
            if dl:
                v["dl"] = dl
            v["sources"] = src_of.get((v.get("user") or "").lower(), [])
        vids.sort(key=lambda v: -(v.get("ts") or 0))
        return {"accounts": accs, "videos": vids, "interval_h": interval, "per_account": per,
                "workers": workers, "worker_choices": WORKERS, "dl": self._dl_counts(),
                "intervals": INTERVALS_H, "per_choices": PER_ACCOUNT, "tool": dict(self.tool), "busy": sorted(self.active),
                "sections": [{"source": k, "count": v, "sync": syncs.get(k)} for k, v in
                             sorted(sections.items(), key=lambda kv: (kv[0] == "manual", kv[0].lower()))],
                "now": now, "rev": rev, "progress": prog, "hot_last": hot_last,
                "hot_per_hour": self.data.get("hot_per_hour") or 0, "hot_choices": HOT_CHOICES,
                "hot_seen_at": self.data.get("hot_seen_at") or 0, "keep_h": KEEP_S // 3600,
                "only_new": self.data.get("only_new") is not False, "hold_h": RECENT_HOLD_H}

    def _progress(self, now):
        """The page's status bar (call with self.lock held): how many accounts are checked for this interval and
        how many wait (new, overdue, "Verifică toate acum"), when that ends at the current speed, when the next
        account is due, the clips found in the last hour and the accounts with a problem. held: accounts that
        wait because only the newly added ones are checked now (until hold_end at the latest)."""
        iv = self.data["interval_h"] * 3600
        total, left, errors, nxt = len(self.data["accounts"]), 0, 0, None
        fresh = held = 0
        hold_end = None
        for a in self.data["accounts"]:
            t = self._due_at(a, iv)
            if t <= now:
                left += 1
                if not a.get("last_check"):
                    fresh += 1
            elif nxt is None or t < nxt[0]:
                nxt = (t, a["user"])
            if a.get("error"):
                errors += 1
            h = a.get("hold_until") or 0
            normal = a.get("retry_at") or (a.get("last_check") or 0) + iv
            if h > now and h > normal and not a.get("queued"):      # waits only because of the hold
                held += 1
                hold_end = h if hold_end is None else max(hold_end, h)
        eta = None
        if left:
            run = []                       # the newest unbroken run of checks (an idle gap would slow the estimate)
            for t in reversed(self._checks_done):
                if t < now - 600 or (run and run[-1] - t > 60):
                    break
                run.append(t)
            if len(run) >= 4 and run[0] - run[-1] >= 5:
                rate = (len(run) - 1) / (run[0] - run[-1])                     # accounts per second, measured
            else:
                w = self.data.get("workers") if self.data.get("workers") in WORKERS else 2
                rate = w / (sum(PAUSE_S) / 2 + 3.0)                            # ~3 s a check + the pause
            eta = now + left / rate
        batch_total = batch_left = 0
        if held:                           # the new accounts this hold is for: how many are checked already
            since = (self.data.get("hold_at") or now) - 5
            for a in self.data["accounts"]:
                if (a.get("added") or 0) >= since:
                    batch_total += 1
                    if not a.get("last_check") or a.get("queued"):
                        batch_left += 1
        return {"total": total, "left": left, "errors": errors, "eta": eta,
                "next": {"at": nxt[0], "user": nxt[1]} if nxt else None,
                "new_hour": sum(n for t, n in self._new_log if t > now - 3600),
                "fresh": fresh, "held": held, "hold_end": hold_end,
                "batch_total": batch_total, "batch_left": batch_left}

    def _hold_recent(self, now):
        """Accounts were just added (call with self.lock held): with "only the new accounts" on, the accounts
        checked fine less than RECENT_HOLD_H ago are not checked again before that, so the Radar looks only at
        the new ones now instead of starting a whole round. Accounts never checked, with problems, or checked
        longer ago keep their schedule. Returns how many wait."""
        if self.data.get("only_new") is False or not any(not a.get("last_check") for a in self.data["accounts"]):
            return 0
        n = 0
        for a in self.data["accounts"]:
            last = a.get("last_check") or 0
            if last and not a.get("error") and not a.get("queued") and now - last < RECENT_HOLD_H * 3600:
                a["hold_until"] = max(a.get("hold_until") or 0, last + RECENT_HOLD_H * 3600)
                n += 1
        if n:
            self.data["hold_at"] = now           # the accounts added from here on are "the new ones"
        return n

    def add(self, text):
        user = clean_user(text)
        if not user:
            return "Scrie numele contului (ex. @nume) sau linkul lui de TikTok."
        with self.lock:
            a = self._acc(user)
            if a and "manual" in a["sources"]:
                return f"@{user} e deja în listă."
            if a:
                a["sources"].append("manual")
            else:
                self.data["accounts"].append({"user": user, "added": time.time(), "last_check": 0, "error": "",
                                              "sources": ["manual"]})
                self._hold_recent(time.time())
            self._save()
        self.wake.set()
        return None

    def _acc(self, user):
        u = (user or "").lower()
        return next((a for a in self.data["accounts"] if a["user"].lower() == u), None)

    def add_many(self, names, source=None):
        """Several accounts at once (a pasted list or the data export), into the "manual" section or into a
        source profile's section (without removing anything). Returns (added, already there)."""
        src = (clean_user(source) if source else None) or "manual"
        src = src.lower()
        added = already = fresh = 0
        with self.lock:
            now = time.time()
            for u in names:
                a = self._acc(u)
                if a is None:
                    self.data["accounts"].append({"user": u, "added": now, "last_check": 0, "error": "",
                                                  "sources": [src]})
                    added += 1
                    fresh += 1
                elif src in a["sources"]:
                    already += 1
                else:
                    a["sources"].append(src)
                    added += 1
            if fresh:
                self._hold_recent(now)
            if added:
                self._save()
        self.wake.set()
        return added, already

    def sync(self, source, items, replace=True, dry=False):
        """The accounts a profile follows (its "Following" list) become exactly the section of that profile:
        new ones are added, the ones it no longer follows leave the section (and the radar, unless another
        section or you added them too). With replace=False nothing leaves. items: [(username, followers or
        None)]. dry=True only counts what would change. Returns the counts."""
        src = (clean_user(source) or "").lower()
        if not src or src == "manual":
            raise ValueError("Lipsește numele profilului din care vin urmăririle (ex. @contul.meu).")
        names = {}
        for u, f in items:
            u = clean_user(u)
            if u and u.lower() != src.lower():
                names.setdefault(u.lower(), (u, f))
        if not names:
            raise ValueError("Lista de urmăriri a venit goală, așa că nu schimb nimic.")
        with self.lock:
            now = time.time()
            by = {a["user"].lower(): a for a in self.data["accounts"]}
            added = sum(1 for k in names if k not in by or src not in by[k]["sources"])
            leaving = [a for a in self.data["accounts"]
                       if src in a["sources"] and a["user"].lower() not in names] if replace else []
            res = {"source": src, "total": len(names), "added": added, "removed": len(leaving), "time": now}
            if dry:
                return res
            fresh = 0
            for key, (u, f) in names.items():
                a = by.get(key)
                if a is None:
                    a = by[key] = {"user": u, "added": now, "last_check": 0, "error": "", "sources": []}
                    self.data["accounts"].append(a)
                    fresh += 1
                if src not in a["sources"]:
                    a["sources"].append(src)
                if f is not None:
                    a["followers"] = f
                    a["followers_at"] = now
            gone = set()
            for a in leaving:
                a["sources"].remove(src)
                if not a["sources"]:
                    gone.add(a["user"].lower())
            if gone:
                self.data["accounts"] = [a for a in self.data["accounts"] if a["user"].lower() not in gone]
                self.data["videos"] = {k: v for k, v in self.data["videos"].items()
                                       if (v.get("user") or "").lower() not in gone or v.get("file")}
            res["held"] = self._hold_recent(now) if fresh else 0
            self.data["syncs"][src] = res
            self._save()
        self.wake.set()
        return res

    def remove_all(self, source=None):
        source = (source or "").lower() or None
        with self.lock:
            if source:
                for a in self.data["accounts"]:
                    if source in a["sources"]:
                        a["sources"].remove(source)
                self.data["syncs"].pop(source, None)
            else:
                for a in self.data["accounts"]:
                    a["sources"] = []
                self.data["syncs"] = {}
            gone = {a["user"].lower() for a in self.data["accounts"] if not a["sources"]}
            self.data["accounts"] = [a for a in self.data["accounts"] if a["sources"]]
            self.data["videos"] = {k: v for k, v in self.data["videos"].items()
                                   if (v.get("user") or "").lower() not in gone or v.get("file")}
            self._save()

    def remove(self, user, source=None):
        """Out of one section (source), or out of the radar altogether (source=None)."""
        source = (source or "").lower() or None
        with self.lock:
            a = self._acc(user)
            if a is None:
                return
            if source and source in a["sources"] and len(a["sources"]) > 1:
                a["sources"].remove(source)
            else:
                self.data["accounts"] = [x for x in self.data["accounts"] if x is not a]
                self.data["videos"] = {k: v for k, v in self.data["videos"].items()
                                       if (v.get("user") or "").lower() != a["user"].lower() or v.get("file")}
            self._save()

    def check_now(self, user=None, only_errors=False):
        """Check again now: one account, those with problems, or all (asked by you: no account waits for the
        "only the new accounts" hold any more)."""
        n = 0
        with self.lock:
            for a in self.data["accounts"]:
                if (user is None or a["user"].lower() == user.lower()) and (not only_errors or a.get("error")):
                    a["queued"] = True
                    a.pop("retry_at", None)
                    a.pop("hold_until", None)
                    n += 1
            self._save()
        self.wake.set()
        return n

    def settings(self, interval_h=None, per_account=None, workers=None, hot_per_hour=None, only_new=None):
        with self.lock:
            if interval_h in INTERVALS_H:
                self.data["interval_h"] = interval_h
            if per_account in PER_ACCOUNT:
                self.data["per_account"] = per_account
            if workers in WORKERS:
                self.data["workers"] = workers
            if hot_per_hour in HOT_CHOICES:
                self.data["hot_per_hour"] = hot_per_hour
            if isinstance(only_new, bool):
                self.data["only_new"] = only_new
                if not only_new:                 # switched off: the accounts waiting for it go back to normal
                    for a in self.data["accounts"]:
                        a.pop("hold_until", None)
            self._save()
        self.wake.set()

    def hide(self, ids, hidden=True):
        """Clips you don't want: out of the lists (and of "the best"), until shown again."""
        n = 0
        with self.lock:
            for i in ids or []:
                v = self.data["videos"].get(str(i))
                if v is not None:
                    if hidden:
                        v["hidden"] = True
                    else:
                        v.pop("hidden", None)
                    n += 1
            if n:
                self._save()
        return n

    def mark_seen(self, ids=None):
        """The NEW mark goes away (for these clips, or for all)."""
        n = 0
        with self.lock:
            for k, v in self.data["videos"].items():
                if v.get("new") and (ids is None or k in ids):
                    v["new"] = False
                    n += 1
            if n:
                self._save()
        return n

    def download(self, ids):
        added = 0
        with self.lock:
            for i in ids or []:
                i = str(i)
                v = self.data["videos"].get(i)
                if v is None or self.downloads.get(i, {}).get("state") in ("queued", "running"):
                    continue
                if v.get("file") and self._in_clips(v["file"]):
                    self._dl(i, "done", v["file"])          # already there
                    continue
                self._dl(i, "queued", "în coadă")
                self._dl_queue.append(i)
                added += 1
        self._dl_wake.set()
        return added

    def cancel_downloads(self):
        """Clips still waiting in the download queue stay where they are (the ones downloading now finish)."""
        with self.lock:
            q, self._dl_queue = self._dl_queue, []
            for i in q:
                self.downloads.pop(i, None)
            self.rev += 1
        return len(q)

    def _in_clips(self, name):
        try:
            return (Path(self.clips_dir()) / name).exists()
        except Exception:  # noqa: BLE001
            return True

    def _dl_counts(self):
        c = {"queued": 0, "running": 0, "done": 0, "error": 0}
        for d in list(self.downloads.values()):
            c[d.get("state")] = c.get(d.get("state"), 0) + 1
        return c

    def _dl(self, vid, state, msg):
        self.downloads[vid] = {"state": state, "msg": msg}
        self.rev += 1

    # ------------------------------------------------------------ yt-dlp
    def _run(self, args, timeout):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        r = subprocess.run([self.py] + args, capture_output=True, timeout=timeout, env=env,
                           creationflags=NO_WINDOW)
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")

    def _ensure_tool(self, force_update=False):
        """yt-dlp present and recent enough (installs / updates it in this Python)."""
        with self.tool_lock:
            return self._ensure_tool_locked(force_update)

    def _ensure_tool_locked(self, force_update):
        try:
            code, out, _ = self._run(["-m", "yt_dlp", "--version"], 120)
        except Exception:  # noqa: BLE001
            code, out = 1, ""
        stale = time.time() - (self.data.get("tool_updated") or 0) > UPDATE_EVERY_S
        if code == 0 and not force_update and not stale:
            self.tool.update(state="ok", version=out.strip(), msg="")
            return True
        self.tool.update(state="installing", msg="instalez yt-dlp..." if code else "actualizez yt-dlp...")
        ok = False
        for spec in ("yt-dlp[default,curl-cffi]", "yt-dlp[default]", "yt-dlp"):
            try:
                c2, _, err = self._run(["-m", "pip", "install", "--upgrade", "--quiet", "--disable-pip-version-check",
                                        spec], 900)
            except Exception as e:  # noqa: BLE001
                c2, err = 1, str(e)
            if c2 == 0:
                ok = True
                break
        try:
            code, out, _ = self._run(["-m", "yt_dlp", "--version"], 120)
        except Exception:  # noqa: BLE001
            code, out = 1, ""
        if code == 0:
            with self.lock:
                self.data["tool_updated"] = time.time()
                self._save()
            self.tool.update(state="ok", version=out.strip(), msg="")
            return True
        self.tool.update(state="error", msg="nu am putut instala yt-dlp (fără internet?)" if not ok else
                         "yt-dlp nu pornește")
        return False

    def _fetch(self, user, count):
        t0 = time.time()
        code, out, err = self._run(
            ["-c", YTDLP_RUNNER, "--flat-playlist", "-J", "--playlist-end", str(count), "--extractor-retries", "3",
             "--socket-timeout", "30", "--no-warnings", "--ignore-config", f"https://www.tiktok.com/@{user}"], 300)
        notes = [ln[6:].strip() for ln in err.splitlines() if ln.startswith("RADAR ")]
        rest = "\n".join(ln for ln in err.splitlines() if not ln.startswith("RADAR ")).strip()
        if code != 0 or not out.strip() or "{" not in out:
            self._diag(f"@{user} FAIL {time.time() - t0:.1f}s | {'; '.join(notes)} | {rest[-300:]}")
            raise RuntimeError(rest or f"yt-dlp a ieșit cu codul {code}")
        info = json.loads(out[out.index("{"):])
        # the follower count TikTok sent with the profile / the clip list (the last one heard is the newest)
        for n in notes:
            m = re.fullmatch(r"followers (\d+)", n)
            if m:
                info["_followers"] = int(m.group(1))
        self._diag(f"@{user} ok {len(info.get('entries') or [])} clipuri {time.time() - t0:.1f}s | "
                   f"{'; '.join(x for x in notes if not x.startswith('followers '))}"
                   + (f" | urmăritori {info['_followers']}" if "_followers" in info else ""))
        return info

    def _diag(self, line):
        """radar_diagnostic.log next to the app: one line per check (what TikTok answered), for repairs."""
        f = self.file.parent / DIAG_FILE
        try:
            with self.diag_lock:
                if f.exists() and f.stat().st_size > DIAG_MAX:
                    keep = f.read_bytes()[-DIAG_MAX // 3:]
                    f.write_bytes(keep[keep.find(b"\n") + 1:])
                with open(f, "a", encoding="utf-8") as fh:
                    fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line.replace("\n", " ")[:900] + "\n")
        except OSError:
            pass

    def _probe_tool(self):
        try:
            code, out, err = self._run(["-c", TOOL_PROBE], 120)
            self._diag(f"yt-dlp {out.strip() or err.strip()[-200:]}")
        except Exception as e:  # noqa: BLE001
            self._diag(f"yt-dlp probe failed: {e!r}")

    # ------------------------------------------------------------ checking
    def _due_at(self, a, iv):
        if a.get("queued") or not a.get("last_check"):
            return 0
        due = a.get("retry_at") or a["last_check"] + iv
        hold = a.get("hold_until")             # "only the new accounts": checked recently, waits a little longer
        return max(due, hold) if hold else due

    def _take(self):
        """The most overdue account nobody is checking yet (marked as being checked), or None.
        Among accounts due right away (new ones, or all of them after "Verifică toate acum"), the ones never
        checked go first, then the oldest checks, so a big list is not walked again from the top."""
        now = time.time()
        with self.lock:
            iv = self.data["interval_h"] * 3600
            busy = {u.lower() for u in self.active}
            best = None
            for a in self.data["accounts"]:
                if a["user"].lower() in busy:
                    continue
                t = self._due_at(a, iv)
                if t > now:
                    continue
                k = (t, a.get("last_check") or 0)
                if best is None or k < best[0]:
                    best = (k, a["user"])
            if best:
                self.active.add(best[1])
                return best[1]
        return None

    def _any_due(self):
        now = time.time()
        with self.lock:
            iv = self.data["interval_h"] * 3600
            return any(self._due_at(a, iv) <= now for a in self.data["accounts"])

    def _loop(self):
        first = True
        while True:
            self.wake.wait(60)
            self.wake.clear()
            if not self._any_due():
                self.flush()
                continue
            if first:
                if not self._ensure_tool():
                    time.sleep(300)
                    continue
                first = False
                self._probe_tool()
            elif time.time() - (self.data.get("tool_updated") or 0) > UPDATE_EVERY_S:
                self._ensure_tool()
            stats = {"done": 0, "new": 0, "with_new": 0, "failed": 0}
            with self.lock:
                n = self.data.get("workers") if self.data.get("workers") in WORKERS else 2
            workers = [threading.Thread(target=self._worker, args=(stats,), daemon=True) for _ in range(n)]
            for w in workers:
                w.start()
                time.sleep(random.uniform(1.0, 2.5))          # not all at the same second
            for w in workers:
                w.join()
            self._hot_flush(force=True)
            self.flush()
            if stats["new"] or stats["failed"]:
                self.log(f"Radar: {stats['done']} conturi verificate, {stats['new']} clipuri noi"
                         + (f" (la {stats['with_new']} conturi)" if stats["new"] else "")
                         + (f", {stats['failed']} cu probleme (vezi Radarul)" if stats["failed"] else ""))

    def _worker(self, stats):
        while True:
            user = self._take()
            if not user:
                return
            try:
                n, err = self._check(user)
            except Exception as e:  # noqa: BLE001  (never let one account stop the round)
                n, err = 0, friendly_error(str(e))
                self._done(user, error=err, kind="soft")
            finally:
                with self.lock:
                    self.active.discard(user)
            with self.lock:
                stats["done"] += 1
                stats["new"] += n
                stats["with_new"] += 1 if n else 0
                stats["failed"] += 1 if err else 0
                if n:
                    self._new_log.append((time.time(), n))
                # adaptive: a refusal makes every worker wait longer, good checks bring the pause back down
                slow = getattr(self, "_slow", 1.0)
                slow = min(SLOW_MAX, slow * 1.6) if (err and error_kind(err) == "soft") else max(1.0, slow * 0.92)
                self._slow = slow
            self._hot_flush()
            time.sleep(random.uniform(*PAUSE_S) * slow)

    def _hot_flush(self, force=False):
        """Clips that started to explode: one line in the log and one Windows notification for all of them, at
        most every HOT_GAP_S (the end of a round sends what is left at once)."""
        with self.lock:
            if not self._hot_pending or (not force and time.time() - self._hot_sent < HOT_GAP_S):
                return
            items, self._hot_pending = self._hot_pending, []
            self._hot_sent = time.time()
        items.sort(key=lambda x: -x["rate"])
        n = len(items)
        top = ", ".join(f"@{x['user']} ({short_num(x['rate'])} viz./oră)" for x in items[:3]) + (" ..." if n > 3 else "")
        title = "Radar: un clip explodează" if n == 1 else f"Radar: {n} clipuri explodează"
        self.log(f"{title}: {top}")
        threading.Thread(target=self._toast, args=(title, top), daemon=True).start()

    def _toast(self, title, text):
        fn = self.notify or windows_toast
        try:
            err = fn(title, text, self.open_url)
        except Exception as e:  # noqa: BLE001
            err = str(e)
        if err:
            self._diag(f"notificare Windows: {err}")
        return err

    def notify_test(self):
        """'Încearcă notificarea' on the page. -> None, or why Windows did not show it."""
        return self._toast("Radar TikTok: așa arată alerta", "Când un clip trece de pragul ales, primești o "
                           "notificare ca asta. Click pe ea deschide Radarul.")

    def hot_seen(self, upto=None):
        """'Am văzut' on the alert: the clips the page showed (exploded until `upto`, its newest one) are no longer
        new alerts; one that exploded right after the page's last refresh stays."""
        now = time.time()
        try:
            upto = float(upto)
        except (TypeError, ValueError):
            upto = None
        mark = min(upto, now) if upto and upto > 0 else now
        with self.lock:
            self.data["hot_seen_at"] = max(self.data.get("hot_seen_at") or 0, mark)
            self._save()

    def _check(self, user, retried=False):
        """One account. Returns (new clips, error or "")."""
        with self.lock:
            per = self.data["per_account"]
        try:
            info = self._fetch(user, per)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            low = msg.lower()
            changed = ("unable to extract" in low or "unsupported url" in low) and "secondary user id" not in low
            if not retried and changed and time.time() - self._forced_update > FORCED_UPDATE_GAP_S:
                # TikTok changed something: a newer yt-dlp usually knows already
                self._forced_update = time.time()
                if self._ensure_tool(force_update=True):
                    return self._check(user, retried=True)
            self._done(user, error=friendly_error(msg), kind=error_kind(msg))
            return 0, friendly_error(msg)
        now = time.time()
        entries = [e for e in (info.get("entries") or []) if isinstance(e, dict) and e.get("id")]
        cut = now - KEEP_S
        new = 0
        hot = []
        with self.lock:
            acc = next((a for a in self.data["accounts"] if a["user"].lower() == user.lower()), None)
            if acc is None:                     # removed while checking
                return 0, ""
            initial = not acc.get("last_ok")
            for e in entries:
                vid = str(e["id"])
                v = self.data["videos"].get(vid)
                thumbs = [t.get("url") for t in (e.get("thumbnails") or []) if isinstance(t, dict) and t.get("url")]
                stats = {"views": e.get("view_count"), "likes": e.get("like_count"),
                         "comments": e.get("comment_count"), "shares": e.get("repost_count"),
                         "saves": e.get("save_count")}
                if v is None:
                    ts = e.get("timestamp")
                    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts < cut:
                        # posted more than 24 h ago (or an old pinned clip), or without a date: of no use (the page
                        # shows only dated clips of the last day), and _prune would drop it anyway, so at the next
                        # check it would come back counted as "new" again
                        continue
                    v = {"id": vid, "user": acc["user"], "first_seen": now, "new": not initial, "hist": []}
                    self.data["videos"][vid] = v
                    new += 0 if initial else 1
                v.update({k: val for k, val in stats.items() if val is not None})
                v.update({"desc": e.get("description") or e.get("title") or "", "ts": e.get("timestamp"),
                          "dur": e.get("duration"), "url": e.get("url") or e.get("webpage_url") or
                          f"https://www.tiktok.com/@{acc['user']}/video/{vid}",
                          "thumb": thumbs[0] if thumbs else v.get("thumb"), "checked": now})
                v["hist"] = [h[:2] for h in (v.get("hist") or [])[-(HIST_KEEP - 1):]] + [[round(now), v.get("views") or 0]]
                thr = self.data.get("hot_per_hour") or 0
                if thr and v.get("ts") and not initial:     # (an account's first check is only the starting point)
                    rate, measured = clip_rate_info(v, now)
                    age_h = (now - v["ts"]) / 3600
                    if rate >= thr and age_h <= HOT_AGE_MAX_H and (measured or age_h <= HOT_AVG_MAX_H):
                        v["hot_rate"] = max(v.get("hot_rate") or 0, round(rate))
                        if not v.get("hot_at"):
                            v["hot_at"] = now
                            hot.append({"id": vid, "user": acc["user"], "rate": round(rate), "views": v.get("views") or 0})
            acc["name"] = info.get("channel") or (entries[0].get("channel") if entries else None) or acc.get("name")
            acc["found"] = len(entries)
            acc["new_last"] = new
            if isinstance(info.get("_followers"), int):
                acc["followers"] = info["_followers"]
                acc["followers_at"] = now
            if hot:
                self._hot_pending.extend(hot)
                self._hot_last = now
            self._prune()
        self._done(user, error="")
        return new, ""

    def _done(self, user, error, kind=None):
        with self.lock:
            iv = self.data["interval_h"] * 3600
            for a in self.data["accounts"]:
                if a["user"].lower() == user.lower():
                    now = time.time()
                    a["last_check"] = now
                    a["error"] = error
                    a.pop("queued", None)
                    a.pop("hold_until", None)
                    self._checks_done.append(now)
                    if error:
                        a["fails"] = (a.get("fails") or 0) + 1
                        if kind == "hard":
                            a["retry_at"] = now + max(iv, HARD_RETRY_S)
                        else:
                            a["retry_at"] = now + min(iv, SOFT_RETRY_S * 2 ** min(a["fails"] - 1, 5))
                    else:
                        a["last_ok"] = now
                        a["fails"] = 0
                        a.pop("retry_at", None)
            self._save(force=False)

    def downloads_busy(self):
        """Clips waiting for / in their download: their record stays until the file is there."""
        return {k for k, d in list(self.downloads.items()) if d.get("state") in ("queued", "running")}

    def _prune(self):
        cut = time.time() - KEEP_S
        busy = self.downloads_busy()
        by_user = {}
        for v in self.data["videos"].values():
            by_user.setdefault((v.get("user") or "").lower(), []).append(v)
        keep = {}
        for vs in by_user.values():
            vs.sort(key=lambda v: -(v.get("ts") or 0))
            for v in vs[:MAX_PER_ACCOUNT]:
                if (v.get("ts") or v.get("first_seen") or 0) >= cut or v["id"] in busy:
                    keep[v["id"]] = v
        self.data["videos"] = keep

    # ------------------------------------------------------------ downloads
    def _dl_loop(self):
        while True:
            self._dl_wake.wait(30)
            self._dl_wake.clear()
            while True:
                with self.lock:
                    vid = self._dl_queue.pop(0) if self._dl_queue else None
                    v = dict(self.data["videos"].get(vid) or {}) if vid else None
                if not vid:
                    break
                if not v:
                    continue
                self._download_one(v)

    def _download_one(self, v):
        """yt-dlp downloads into a folder of its own next to the app; only the finished file goes into "clipuri"
        (under a hidden name first, then renamed), so a dubbing run never picks up half a clip."""
        vid = v["id"]
        self._dl(vid, "running", "se descarcă...")
        if self.tool.get("state") != "ok" and not self._ensure_tool():
            self._dl(vid, "error", self.tool.get("msg") or "yt-dlp lipsește")
            return
        work = self.file.parent / "radar_descarcari"
        name = re.sub(r"[^\w.\-]+", "_", f"{v.get('user') or 'tiktok'}_{vid}")
        try:
            dest = Path(self.clips_dir())
            dest.mkdir(parents=True, exist_ok=True)
            work.mkdir(parents=True, exist_ok=True)
            self._clean(work, name)
            args = ["-m", "yt_dlp", "--no-playlist", "--ignore-config", "--no-warnings",
                    "-S", "vcodec:h264,res,br", "-f", "bv*+ba/b", "--merge-output-format", "mp4",
                    "-o", str(work / f"{name}.%(ext)s"), v["url"]]
            ff = self.ffmpeg() if self.ffmpeg else None
            if ff:
                args[3:3] = ["--ffmpeg-location", str(Path(ff).parent)]
            code, out, err = self._run(args, 900)
            if code != 0:
                raise RuntimeError(err.strip() or f"cod {code}")
            got = [p for p in work.glob(f"{name}.*") if p.suffix.lower() in VIDEO_EXTS
                   and not re.search(r"\.f\d+$|\.temp$|\.part$", p.stem)]
            if not got:
                raise RuntimeError("fișierul nu a apărut")
            src = max(got, key=lambda p: p.stat().st_size)
            final = dest / (name + src.suffix.lower())
            self._move_in(src, final)
            with self.lock:
                if vid in self.data["videos"]:
                    self.data["videos"][vid]["file"] = final.name
                    self._save()
            self._dl(vid, "done", final.name)
            self.log(f"Radar: descărcat {final.name} în clipuri (gata de dublat)")
        except Exception as e:  # noqa: BLE001
            self._dl(vid, "error", friendly_error(str(e)))
            self.log(f"Radar: descărcarea {vid} nu a mers: {friendly_error(str(e))}")
        finally:
            self._clean(work, name)

    @staticmethod
    def _clean(work, name):
        for p in work.glob(f"{name}.*"):
            try:
                p.unlink()
            except OSError:
                pass

    @staticmethod
    def _move_in(src, final):
        """Into the clips folder in one step: a hidden ".name.part" first (the dubbing skips it), then renamed."""
        part = final.with_name("." + final.name + ".part")
        try:
            os.replace(src, part)
        except OSError:                       # another drive: copy (still under the hidden name)
            shutil.copyfile(src, part)
        os.replace(part, final)
