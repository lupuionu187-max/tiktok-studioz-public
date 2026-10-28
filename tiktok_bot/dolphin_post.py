#!/usr/bin/env python3
"""
TikTok multi-account poster prin DOLPHIN ANTY (local, de pe IP-ul tau).
Porneste fiecare profil Dolphin -> se conecteaza la browserul lui ->
urca clipul -> pune caption -> apasa Post -> inchide profilul.

Necesita: aplicatia Dolphin Anty DESCHISA (API local pe 127.0.0.1:3001).

MODURI:
  Listeaza profilurile din contul tau:
      python dolphin_post.py list

  Test pe UN profil (nume sau id), fara sa apese Post (doar pana la pre-publish):
      python dolphin_post.py post --video "videos/clip.mp4" --caption "test #fyp" --accounts cont1 --dry

  Posteaza real pe cateva conturi:
      python dolphin_post.py post --video "videos/clip.mp4" --caption "text #fyp" --accounts cont1,cont2 --delay 90

  Posteaza pe TOATE (fara CONT AI):
      python dolphin_post.py post --video "videos/clip.mp4" --caption "text #fyp" --all --delay 120
"""
import argparse, json, time, os, random, urllib.request, threading
from pathlib import Path

_api_lock = threading.Lock()  # serializeaza apelurile Dolphin (start/stop) intre thread-uri

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

ROOT = Path(__file__).resolve().parent
SECRETS = ROOT.parent / ".secrets" / "dolphin.env"
LOCAL = "http://localhost:3001/v1.0"
REMOTE = "https://dolphin-anty-api.com"
UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"
SKIP = {"CONT AI"}  # profiluri de ignorat la --all (nume exacte, legacy)
SKIP_SUBSTR = ("banat",)  # exclude orice cont cu asta in nume (ex: banate)

def is_skipped(name):
    if name in SKIP:
        return True
    low = (name or "").lower()
    return any(s in low for s in SKIP_SUBSTR)
# Butonul de publicare - potrivire EXACTA (RO: 'Posteaza', EN: 'Post').
# NU folosi contains('Post') ca prinde 'Postari' (meniu) si duce in alta pagina.
POST_XPATHS = [
    "//button[normalize-space()='Postează']",
    "//button[normalize-space()='Posteaza']",
    "//button[normalize-space()='Post']",
    "//button[@data-e2e='post_video_button']",
]

def token():
    return SECRETS.read_text(encoding="utf-8").split("=", 1)[1].strip()

def api_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def list_profiles():
    d = api_get(f"{REMOTE}/browser_profiles?limit=100",
                headers={"Authorization": "Bearer " + token()})
    items = d.get("data", d if isinstance(d, list) else [])
    return [{"id": p.get("id"), "name": p.get("name")} for p in items]

def resolve_targets(args, profiles):
    by_name = {p["name"]: p for p in profiles}
    by_id = {str(p["id"]): p for p in profiles}
    if args.all:
        return [p for p in profiles if not is_skipped(p["name"])]
    out = []
    for tok_ in args.accounts.split(","):
        tok_ = tok_.strip()
        if not tok_: continue
        if tok_ in by_name: out.append(by_name[tok_])
        elif tok_ in by_id: out.append(by_id[tok_])
        else: print(f"[!] Nu gasesc profilul '{tok_}'")
    return out

def _lh():
    return {"Authorization": "Bearer " + token()}

def dolphin_start(pid, verbose=True):
    import urllib.error
    for attempt in range(8):
        try:
            with _api_lock:
                r = api_get(f"{LOCAL}/browser_profiles/{pid}/start?automation=1", headers=_lh(), timeout=60)
            return r.get("automation", {}).get("port")
        except urllib.error.HTTPError as e:
            body = ""
            try: body = e.read().decode()
            except Exception: pass
            if verbose: print(f"    (start try {attempt+1}: {e.code} {body[:80]})")
            if "already running" in body or "DUPLICATE" in body:
                dolphin_stop(pid); time.sleep(6); continue
            if "Token refresh" in body or e.code == 500:
                time.sleep(8); continue
            raise
        except Exception as e:
            if verbose: print(f"    (start try {attempt+1}: {type(e).__name__} {e})")
            time.sleep(6)
    return None

def dolphin_stop(pid):
    try:
        with _api_lock:
            api_get(f"{LOCAL}/browser_profiles/{pid}/stop", headers=_lh(), timeout=30)
    except Exception: pass

def attach(port):
    opts = Options()
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    return webdriver.Chrome(options=opts)

def dismiss_tour(d):
    """Inchide turul de onboarding TikTok Studio (react-joyride) care blocheaza click-urile."""
    # 1) incearca butoane de skip/close
    for xp in ["//button[@data-test-id='button-skip']",
               "//button[contains(translate(.,'SKIP','skip'),'skip')]",
               "//button[@aria-label='Close']",
               "//button[contains(.,'Skip')]", "//button[contains(.,'Got it')]",
               "//button[contains(.,'Sari')]", "//button[contains(.,'Am inteles')]"]:
        try:
            b = d.find_element(By.XPATH, xp)
            if b.is_displayed():
                b.click(); time.sleep(0.6); break
        except Exception: continue
    # 2) fallback: sterge overlay-urile din DOM
    try:
        d.execute_script("""
            document.querySelectorAll('.react-joyride__overlay,.react-joyride__spotlight,[data-test-id=overlay]')
              .forEach(function(e){e.remove();});
        """)
    except Exception: pass

def post_one(prof, video, caption, dry=False):
    video = str(Path(video).resolve())
    if not os.path.exists(video):
        print(f"[!] Video lipsa: {video}"); return False
    pid, name = prof["id"], prof["name"]
    print(f"[..] {name}: pornesc profilul Dolphin...")
    port = None
    try:
        port = dolphin_start(pid)
        if not port:
            print(f"[ERR] {name}: nu am primit port de la Dolphin (app deschisa?)"); return False
        time.sleep(3)
        d = attach(port)
        d.get(UPLOAD_URL)
        wait = WebDriverWait(d, 45)
        try:
            fi = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']")))
        except Exception:
            iframe = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "iframe")))
            d.switch_to.frame(iframe)
            fi = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']")))
        fi.send_keys(video)
        print(f"[..] {name}: clip incarcat, astept procesarea...")
        time.sleep(14)
        dismiss_tour(d)
        try:
            cap = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[contenteditable='true']")))
            d.execute_script("arguments[0].scrollIntoView({block:'center'});", cap)
            time.sleep(0.5)
            try:
                cap.click()
            except Exception:
                dismiss_tour(d); time.sleep(0.5); d.execute_script("arguments[0].focus();", cap)
            time.sleep(1)
            cap.send_keys(Keys.CONTROL, "a"); cap.send_keys(Keys.DELETE)
            for ch in caption:
                cap.send_keys(ch); time.sleep(0.01)
            print(f"[..] {name}: caption pus.")
        except Exception as e:
            print(f"[warn] {name}: casuta caption negasita ({e})")
        if dry:
            time.sleep(10)
            found = False
            for xp in POST_XPATHS:
                try:
                    b = d.find_element(By.XPATH, xp)
                    found = True
                    print(f"[DRY] {name}: buton '{b.text.strip()}' GASIT, enabled={b.is_enabled()}")
                    break
                except Exception: continue
            if not found:
                print(f"[DRY] {name}: buton Post NEGASIT - trebuie ajustat selectorul")
            print(f"[DRY] {name}: m-am oprit inainte de a apasa Post (nimic publicat).")
            time.sleep(20)
            return found
        # Publica: reincearca Postează pana la redirect (upload-ul clipurilor mari dureaza).
        print(f"[..] {name}: astept finalizarea upload-ului si public...")
        posted = False
        deadline = time.time() + 480  # pana la 8 min pt fisiere mari
        clicks = 0
        while time.time() < deadline:
            dismiss_tour(d)
            btn = None
            for xp in POST_XPATHS:
                try:
                    b = d.find_element(By.XPATH, xp)
                    if b.is_displayed() and b.is_enabled():
                        btn = b; break
                except Exception: continue
            if btn:
                try:
                    d.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                    try: btn.click()
                    except Exception: d.execute_script("arguments[0].click();", btn)
                    clicks += 1
                except Exception: pass
                time.sleep(9)
                url = d.current_url
                if "upload" not in url or "/content" in url or "/posts" in url:
                    print(f"[OK] {name}: POSTAT dupa {clicks} incercari (redirect -> {url})")
                    posted = True; break
                # inca pe pagina => upload neterminat, mai asteapta
            time.sleep(12)
        if posted:
            return True
        print(f"[!] {name}: nu s-a confirmat publicarea in 8 min (upload prea lung/eroare).")
        return False
    except Exception as e:
        print(f"[ERR] {name}: {e}"); return False
    finally:
        if port:
            dolphin_stop(pid)

def cmd_post(args):
    profiles = list_profiles()
    targets = resolve_targets(args, profiles)
    if not targets:
        print("[!] Niciun profil selectat."); return
    print(f"[*] {len(targets)} conturi. Delay ~{args.delay}s.\n")
    res = {}
    for i, p in enumerate(targets):
        res[p["name"]] = post_one(p, args.video, args.caption, dry=args.dry)
        if i < len(targets) - 1:
            w = args.delay + random.randint(0, max(1, args.delay // 3))
            print(f"[..] pauza {w}s...\n"); time.sleep(w)
    ok = sum(1 for v in res.values() if v)
    print(f"\n===== GATA: {ok}/{len(targets)} =====")
    for n, v in res.items():
        print(f"  {n:20} {'OK' if v else 'verifica manual'}")

def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list")
    pp = sub.add_parser("post")
    pp.add_argument("--video", required=True)
    pp.add_argument("--caption", default="")
    pp.add_argument("--accounts", default="")
    pp.add_argument("--all", action="store_true")
    pp.add_argument("--delay", type=int, default=100)
    pp.add_argument("--dry", action="store_true")
    a = p.parse_args()
    if a.cmd == "list":
        for pr in list_profiles():
            print(f"  {pr['id']}  {pr['name']}")
    elif a.cmd == "post":
        cmd_post(a)
    else:
        p.print_help()

if __name__ == "__main__":
    main()
