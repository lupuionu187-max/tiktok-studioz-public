# Ghid de lansare — aplicația pe un site + actualizări automate

Scopul: oricine intră pe o adresă vede interfața, iar aplicația rulează pe
calculatorul fiecăruia (resursele lui). Când tu schimbi ceva și dai push,
toți primesc actualizarea automat.

Totul e deja pregătit în cod. Mai jos sunt pașii pe care îi faci **tu**, o
singură dată. Durează ~30 de minute.

---

## 0. Întâi securitatea (obligatoriu) — vezi `SECURITATE.md`

- Dacă repo-ul de lucru e public, **fă-l privat acum**.
- **Regenerează** cheile `runpod_api_key`, `vast_api_key`, `gemini_api_key`
  (au fost în istoricul git → sunt compromise). Pune cheile noi în Setări.

---

## 1. Construiește repo-ul PUBLIC (doar cod, fără datele tale)

În folderul aplicației:

```
python pregateste_public.py
```

Face un folder curat `../tiktok-studioz-public` cu **doar codul** și verifică
singur că nu a scăpat niciun secret. Dacă găsește ceva, se oprește și îți spune.

Apoi creează pe GitHub un **repo nou, PUBLIC**, numit de ex. `tiktok-studioz-public`,
și urcă folderul (comenzile exacte ți le afișează scriptul la final):

```
cd ../tiktok-studioz-public
git init
git add -A
git commit -m "Cod public TikTok Studioz"
git branch -M main
git remote add origin https://github.com/UTILIZATORUL/tiktok-studioz-public.git
git push -u origin main
```

> Înlocuiește `UTILIZATORUL` cu numele tău de GitHub.

---

## 2. De aici înainte, lucrezi în repo-ul public

Ca să nu mai ai două copii de întreținut:

1. Clonează repo-ul public într-un folder nou de lucru:
   ```
   git clone https://github.com/UTILIZATORUL/tiktok-studioz-public.git lucru
   ```
2. Copiază în el **datele tale** (rămân doar local, sunt ignorate de git):
   `setari.json`, `conturi_clipuri.json`, `povestiri/`, `clipuri/`, `modele/` etc.
3. De acum, când schimbi cod: `git add -A && git commit -m "..."` și `git push`.
   **Toți utilizatorii primesc schimbarea la următoarea pornire.**

Datele tale nu ajung pe GitHub niciodată (le ignoră `.gitignore`-ul din repo-ul public).

---

## 3. Pornește site-ul (GitHub Pages)

Pe repo-ul public: **Settings → Pages → Build and deployment → Source: Deploy
from a branch → Branch: `main` / `/ (root)` → Save.**

După 1-2 minute, adresa ta e:
```
https://UTILIZATORUL.github.io/tiktok-studioz-public/
```
- adresa de bază = pagina de prezentare (`index.html`)
- `.../ui.html` = interfața aplicației

Aceasta e adresa pe care o dai oamenilor. Când schimbi `ui.html` și dai push,
interfața de pe site se actualizează singură.

---

## 4. Pune parola (ca site-ul să poată comanda motorul local)

Pe fiecare calculator, în `setari.json`, adaugă două câmpuri:

- `web_parola` = o parolă secretă (aceeași pe care o va introduce utilizatorul în bară)
- `web_origini` = adresa site-ului tău, de ex. `https://UTILIZATORUL.github.io`

Exemplu de adăugat în `setari.json`:
```
"web_parola": "ALEGE-O-PAROLA",
"web_origini": "https://UTILIZATORUL.github.io"
```

Fără aceste câmpuri (goale), aplicația merge **doar local**, exact ca înainte —
niciun site din afară nu o poate comanda. Asta e protecția ta.

> Alternativ, în loc de `setari.json`, poți folosi variabilele de mediu
> `WEB_PAROLA` și `WEB_ORIGINI`.

---

## 5. Modelele AI (fișierele mari)

Modelele din `modele/` sunt prea mari pentru GitHub, deci nu intră în repo.
Două variante:

- **Simplu:** fiecare utilizator își copiază folderul `modele/` manual (i-l dai
  pe WeTransfer / Drive / stick).
- **Automat:** pune fișierele mari pe GitHub Releases / Drive și completează
  `modele_descarcare.json` (vezi exemplul de mai jos). Lansatorul le descarcă
  o singură dată, la prima pornire.

`modele_descarcare.json` (opțional, în folderul aplicației):
```json
{
  "modele/numele_modelului.pt": "https://link-direct-de-descarcare",
  "modele/alt_model.onnx": "https://alt-link"
}
```

---

## 6. Cum folosește un utilizator nou aplicația

1. Instalează Git + Python.
2. `git clone https://github.com/UTILIZATORUL/tiktok-studioz-public.git`
3. Pornește `Porneste aplicatia.bat` (își pune singur ce lipsește și se actualizează).
4. Pune-și **cheile lui** (RunPod/Vast/Gemini) și parola în Setări.
5. Deschide adresa site-ului → apasă „Deschide aplicația" → introduce parola o dată.

Gata: interfața e de pe site, dar munca se face pe calculatorul lui, cu GPU-ul lui.

---

## Ce am pregătit în cod (ca să știi)

| Fișier | Ce face |
|---|---|
| `requirements.txt` | lista de pachete Python |
| `launcher.py` | pornirea cu auto-update (git pull), protejează datele, revine la versiunea veche dacă pică |
| `Porneste aplicatia.bat` | pornire cu un clic (Windows) |
| `dublare_ui.py` | +CORS +parolă (aditiv: gol = merge ca înainte) |
| `ui.html` | se conectează la `localhost` când e deschis de pe site |
| `pregateste_public.py` | construiește repo-ul public curat + verifică secrete |
| `index.html` | pagina de site (landing + intrare în UI) |
| `SECURITATE.md` | ce e sensibil și ce ai de făcut |

## Atenție / limite cunoscute

- Auto-update-ul (`git reset --hard`) aduce exact codul de pe GitHub. Dacă un
  utilizator modifică local fișiere de **cod**, acele modificări se pierd la
  update (datele lui NU se pierd). E normal pentru „toți pe aceeași versiune".
- Funcțiile de voce/upscale cer placă NVIDIA + `torch` (instalare separată,
  lansatorul arată comanda).
- Partea „UI de pe site → motor local" (CORS/parolă) trebuie testată o dată pe
  calculatorul tău cu Windows; local (fără site) a rămas neschimbată.
- Aplicația de postare (`tiktok_bot/`) e mai mare și poate avea nevoie de pași
  separați; acest ghid acoperă aplicația de dublare.
