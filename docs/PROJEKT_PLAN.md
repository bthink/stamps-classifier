# Stamps Classifier - zalozenia i plan wdrozen

## 1. Zalozenia bazowe projektu (stan obecny)

### 1.1 Cel
- Lokalna aplikacja CLI do przygotowania opisow sprzedazy znaczkow pocztowych (glownie PRL) na podstawie zdjec.
- Brak UI web/mobile.
- Wejscie i wyjscie tylko przez katalogi oraz pliki.

### 1.2 Podstawowy workflow
1. Uzytkownik wrzuca zdjecia do `input/`.
2. `process_stamps.py` wykrywa obrazy i konwertuje HEIC do JPG.
3. AI Vision analizuje zdjecia.
4. Program zmienia nazwy plikow na opisowe.
5. Program tworzy zbiorczy `output/listings.txt`.
6. Program tworzy `output/listings.csv`.
7. Wyniki AI sa cache-owane w `cache/hash.json`.

### 1.3 Aktualna struktura projektu
- `process_stamps.py` - glowny pipeline.
- `input/` - zdjecia wejsciowe.
- `input/original_heic/` - oryginalne pliki HEIC po konwersji.
- `output/listings.txt` - zbiorczy plik opisow.
- `output/listings.csv` - tabela do dalszej obrobki/importu.
- `cache/hash.json` - cache odpowiedzi modelu po hashu obrazu.
- `docs/PROJEKT_PLAN.md` - ten dokument.

### 1.4 Zasady techniczne
- Python 3.11+.
- Biblioteki: `openai`, `pillow`, `pillow-heif`.
- Odpornosc na bledy pojedynczych plikow - brak crasha calego procesu.
- Wspierane formaty wejscia: JPG, JPEG, PNG, HEIC.

## 2. Planowane rozszerzenia i sugerowana implementacja

## 2.1 Pelna polszczyzna w plikach wyjsciowych

### Cel
- Wszystkie tresci i etykiety w wyjsciu (`listings.txt`, `listings.csv`) maja byc po polsku.
- Zero angielskich etykiet typu `content`, `description`, `type` w warstwie wyjsciowej dla uzytkownika.

### Sugerowana implementacja
1. Wydziel modul mapowania etykiet, np. `output_labels.py`.
2. Zdefiniuj stale etykiety po polsku:
- `TYTUL`
- `OPIS`
- `TAGI`
- `TYP`
- `STAN`
- `PEWNOSC`
- `WYMAGA_WERYFIKACJI`
3. W `listings.csv` zastosuj polskie naglowki:
- `nazwa_pliku`
- `tytul`
- `opis`
- `tagi`
- `typ`
- `stan`
- `pewnosc`
- `wymaga_weryfikacji`
4. Dodaj walidator jezykowy wyjscia:
- lista zakazanych tokenow angielskich (`type`, `condition`, `content`, `review`, `confidence`)
- jesli wykryte, log ostrzegawczy + automatyczna podmiana etykiety.
5. Doprecyzuj prompt modelu:
- "Return values in Polish for title and description."
- "Do not use English terms in title/description unless proper names."
6. Dodaj testy jednostkowe dla generatora TXT/CSV (snapshot testy).

### Zmiany w kodzie
- Refaktor `format_listing_entry`.
- Refaktor writera CSV i nazw kolumn.
- Nowy helper: `normalize_polish_output(data)`.

### Kryteria akceptacji
- W `output/listings.txt` brak angielskich etykiet.
- Naglowki CSV sa po polsku.
- Testy przechodza dla min. 3 przypadkow (pewny, niepewny, failed).

## 2.2 Sugestia ceny na podstawie Allegro

### Cel
- Dla kazdego rozpoznanego znaczka wygenerowac sugestie ceny sprzedazy w PLN.

### Sugerowana implementacja
1. Dodaj osobny etap `price_suggestion` po analizie AI i przed zapisem wynikow.
2. Zapytanie buduj z cech:
- kraj
- era
- rok
- temat/seria
- typ
- stan
3. Zbieraj dane referencyjne aukcji:
- tytul
- cena
- waluta
- data
- podobienstwo zapytania
4. Liczenie ceny sugerowanej:
- odrzucenie skrajnych wartosci (winsoryzacja 10-90 percentyl)
- mediana jako baza
- korekta za stan (`mint > used > cto > unknown`)
- korekta za pewnosc modelu (niska pewnosc obniza wage sugestii)
5. Dodaj pole wyjsciowe:
- `cena_sugerowana_pln`
- `zakres_ceny_pln` (np. "18-25")
- `zrodlo_ceny` (np. `allegro`, `fallback`)
- `pewnosc_ceny`
6. Gdy brak danych rynkowych:
- fallback do lokalnej tabeli minimalnej (`docs/` albo `config/pricing_rules.json`)
- jawna flaga `wymaga_recznej_wyceny=true`.
7. Caching cen:
- `cache/pricing.json` keyed by hash cech zapytania, TTL np. 7 dni.

### Zmiany w kodzie
- Nowy modul: `pricing.py`.
- Rozszerzenie struktury rekordu wynikowego o pola cenowe.
- Rozszerzenie `listings.csv` i `listings.txt` o sekcje ceny.

### Kryteria akceptacji
- Dla min. 80% rozpoznanych znaczkow jest wygenerowana cena lub sensowny fallback.
- Brak crasha pipeline przy bledzie pobierania danych cenowych.

## 2.3 Wiele znaczkow na jednym zdjeciu (strona klasera)

### Cel
- Obsluga zdjec calej strony klasera i automatyczny podzial na pojedyncze znaczki.

### Sugerowana implementacja
1. Dodaj etap detekcji regionow znaczkow:
- wejscie: cale zdjecie strony
- wyjscie: lista bounding boxow + score detekcji
2. Strategie detekcji:
- opcja A: Vision model zwraca boxy w JSON
- opcja B: lokalny preprocessing CV (kontury/prostokaty) + filtr proporcji
- rekomendacja: start od opcji A, potem fallback do B
3. Dla kazdego boxa:
- przytnij crop do pliku tymczasowego
- uruchom obecny pipeline klasyfikacji na cropie
- przypisz `source_image` i `stamp_index`
4. Nazewnictwo:
- oryginalny plik strony pozostaje bez zmian
- wygenerowane elementy maja nazwy:
`<source_stem>__s01.jpg`, `<source_stem>__s02.jpg`, itd.
5. Wynik agregacji:
- `listings.txt` i `listings.csv` zawieraja osobny rekord na kazdy wykryty znaczek
- dodatkowe kolumny:
`source_image`, `crop_index`, `crop_bbox`
6. Kontrola jakosci:
- minimalny rozmiar cropa (np. > 128 px)
- odrzucenie boxow z bardzo niska pewnoscia
- flaga `needs_manual_review=true` dla niepewnych detekcji

### Zmiany w kodzie
- Nowy modul: `detector.py`.
- Nowa faza pipeline przed `analyze_image`.
- Aktualizacja cache, by uwzglednial hash cropa, nie tylko calego zdjecia.

### Kryteria akceptacji
- Dla testowej strony klasera z 20 znaczkami system wykrywa wiekszosc elementow i nie przerywa pracy.
- Kazdy wykryty znaczek ma osobny rekord wyjsciowy.

## 2.4 Opcjonalne pomijanie juz sklasyfikowanych zdjec

### Cel
- Nie wykonywac ponownej analizy AI dla zdjec, ktore sa juz gotowe, aby oszczedzac tokeny.

### Sugerowana implementacja
1. Dodaj trwauly indeks przetworzonych plikow, np. `cache/processed_index.json`.
2. Klucz rekordu:
- `file_hash`
- `filename`
- `status` (`done`, `failed`, `review`)
- `processed_at`
- `output_record_id`
3. Tryb domyslny:
- `--skip-processed` wlaczone
- jesli hash istnieje i status `done`, pomin plik
4. Tryby CLI:
- `--force` - wymusza pelne ponowne przetworzenie
- `--retry-failed` - ponawia tylko `failed`
- `--recheck-review` - ponawia tylko rekordy `review`
5. Integracja z obecnym cache:
- najpierw sprawdz `processed_index`
- potem `hash.json`
- dopiero na koncu wywolaj model
6. Czytelny log:
- `Skipping IMG_0001.jpg (already processed)`
- `Reprocessing IMG_0002.jpg (retry failed)`

### Zmiany w kodzie
- Nowy modul: `processing_index.py`.
- Rozszerzenie parsera argumentow CLI.
- Aktualizacja zapisow po kazdym sukcesie/porazce.

### Kryteria akceptacji
- Drugie uruchomienie na tym samym secie plikow nie wywoluje modelu dla rekordow `done`.
- Opcje `--force`, `--retry-failed`, `--recheck-review` dzialaja zgodnie z oczekiwaniem.

## 3. Proponowana kolejnosc wdrozen

1. Pelna polszczyzna wyjscia.
2. Pomijanie juz sklasyfikowanych zdjec.
3. Wiele znaczkow na jednym zdjeciu.
4. Sugestia ceny z Allegro.

Uzasadnienie:
- 1 i 2 daja szybki zysk biznesowy i redukcje kosztu.
- 3 rozszerza wejscie, ale jest bardziej zlozone algorytmicznie.
- 4 zalezy od jakosci danych zewnetrznych i warto robic po stabilizacji klasyfikacji.
