# Stamps Classifier - zalozenia, stan i plan

## 1. Aktualny stan projektu

### 1.1 Cel
- Lokalna aplikacja CLI do przygotowania opisow sprzedazy znaczkow pocztowych (glownie PRL) na podstawie zdjec.
- Brak UI web/mobile.
- Wejscie i wyjscie tylko przez katalogi oraz pliki.

### 1.2 Aktualny workflow
1. Uzytkownik wrzuca zdjecia do `input/`.
2. `process_stamps.py` wykrywa obrazy i konwertuje HEIC do JPG.
3. AI Vision analizuje zdjecia (albo bierze wynik z cache po hashu).
4. Program pobiera ceny porownawcze z internetu (Allegro API) i liczy sugestie ceny.
5. Program zmienia nazwy plikow na opisowe.
6. Program tworzy katalog wyjsciowy per znaczek:
- `output/<id_znaczka>/opis.txt`
- `output/<id_znaczka>/<zdjecie>.jpg|png`
7. Program tworzy zbiorczy `output/listings.csv`.
8. Program zapisuje:
- `cache/hash.json` - cache odpowiedzi modelu
- `cache/processed_index.json` - indeks przetworzonych plikow i statusow
- `cache/pricing.json` - cache sugestii cen i zapytan

### 1.3 Aktualna struktura projektu
- `process_stamps.py` - glowny pipeline.
- `input/` - zdjecia wejsciowe.
- `input/original_heic/` - oryginalne pliki HEIC po konwersji.
- `output/listings.csv` - zbiorczy CSV.
- `output/<folder_znaczka>/opis.txt` - opis pojedynczego znaczka.
- `output/<folder_znaczka>/<zdjecie>` - kopia zdjecia powiazana z opisem.
- `cache/hash.json` - cache AI po hashu obrazu.
- `cache/processed_index.json` - statusy `done/review/failed`.
- `docs/PROJEKT_PLAN.md` - ten dokument.

### 1.4 Zrealizowane punkty roadmapy
- Pelna polszczyzna wyjscia (`opis.txt`, naglowki i wartosci w `listings.csv`).
- Opcjonalne pomijanie juz sklasyfikowanych zdjec:
- domyslnie skip dla `done`
- flagi `--force`, `--retry-failed`, `--recheck-review`, `--no-skip-processed`
- Sugestia ceny z internetu (Allegro API) + cache cen:
- pola: `cena_sugerowana_pln`, `zakres_ceny_pln`, `zrodlo_ceny`, `pewnosc_ceny`, `wymaga_recznej_wyceny`
- flaga `--pricing-force-refresh`
- flaga `--no-online-pricing`

## 2. Jak uzywac

### 2.1 Wymagania
- Python 3.11+
- Zainstalowane biblioteki:
```bash
python3 -m pip install --upgrade "openai>=1.0.0" pillow pillow-heif requests
```
- Ustawiony klucz API:
```bash
cd /Users/bartoszfink/dzikieProjekty/stamps-classifier
set -a; source .env; set +a
```
- Dla cen internetowych z Allegro ustaw dodatkowo:
```bash
export ALLEGRO_CLIENT_ID="twoj_client_id"
export ALLEGRO_CLIENT_SECRET="twoj_client_secret"
```
- Uwaga: endpoint `/offers/listing` moze wymagac odpowiednich uprawnien aplikacji Allegro.
- Opcjonalnie:
```bash
export ALLEGRO_API_BASE="https://api.allegro.pl"
export ALLEGRO_TOKEN_URL="https://allegro.pl/auth/oauth/token"
export ALLEGRO_LISTING_LIMIT="20"
```

### 2.2 Standardowe uruchomienie
```bash
cd /Users/bartoszfink/dzikieProjekty/stamps-classifier
python3 process_stamps.py
```

### 2.3 Tryby ponownego przetwarzania
```bash
# Pelne ponowne przetworzenie wszystkiego
python3 process_stamps.py --force

# Ponow tylko rekordy oznaczone jako failed
python3 process_stamps.py --retry-failed

# Ponow tylko rekordy oznaczone jako review
python3 process_stamps.py --recheck-review

# Nie pomijaj done (wymus reprocessing done)
python3 process_stamps.py --no-skip-processed

# Odswiez ceny internetowe (bez uzycia cache cen)
python3 process_stamps.py --pricing-force-refresh

# Wylacz etap cen internetowych
python3 process_stamps.py --no-online-pricing
```

### 2.4 Wyniki
- `output/listings.csv` - zbiorczy plik do dalszej pracy/importu.
- `output/<id_znaczka>/opis.txt` - opis pojedynczego znaczka.
- `output/<id_znaczka>/<zdjecie>` - kopia powiazanego obrazu.
- `cache/pricing.json` - cache cen i zapytan.

Kolumny cenowe w `listings.csv`:
- `cena_sugerowana_pln`
- `zakres_ceny_pln`
- `zrodlo_ceny`
- `pewnosc_ceny`
- `wymaga_recznej_wyceny`

## 3. Kolejne planowane punkty

### 3.1 Wiele znaczkow na jednym zdjeciu (strona klasera)
Sugerowana implementacja:
1. Dodac detekcje regionow znaczkow (bbox + score).
2. Dla kazdego bbox zrobic crop i uruchomic obecna klasyfikacje.
3. Dodac metadane:
- `source_image`
- `crop_index`
- `crop_bbox`
4. Zapisywac osobny rekord wyjsciowy dla kazdego wykrytego znaczka.
5. Dodac filtry jakosci (min rozmiar, min score, auto-review dla niepewnych).
