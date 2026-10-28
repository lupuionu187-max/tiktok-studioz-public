"""Vmake prin site, cu contul platit al utilizatorului (nu API-ul cu credite): un Chrome separat, cu
profilul lui (vmake_profil, logat o data cu "Vmake logare.bat"), face ce ar face omul pe vmake.ai:
urca clipul, alege unealta, asteapta rezultatul si il descarca. Semnaturile cererilor le face chiar site-ul.

Ruleaza in mediul cu Playwright (_mutare\\vmake_venv), pornit de dubleaza.py:
  python vmake.py --tool sterge|imbunatateste --in clip.mp4 --out rezultat.mp4 [--model fast|quality|master]
                  [--rez original|2k|4k] [--timeout-min 60]
  python vmake.py --verifica        (doar: e logat?)
Iese cu o singura linie JSON pe stdout: {"ok": true, "out", "secunde"} sau {"ok": false, "cod", "eroare"}.
cod: "sesiune" (trebuie logare din nou), "verificare" (site-ul cere o verificare anti-robot: o face omul),
"ocupat" (profilul e folosit de alt Chrome), "eroare" (altceva; se poate reincerca)."""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILE = Path(os.environ.get("VMAKE_PROFIL") or HERE / "vmake_profil")
TOOLS = {
    "sterge": "https://vmake.ai/video-watermark-remover/upload",
    "imbunatateste": "https://vmake.ai/video-enhancer/upload",
}
MODELS = {"fast": "Fast", "quality": "Quality", "master": "Master"}
RES = {"original": "Original", "2k": "2K", "4k": "4K"}
UPLOAD_S = 20 * 60          # the clip appears on the page (upload done) within this
DOWNLOAD_S = 30 * 60        # the result file is downloaded within this
DEBUG = Path(os.environ["VMAKE_DEBUG"]) if os.environ.get("VMAKE_DEBUG") else None   # screenshots while it runs
TRANSIENT_TRIES = 3        # the whole job again after a temporary error (network, page not loading)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class Stop(Exception):
    def __init__(self, cod, msg):
        super().__init__(msg)
        self.cod = cod


def say(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, file=sys.stderr, flush=True)


def _cookies_ok(page):
    """The cookie banner (first run of a profile): only the essential cookies (optional ones stay off)."""
    try:
        b = page.locator("button:has-text('Manage Cookies')")
        if b.count() and b.first.is_visible():
            b.first.click(timeout=5000)
            page.locator("button:has-text('Save and close')").first.click(timeout=5000)
    except Exception:  # noqa: BLE001 - no banner / already answered
        pass


def _check_page(page):
    """Logged in, and no anti-robot check on the page (that one is the user's to do, never ours)."""
    html = page.content().lower()
    if "captcha" in html and page.locator("iframe[src*='captcha']").count():
        raise Stop("verificare", "Vmake cere o verificare anti-robot: deschide 'Vmake logare.bat' si treci de ea")
    if not page.locator("img[src*='avatar']").count():
        raise Stop("sesiune", "nu esti logat in Vmake (sesiunea a expirat): porneste 'Vmake logare.bat', "
                              "intra in cont, inchide fereastra")


def _card(page, name):
    return page.locator("div[class*='task-card--']").filter(has_text=name).first


def _is_active(card, label):
    """Is the option `label` (Subtitle, Fast, 2K...) the selected one in this clip's card?"""
    return bool(card.evaluate(
        """(card, label) => {
            const leaf = [...card.querySelectorAll('*')].find(e => e.childElementCount === 0 && e.textContent.trim() === label);
            for (let p = leaf; p && p !== card; p = p.parentElement)
                if (/active|selected|checked|current/i.test(String(p.className)) || p.getAttribute('aria-pressed') === 'true'
                    || p.getAttribute('aria-selected') === 'true' || p.getAttribute('data-state') === 'active') return true;
            return false;
        }""", label))


def _pick(card, label):
    el = card.get_by_text(label, exact=True).first
    try:
        el.wait_for(state="visible", timeout=20000)       # the options panel shows a moment after the upload
        card.page.wait_for_timeout(1500)
    except Exception:  # noqa: BLE001
        pass
    if _is_active(card, label):
        return
    try:
        el.click(timeout=6000)
    except Exception:  # noqa: BLE001 - something lies over it (a tip, a banner): click it the page's own way
        el.evaluate("e => { let p = e; while (p && !/^(BUTTON|LABEL|A)$/.test(p.tagName) && p.onclick == null "
                    "&& !/card|item|option|tab/i.test(String(p.className))) p = p.parentElement; (p || e).click(); }")
    card.page.wait_for_timeout(800)
    if not _is_active(card, label):
        log(f"atentie: optiunea {label} nu pare aleasa")


def _state(card):
    """'gata' (Download visible, nothing running), 'lucreaza', 'esuat' or 'asteapta' (Apply not pressed)."""
    t = card.inner_text(timeout=10000)
    low = t.lower()
    if re.search(r"\bfail|\berror\b|try again", low):
        return "esuat", t
    if re.search(r"processing|uploading|in queue|queuing|waiting", low):
        return "lucreaza", t
    dl = card.locator("button:has-text('Download')")
    if dl.count() and dl.first.is_visible() and dl.first.is_enabled():
        return "gata", t
    if "click apply" in low or "to be processed" in low:
        return "asteapta", t
    return "lucreaza", t


def run_job(p, tool, src, out, model, rez, timeout_s):
    t0 = time.time()
    work = Path(tempfile.mkdtemp(prefix="vmake-"))
    try:
        # a name of its own: the card of THIS clip is found by it among the earlier ones on the page
        # (short: the page cuts long names in the middle, "HOA President Tr...wing.mp4")
        name = f"v{int(t0 * 10) % 10_000_000:07d}{src.suffix.lower()}"
        up = work / name
        shutil.copy2(src, up)
        try:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE), channel="chrome", headless=False, accept_downloads=True,
                viewport={"width": 1280, "height": 860},
                args=["--window-position=-2400,-2400", "--window-size=1280,900"])
        except Exception as e:  # noqa: BLE001
            if "user data directory is already in use" in str(e).lower() or "lock" in str(e).lower():
                raise Stop("ocupat", "profilul Vmake e deschis in alt Chrome (inchide fereastra de logare)")
            raise
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(TOOLS[tool], wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(3000)
            _cookies_ok(page)
            _check_page(page)
            page.locator("input[type=file]").first.set_input_files(str(up), timeout=30000)
            log(f"urc {src.name} ({src.stat().st_size / 1e6:.0f} MB) la {tool}")
            page.wait_for_url("**/editor**", timeout=UPLOAD_S * 1000)
            if DEBUG:
                page.wait_for_timeout(3000)
                page.screenshot(path=str(DEBUG / f"{tool}_editor.png"))
            card = _card(page, name)
            card.wait_for(timeout=UPLOAD_S * 1000)
            log("clipul e pe pagina")
            if tool == "sterge":
                _pick(card, "Auto")
                _pick(card, "Subtitle")
            else:
                _pick(card, MODELS[model])
                _pick(card, RES[rez])
            deadline = t0 + timeout_s
            applied = 0.0            # when Apply was last pressed (0 = not yet; some pages start by themselves)
            last = ""
            while True:
                if time.time() > deadline:
                    raise Stop("eroare", f"Vmake nu a terminat in {timeout_s // 60} min")
                st, txt = _state(card)
                if st == "asteapta" and time.time() - applied > 30:
                    card.locator("button:has-text('Apply')").first.click(timeout=10000)
                    applied = time.time()
                    log("Apply apasat")
                elif st == "lucreaza" and not applied:
                    applied = time.time()           # it started by itself
                elif st == "gata" and applied:
                    break
                elif st == "esuat":
                    raise Stop("eroare", "Vmake a raportat o eroare: " + " ".join(txt.split())[:200])
                m = re.search(r"(?:processing|uploading)[^\d]{0,20}(\d{1,3})\s*%", txt, re.I)
                prog = m.group(1) + "%" if m else ""
                if prog and prog != last:
                    log(f"{tool}: {prog}")
                    last = prog
                if DEBUG:
                    page.screenshot(path=str(DEBUG / f"{tool}_{int(time.time() - t0):05d}.png"))
                    log(f"stare {st}: " + " ".join(txt.split())[:160])
                page.wait_for_timeout(5000)
            log(f"gata pe Vmake in {time.time() - t0:.0f}s; descarc")
            with page.expect_download(timeout=DOWNLOAD_S * 1000) as d:
                card.locator("button:has-text('Download')").first.click(timeout=15000)
            part = out.with_name(out.name + ".part")
            d.value.save_as(str(part))
            if part.stat().st_size < 1000:
                raise Stop("eroare", "fisierul descarcat e gol")
            part.replace(out)
            return time.time() - t0
        finally:
            ctx.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", choices=list(TOOLS))
    ap.add_argument("--in", dest="src")
    ap.add_argument("--out")
    ap.add_argument("--model", default="fast", choices=list(MODELS))
    ap.add_argument("--rez", default="2k", choices=list(RES))
    ap.add_argument("--timeout-min", type=float, default=60)
    ap.add_argument("--verifica", action="store_true")
    a = ap.parse_args()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        if a.verifica:
            try:
                ctx = p.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=False,
                                                           args=["--window-position=-2400,-2400"])
                try:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    page.goto(TOOLS["imbunatateste"], wait_until="domcontentloaded", timeout=90000)
                    page.wait_for_timeout(3000)
                    _cookies_ok(page)
                    _check_page(page)
                finally:
                    ctx.close()
                say(ok=True)
            except Stop as e:
                say(ok=False, cod=e.cod, eroare=str(e))
            except Exception as e:  # noqa: BLE001
                say(ok=False, cod="eroare", eroare=str(e)[:300])
            return
        src, out = Path(a.src).resolve(), Path(a.out).resolve()
        err = None
        for i in range(TRANSIENT_TRIES):
            try:
                secs = run_job(p, a.tool, src, out, a.model, a.rez, int(a.timeout_min * 60))
                say(ok=True, out=str(out), secunde=round(secs))
                return
            except Stop as e:
                if e.cod != "eroare":
                    say(ok=False, cod=e.cod, eroare=str(e))
                    return
                err = str(e)
            except Exception as e:  # noqa: BLE001 - network, page that did not load, a moved button
                err = f"{e.__class__.__name__}: {str(e)[:300]}"
            log(f"incercarea {i + 1} nu a mers ({err}); reincerc" if i + 1 < TRANSIENT_TRIES else f"nu a mers ({err})")
            time.sleep(20 * (i + 1))
        say(ok=False, cod="eroare", eroare=err)


if __name__ == "__main__":
    main()
