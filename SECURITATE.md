# Securitate — citește înainte să faci ceva public

## ⚠️ Pericolul principal

Repo-ul tău de lucru (cel privat, de acum) conține **date personale și chei
API reale**. Dacă acest repo ajunge PUBLIC, oricine de pe internet le vede.

Ce e sensibil în el chiar acum:

- **Chei API cu valoare**, scrise în `setari.json`:
  - `runpod_api_key`
  - `vast_api_key`
  - `gemini_api_key`
  (acestea sunt „parolele de bani" — cu ele se pot face cheltuieli pe contul tău)
- **Datele tale de business**: `conturi_clipuri.json`, `cheltuieli.json`,
  `radar.json`, `capcut_finalizate.json`, dump-urile din `tiktok_bot/studio/runs/`.
- **Backup-urile** (`backup_*/`) — copii vechi cu aceleași date.

## Ce ai de făcut (în ordine)

1. **NU face public repo-ul de lucru.** Dacă e deja public → fă-l privat ACUM
   (GitHub → Settings → General → Danger Zone → Change visibility → Private).

2. **Regenerează cele 3 chei**, pentru că au stat în istoricul git și trebuie
   considerate compromise:
   - RunPod → Settings → API Keys → revoke + create new
   - Vast.ai → Account → API Key → reset
   - Google AI Studio (Gemini) → API Keys → delete + create new
   Apoi pune cheile noi în `setari.json` (sau din interfață, Setări).

3. **Folosește repo-ul PUBLIC doar pentru cod.** Îl construiești cu
   `python pregateste_public.py` — copiază doar codul, fără chei și fără date,
   și verifică singur că n-a scăpat niciun secret.

## Regula de aur

| Niciodată în repo-ul public | Mereu doar local |
|---|---|
| chei API, parole | `setari.json` (cu cheile tale) |
| conturi, clipuri, povestiri | `conturi_clipuri.json`, `clipuri/`, `povestiri/` |
| modele AI | `modele/` |
| backup-uri | `backup_*/` |

Fiecare utilizator își pune **propriile** chei pe calculatorul lui, în `setari.json`.
Ele nu pleacă niciodată de pe calculatorul lui.

## De ce o parolă fixă e de ajuns aici

Motorul ascultă doar pe `127.0.0.1` (calculatorul local). Un site public poate
cere motorului să lucreze doar dacă:
1. adresa site-ului e în lista `web_origini`, **și**
2. cererea vine cu parola corectă (`web_parola`).

Fără parolă (câmp gol), **niciun** site din afară nu poate comanda motorul —
aplicația merge exact ca înainte, doar local. Vezi `GHID_LANSARE.md`.
