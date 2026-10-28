#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TikTok Studioz - Lansator cu actualizare automata.

Ce face, la fiecare pornire:
  1. verifica pe GitHub daca ai o versiune noua a codului;
  2. daca da, o trage automat (git) - FARA sa atinga datele tale
     (setari.json, conturi, clipuri, povestiri, modele - toate raman);
  3. porneste motorul local (dublare_ui.py) si deschide pagina;
  4. daca pornirea pica imediat dupa un update, revine la versiunea
     anterioara (care mergea) ca sa nu ramai blocat.

Datele tale NU ajung niciodata pe GitHub: ele sunt "ignorate" de git,
iar actualizarea inlocuieste doar fisierele de cod.

Configurare optionala in "actualizare.json" (langa acest fisier):
  {
    "repo":   "https://github.com/UTILIZATORUL/tiktok-studioz-public.git",
    "ramura": "main",
    "auto":   true,          // false = intreaba inainte de update
    "venv":   true           // false = foloseste Python-ul de sistem, fara mediu separat
  }
Daca lipseste, se foloseste "origin" (de unde a fost clonat) si ramura curenta.
"""
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG_FILE = HERE / "actualizare.json"
VERSIUNE_ANT = HERE / ".versiune_anterioara"   # commit-ul de dinainte de ultimul update (pentru revenire)
MODELE_MANIFEST = HERE / "modele_descarcare.json"  # optional: {"modele/x.pt": "https://..."}


# ---------------------------------------------------------------- utilitare
def spune(msg):
    print(msg, flush=True)


def ruleaza(args, **kw):
    """Ruleaza o comanda si intoarce (cod, iesire). Nu arunca exceptii."""
    try:
        r = subprocess.run(args, cwd=str(HERE), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", **kw)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, f"Comanda lipseste: {args[0]}"
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def citeste_cfg():
    cfg = {"repo": "", "ramura": "", "auto": True, "venv": True}
    try:
        cfg.update(json.loads(CFG_FILE.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 - fara fisier sau stricat: ramanem pe valorile implicite
        pass
    return cfg


def are_git():
    cod, _ = ruleaza(["git", "--version"])
    return cod == 0


def e_repo_git():
    return (HERE / ".git").exists()


def ramura_curenta():
    cod, out = ruleaza(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    return out.strip() if cod == 0 else "main"


def commit_curent():
    cod, out = ruleaza(["git", "rev-parse", "HEAD"])
    return out.strip() if cod == 0 else ""


# ---------------------------------------------------------------- actualizare
def actualizeaza(cfg):
    """Trage ultima versiune de cod. Intoarce True daca s-a schimbat ceva."""
    if not e_repo_git():
        spune("ℹ  Nu e o copie Git (lipseste .git) - sar peste actualizare.")
        spune("   Ca sa primesti actualizari automate, descarca aplicatia prin 'git clone'.")
        return False
    if not are_git():
        spune("⚠  Git nu e instalat - nu pot verifica actualizari. Instaleaza de la https://git-scm.com/")
        return False

    ramura = cfg.get("ramura") or ramura_curenta()
    remote = "origin"

    # daca in config e dat alt repo, il setam ca 'origin'
    if cfg.get("repo"):
        ruleaza(["git", "remote", "set-url", "origin", cfg["repo"]])

    spune("🔎 Caut actualizari pe GitHub...")
    cod, out = ruleaza(["git", "fetch", remote, ramura])
    if cod != 0:
        spune("⚠  Nu am putut ajunge la GitHub (esti conectat la internet?). Pornesc versiunea locala.")
        return False

    local = commit_curent()
    cod, tinta = ruleaza(["git", "rev-parse", f"{remote}/{ramura}"])
    tinta = tinta.strip()
    if cod != 0 or not tinta:
        spune("⚠  Nu gasesc ramura pe GitHub. Pornesc versiunea locala.")
        return False
    if local == tinta:
        spune("✓  Ai deja ultima versiune.")
        return False

    if not cfg.get("auto", True):
        rasp = input("Exista o versiune noua. O instalez acum? [D/n] ").strip().lower()
        if rasp in ("n", "nu", "no"):
            spune("→ Am lasat versiunea curenta.")
            return False

    # retinem commit-ul actual ca sa putem reveni daca noul pica
    if local:
        try:
            VERSIUNE_ANT.write_text(local, encoding="utf-8")
        except OSError:
            pass

    spune("⬇  Instalez versiunea noua...")
    # reset --hard aduce exact codul de pe GitHub; fisierele tale "ignorate" (setari, clipuri,
    # modele, povestiri) sunt NEURMARITE de git, deci NU se sterg si NU se modifica.
    cod, out = ruleaza(["git", "reset", "--hard", f"{remote}/{ramura}"])
    if cod != 0:
        spune("⚠  Actualizarea a esuat:\n" + out)
        return False
    spune("✓  Actualizat la ultima versiune.")
    return True


def revino_la_versiunea_veche():
    """Daca pornirea a picat dupa update, ne intoarcem la commit-ul salvat."""
    try:
        vechi = VERSIUNE_ANT.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not vechi:
        return False
    spune("↩  Versiunea noua n-a pornit - revin la cea anterioara...")
    cod, _ = ruleaza(["git", "reset", "--hard", vechi])
    return cod == 0


# ---------------------------------------------------------------- mediu Python
def python_pt_rulat(cfg):
    """Intoarce calea catre Python-ul cu care pornim aplicatia (venv sau sistem)."""
    if not cfg.get("venv", True):
        return sys.executable
    venv = HERE / ".venv"
    py = venv / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    if not py.exists():
        spune("🧰 Pregatesc mediul Python (o singura data, poate dura)...")
        cod, out = ruleaza([sys.executable, "-m", "venv", str(venv)])
        if cod != 0:
            spune("⚠  Nu am putut crea mediul venv, folosesc Python-ul de sistem.\n" + out)
            return sys.executable
    # instalam/actualizam pachetele de baza
    req = HERE / "requirements.txt"
    if req.exists():
        spune("📦 Verific pachetele necesare...")
        ruleaza([str(py), "-m", "pip", "install", "--upgrade", "pip", "--quiet"])
        cod, out = ruleaza([str(py), "-m", "pip", "install", "-r", str(req), "--quiet"])
        if cod != 0:
            spune("⚠  Unele pachete nu s-au instalat complet (poate torch/GPU - vezi requirements.txt):\n"
                  + out[-800:])
    # avertizare pentru torch (GPU) - nu se instaleaza automat
    cod, _ = ruleaza([str(py), "-c", "import torch"])
    if cod != 0:
        spune("⚠  'torch' (pentru GPU/voce) nu e instalat. Functiile de voce/upscale nu vor merge pana nu-l pui.")
        spune("   Instaleaza-l o data, potrivit placii tale video, de la: https://pytorch.org/get-started/locally/")
    return py


# ---------------------------------------------------------------- modele mari
def descarca_modele():
    """Descarca modelele mari daca lipsesc, dupa un manifest optional (o singura data)."""
    if not MODELE_MANIFEST.exists():
        return
    try:
        man = json.loads(MODELE_MANIFEST.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    import urllib.request
    for rel, url in man.items():
        dest = HERE / rel
        if dest.exists() and dest.stat().st_size > 0:
            continue
        if not url:
            continue
        spune(f"⬇  Descarc modelul {rel} (o singura data)...")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            op = urllib.request.build_opener()
            with op.open(url) as r, open(dest, "wb") as f:
                while True:
                    buf = r.read(1 << 20)
                    if not buf:
                        break
                    f.write(buf)
            spune(f"✓  {rel} descarcat.")
        except Exception as e:  # noqa: BLE001
            spune(f"⚠  Nu am putut descarca {rel}: {e}")


# ---------------------------------------------------------------- pornire motor
def porneste_motorul(py):
    motor = HERE / "dublare_ui.py"
    if not motor.exists():
        spune("✖  Nu gasesc dublare_ui.py - aplicatia pare incompleta.")
        return 1
    spune("🚀 Pornesc aplicatia...")
    t0 = time.time()
    try:
        proc = subprocess.Popen([str(py), str(motor)], cwd=str(HERE))
    except Exception as e:  # noqa: BLE001
        spune(f"✖  Nu am putut porni: {e}")
        return 1
    # asteptam: daca moare in primele ~12s dupa un update, consideram ca e stricat
    try:
        proc.wait(timeout=12)
        # a iesit repede
        if proc.returncode not in (0, None) and time.time() - t0 < 12:
            return proc.returncode
        return proc.returncode or 0
    except subprocess.TimeoutExpired:
        # a trecut de pragul critic - ruleaza normal; asteptam pana se inchide
        proc.wait()
        return proc.returncode or 0


# ---------------------------------------------------------------- principal
def main():
    spune("=" * 52)
    spune("   TikTok Studioz - pornire")
    spune("=" * 52)
    cfg = citeste_cfg()

    fac_update = "--fara-update" not in sys.argv
    s_a_schimbat = actualizeaza(cfg) if fac_update else False

    descarca_modele()
    py = python_pt_rulat(cfg)

    cod = porneste_motorul(py)

    # daca tocmai am actualizat si motorul a picat imediat, revenim si reincercam o data
    if cod not in (0, None) and s_a_schimbat and revino_la_versiunea_veche():
        py = python_pt_rulat(cfg)
        cod = porneste_motorul(py)

    if cod not in (0, None):
        spune("")
        spune("✖  Aplicatia s-a inchis cu o eroare. Citeste mesajele de mai sus.")
        try:
            input("Apasa Enter ca sa inchizi...")
        except EOFError:
            pass
    return cod or 0


if __name__ == "__main__":
    sys.exit(main())
