# Stamps Classifier - zalozenia, stan i plan

## 1. Aktualny stan projektu

### 1.1 Cel
- Lokalna aplikacja CLI do przygotowania opisow sprzedazy znaczkow pocztowych (glownie PRL) na podstawie zdjec.
- Brak UI web/mobile.
- Wejscie i wyjscie tylko przez katalogi oraz pliki.

### 1.2 Aktualny workflow
1. Uzytkownik wrzuca zdjecia do `input/`.
2. `process_stamps.py` wykrywa obrazy i konwertuje HEIC do JPG.
3. Dla kazdego obrazu:
- tryb domyslny: model Vision wykrywa wiele znaczkow (bbox), a kazdy region jest cropowany.
- fallback: brak detekcji -> analizowane jest cale zdjecie jako jeden znaczek.
4. AI Vision analizuje kazdy znaczek (crop lub cale zdjecie) albo bierze wynik z cache po hashu.
5. Program zmienia nazwy na opisowe dla pojedynczego znaczka lub tworzy opisowe nazwy dla cropow.
6. Program tworzy katalog wyjsciowy per znaczek:
- `output/<id_znaczka>/opis.txt`
- `output/<id_znaczka>/<zdjecie>.jpg|png`
7. Program tworzy zbiorczy `output/listings.csv`.
8. Program zapisuje:
- `cache/hash.json` - cache odpowiedzi modelu
- `cache/processed_index.json` - indeks przetworzonych plikow i statusow
- `cache/detection.json` - cache wykrytych bbox dla zdjec zrodlowych
9. Program generuje statyczna strone katalogu:
- `output/index.html` (tabela: zdjecie, opis i pozostale kolumny)

### 1.3 Aktualna struktura projektu
- `process_stamps.py` - glowny pipeline.
- `input/` - zdjecia wejsciowe.
- `input/original_heic/` - oryginalne pliki HEIC po konwersji.
- `output/listings.csv` - zbiorczy CSV.
- `output/index.html` - statyczny katalog do publikacji.
- `output/<folder_znaczka>/opis.txt` - opis pojedynczego znaczka.
- `output/<folder_znaczka>/<zdjecie>` - kopia zdjecia powiazana z opisem.
- `cache/hash.json` - cache AI po hashu obrazu.
- `cache/processed_index.json` - statusy `done/review/failed`.
- `cache/detection.json` - cache detekcji bbox.
- `cache/crops/` - tymczasowe cropy znaczkow.
- `docs/PROJEKT_PLAN.md` - ten dokument.

### 1.4 Zrealizowane punkty roadmapy
- Pelna polszczyzna wyjscia (`opis.txt`, naglowki i wartosci w `listings.csv`).
- Opcjonalne pomijanie juz sklasyfikowanych zdjec:
- domyslnie skip dla `done`
- flagi `--force`, `--retry-failed`, `--recheck-review`, `--no-skip-processed`
- Obsluga wielu znaczkow na jednym zdjeciu:
- detekcja bbox + cropowanie
- osobny rekord output i CSV dla kazdego wykrytego znaczka
- metadane: zrodlo obrazu, indeks znaczka, bbox
- flaga `--single-stamp-only` do wymuszenia starego trybu 1 zdjecie = 1 znaczek

## 2. Jak uzywac

### 2.1 Wymagania
- Python 3.11+
- Zainstalowane biblioteki:
```bash
python3 -m pip install --upgrade "openai>=1.0.0" pillow pillow-heif
```
- Ustawiony klucz API:
```bash
cd /Users/bartoszfink/dzikieProjekty/stamps-classifier
set -a; source .env; set +a
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

# Wylacz detekcje wielu znaczkow i analizuj cale zdjecie jako jeden znaczek
python3 process_stamps.py --single-stamp-only
```

### 2.4 Wyniki
- `output/listings.csv` - zbiorczy plik do dalszej pracy/importu.
- `output/index.html` - gotowa statyczna strona z tabela wynikow.
- `output/<id_znaczka>/opis.txt` - opis pojedynczego znaczka.
- `output/<id_znaczka>/<zdjecie>` - kopia powiazanego obrazu.

Nowe kolumny techniczne w `listings.csv`:
- `zrodlo_obrazu`
- `indeks_znaczka`
- `bbox`

## 3. Kolejne planowane punkty

### 3.1 Sugestia ceny (na razie poza zakresem)
Sugerowana implementacja:
1. Dodac etap `price_suggestion` po analizie AI.
2. Budowac zapytanie po: kraj, era, rok, temat/seria, typ, stan.
3. Pobierac ceny z wybranego zrodla (do ustalenia) i liczyc mediane po odrzuceniu skrajnych wartosci.
4. Dodac pola:
- `cena_sugerowana_pln`
- `zakres_ceny_pln`
- `zrodlo_ceny`
- `pewnosc_ceny`
5. Dodac fallback i flage `wymaga_recznej_wyceny`.
