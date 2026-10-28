#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Construieste un folder CURAT, doar cu cod, gata de pus intr-un repo PUBLIC
de pe care toti utilizatorii isi iau actualizarile.

NU copiaza NIMIC personal: fara chei API, fara conturi, fara clipuri,
povestiri, modele, backup-uri sau node_modules. La final verifica singur
ca nu a scapat niciun secret si refuza daca gaseste ceva.

Folosire:
    python pregateste_public.py                 (scrie in ../tiktok-studioz-public)
    python pregateste_public.py  CALE_DESTINATIE

Dupa ce ruleaza, iti arata exact comenzile git de urcat pe GitHub.
"""
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEST = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else (HERE.parent / "tiktok-studioz-public")

# --- Ce COD publicam (dupa extensie), oriunde in proiect ---------------
EXT_COD = {".py", ".html", ".css", ".js", ".md", ".bat", ".ps1", ".cfg", ".ini", ".toml", ".yaml", ".yml"}

# --- Fisiere exacte permise la radacina (chiar daca nu au extensie de cod)
FISIERE_PERMISE = {"requirements.txt", ".gitignore", ".gitattributes",
                   "actualizare.exemplu.json", "modele_descarcare.exemplu.json"}

# --- Subfolderele REALE ale aplicatiei: DOAR in acestea intram ---------
#  (restul din folderul utilizatorului - venv-uri, cercetare, build-uri - se ignora)
ALLOWED_DIRS = {"fonturi", "tiktok_bot", "drummer_animation"}

# --- Componente de cale "gunoi": daca apar oriunde in cale, excludem ----
FOLDERE_INTERZISE = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "site-packages", "Lib", "lib", "Scripts", "bin", "include", "share", "etc",
    "build", "dist", "payload",
    "modele", "clipuri", "povestiri", "jurnal_voce", "dublate",
    "capcut_de_tradus", "capcut_traduse", "capcut_final",
    "runs", "dump", "studio",         # date din tiktok_bot
}

# --- Fisiere de date / personale interzise explicit (chiar daca sunt .json/.txt)
FISIERE_INTERZISE = {
    "setari.json", "setari_1.json", "setari_backup_inainte_de_runpod.json",
    "conturi_clipuri.json", "cheltuieli.json", "radar.json",
    "capcut_finalizate.json", "viteza_voce.json", "gpu_nvenc.json",
    "agent_raspuns_incomplet.txt",
}

# --- Extensii mereu interzise (date/media/modele) ----------------------
EXT_INTERZISE = {
    ".json", ".jsonl", ".txt",       # date (requirements.txt e permis explicit mai sus)
    ".mp4", ".mov", ".mkv", ".avi", ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg",
    ".pt", ".pth", ".ckpt", ".safetensors", ".onnx", ".bin", ".engine", ".trt", ".plan",
    ".zip", ".7z", ".rar", ".apk", ".apks", ".xapk", ".aab", ".obb",
    ".exe", ".dll", ".lib", ".png", ".jpg", ".jpeg", ".webp", ".gif",
}

# fonturile sunt necesare la randare si nu-s secrete
EXT_FONT = {".ttf", ".otf", ".woff", ".woff2"}


def _componenta_gunoi(p: str) -> bool:
    """O componenta de folder care nu are ce cauta in cod: venv, pachete, foldere personale."""
    if p in FOLDERE_INTERZISE:
        return True
    if p.startswith(".") or p.startswith("_") or p.startswith("backup_"):
        return True
    if p.lower().startswith("venv"):
        return True
    return False


def interzis_cale(rel: Path) -> bool:
    # vreun folder din cale e "gunoi"?
    if any(_componenta_gunoi(p) for p in rel.parts[:-1]):
        return True
    if rel.name in FISIERE_INTERZISE:
        return True
    return False


def _fisier_permis(rel: Path) -> bool:
    if rel.name in FISIERE_PERMISE:
        return True
    ext = rel.suffix.lower()
    if ext in EXT_FONT:
        return True
    if ext in EXT_INTERZISE:
        return False
    return ext in EXT_COD


def permis(rel: Path) -> bool:
    if interzis_cale(rel):
        return False
    parts = rel.parts
    if len(parts) == 1:
        return _fisier_permis(rel)          # fisier la radacina aplicatiei
    if parts[0] not in ALLOWED_DIRS:
        return False                        # intram DOAR in folderele reale ale aplicatiei
    return _fisier_permis(rel)


GITIGNORE_PUBLIC = """# Repo PUBLIC - aici NU intra niciodata date personale.
# Fisierele de mai jos raman doar pe calculatorul fiecarui utilizator.

# setari si chei (fiecare le pune pe calculatorul lui)
setari*.json
actualizare.json
modele_descarcare.json
.versiune_anterioara

# datele si munca utilizatorului
conturi*.json
cheltuieli.json
radar.json
capcut_finalizate.json
viteza_voce.json
gpu_nvenc.json
povestiri/
clipuri/
capcut_de_tradus/
capcut_traduse/
capcut_final/
jurnal_voce/
backup_*/

# modele AI si binare mari
modele/
*.pt
*.pth
*.ckpt
*.safetensors
*.onnx
*.bin
*.engine
*.trt
*.plan

# media
*.mp4
*.mov
*.mkv
*.avi
*.mp3
*.wav
*.flac
*.m4a
*.aac
*.ogg

# arhive / aplicatii
*.zip
*.7z
*.rar
*.apk
*.apks
*.xapk
*.aab
*.obb

# medii Python / cache
.venv/
venv/
__pycache__/
*.pyc

# node
node_modules/

# dumpuri din aplicatia de postare
tiktok_bot/studio/runs/
tiktok_bot/**/dump/
*.jsonl
"""

# tipare de secrete (ca sa refuzam daca scapa ceva). \b = granita de cuvant,
# ca "ecnshf_123" (nume de clip) sa NU fie confundat cu un token "hf_...".
SECRETE = re.compile(
    r"(\bsk-[A-Za-z0-9]{20,}|\bhf_[A-Za-z0-9]{30,}|\bAIza[0-9A-Za-z_\-]{30,}|"
    r"\bghp_[A-Za-z0-9]{30,}|\bxox[baprs]-[A-Za-z0-9\-]{20,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
CHEI_SENSIBILE = re.compile(r'"(?:[a-z_]*api[_-]?key|[a-z_]*token|[a-z_]*secret|web_parola)"\s*:\s*"([^"]{8,})"', re.I)


def _valoare_falsa(val: str) -> bool:
    """True daca 'valoarea' nu e de fapt un secret (placeholder sau nume de cheie din cod JS)."""
    v = val.strip()
    if v.lower() in ("", "null", "none", "false", "true", "schimba-ma", "parola", "cheie", "cheie salvată"):
        return True
    # e un NUME de cheie (ex. "runpod_api_key" scris in cod JS), nu o valoare reala
    if re.fullmatch(r"[a-z][a-z0-9_]*", v) and re.search(r"(key|token|secret|api|parola|origini)", v):
        return True
    # placeholder din documentatie, scris cu MAJUSCULE (ex. ALEGE-O-PAROLA, PAROLA_TA)
    if re.fullmatch(r"[A-Z][A-Z0-9_\-]{2,}", v):
        return True
    # bucati de cod / sabloane din documentatie
    if any(s in v for s in ("${", "}", "S.", "S[", "<", ">", " ", "github.io", "numele-tau", "exemplu", "example")):
        return True
    return False


def copiaza():
    # Build-ul e un folder GENERAT: daca exista deja, il stergem complet si-l refacem curat,
    # ca sa nu ramana fisiere vechi (ex. gunoi dintr-o rulare anterioara). Doar cu grija sa
    # nu stergem din greseala folderul aplicatiei sau un parinte al lui.
    h = HERE.resolve()
    d = DEST.resolve()
    if d == h or h == d or str(h).startswith(str(d) + os.sep):
        print(f"✖  Destinatie nesigura ({DEST}). Alege alt folder.")
        return False
    if DEST.exists():
        print(f"🧹 Sterg build-ul vechi si refac curat: {DEST}")
        shutil.rmtree(DEST, ignore_errors=True)

    n = 0
    for root, dirs, files in os.walk(HERE):
        rel_root = Path(root).relative_to(HERE)
        la_radacina = (str(rel_root) == ".")
        # NU intram in foldere gunoi; la radacina intram DOAR in folderele aplicatiei
        pastram = []
        for name in dirs:
            if _componenta_gunoi(name):
                continue
            if la_radacina and name not in ALLOWED_DIRS:
                continue
            pastram.append(name)
        dirs[:] = pastram
        for f in files:
            rel = Path(f) if la_radacina else (rel_root / f)
            if not permis(rel):
                continue
            dest = DEST / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(root) / f, dest)
            n += 1
    # .gitignore puternic in repo-ul public
    (DEST / ".gitignore").write_text(GITIGNORE_PUBLIC, encoding="utf-8")
    print(f"✓  Am copiat {n} fisiere de cod in: {DEST}")
    return True


def verifica_secrete():
    gasite = []
    for p in DEST.rglob("*"):
        if not p.is_file():
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        for m in SECRETE.finditer(txt):
            gasite.append((p.relative_to(DEST), "secret: " + m.group(0)[:12] + "..."))
        for m in CHEI_SENSIBILE.finditer(txt):
            if not _valoare_falsa(m.group(1)):
                gasite.append((p.relative_to(DEST), "cheie cu valoare: " + m.group(0)[:40]))
    return gasite


def main():
    print("=" * 56)
    print("  Pregatesc repo-ul PUBLIC (doar cod, fara date personale)")
    print("=" * 56)
    if not copiaza():
        return 1
    print("🔎 Verific sa nu fi scapat vreun secret...")
    gasite = verifica_secrete()
    if gasite:
        print("\n✖  ATENTIE: am gasit lucruri sensibile in folderul curat. NU-l urca asa!")
        for rel, ce in gasite[:30]:
            print(f"   - {rel}: {ce}")
        print("\n   Sterge-le din cod (foloseste setari.json local, nu valori scrise in cod),")
        print("   apoi ruleaza din nou.")
        return 2
    print("✓  Curat: niciun secret gasit.")
    print("\nUrmatorii pasi, ca sa-l pui pe GitHub (repo NOU, public):")
    print(f"   cd \"{DEST}\"")
    print("   git init")
    print("   git add -A")
    print('   git commit -m "Cod public TikTok Studioz"')
    print("   git branch -M main")
    print("   git remote add origin https://github.com/UTILIZATORUL/tiktok-studioz-public.git")
    print("   git push -u origin main")
    print("\nApoi utilizatorii iau aplicatia o data cu:")
    print("   git clone https://github.com/UTILIZATORUL/tiktok-studioz-public.git")
    print("   si pornesc 'Porneste aplicatia.bat' (restul se actualizeaza singur).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
