# Irodori: szczegóły techniczne

Kolorowe wydruki z kilku szpul. Z teksturowanego albo malowanego modelu dobiera 2–4 szpule (podgląd do 8), tworzy
z nich do 251 mieszanek warstwowych, pokazuje przewidywany wydruk i zapisuje projekt dla Snapmaker Orca 2.4 albo
G-code dla Snapmaker U1. Działa lokalnie, bez internetu. Instrukcja dla użytkownika: [README](../README.md) i `frontend/help.html`.

## Zmienne środowiskowe

`IRODORI_PORT`, `IRODORI_NO_BROWSER`, `IRODORI_ORCA`, `IRODORI_BLENDER`, `IRODORI_ORCA_CONFIG`, `IRODORI_CONTAINER`
(distrobox z narzędziami, gdy serwer stoi poza nim). Szukanie narzędzi i folderów: `backend/tools.py`.

## Bezpieczeństwo

- Serwer słucha tylko na 127.0.0.1. `backend/guard.py` odrzuca żądania z obcym nagłówkiem `Host` (DNS rebinding),
  z obcym `Origin` albo `Sec-Fetch-Site: cross-site` (CSRF) i większe niż 1 GiB.
- CSP `default-src 'self'` z hashem import mapy; czcionki i biblioteki są w `frontend/`, strona nie łączy się z niczym na zewnątrz.
- Nazwy wgrywanych plików są przycinane do samej nazwy (bez folderów i `..`); 3MF, który rozpakowuje się do ponad 4 GiB, jest odrzucany.
- Blender startuje z `--factory-startup --disable-autoexec`; Orca i Blender są wołane listą argumentów, bez powłoki.
- Zależności mają przedziały wersji w `requirements.txt`.

## Jak to działa

- Receptura to cykl slotów, jedna cyfra = jedna warstwa; ta sama szpula najwyżej 2 razy z rzędu (wewnętrzna ściana: 3).
- Kolor receptury przewiduje FilamentMixer z Orca (`filament_mixer_model.h`, MIT), mieszany w kolejności slotów jak
  `blend_color_multi`, więc kolor jednej receptury jest identyczny z próbką w Orca.
- „W slicerze” = `compute_mixed_filament_display_color` z Orca, przeniesione do `recipes.slicer_colors`: przy dwóch grupach
  wzorca Orca uśrednia grupę zewnętrzną na 1 ścianie i wewnętrzną na pozostałych (`wall_loops`) w cyklu min(NWW, 48).
  Bez filtra próbka w Orca różniła się od wydruku nawet o 0,15 OKLab, dlatego dwie receptury ścian biorą tylko pary
  z różnicą < 0,035, a domyślnie jest jedna receptura.
- Mieszanki: zachłanny facility location ważony polem; „ile trzeba” ucina listę, gdy reszta poprawia średni
  błąd o mniej niż 1% (min. 0,0005 OKLab / 0,05 ΔE).
- Dokładność „najwyższa” (`Settings.precise`, domyślnie): mieszanki dobierane i przypisywane w ΔE2000 (tej samej mierze
  co % zgodności; przypis = 6 najbliższych w OKLab przeliczonych ΔE2000), ranking ocenia 200 zestawów na 1200 kubełkach
  i dopracowuje 6 najlepszych oraz zestawy CMY / T1–T4 wymianą pojedynczych szpul na dowolną z biblioteki
  (`palette._improve`), a pełne palety buduje dla 2× więcej propozycji i zostawia najlepsze. Pomiar (match %, szybka →
  najwyższa): DamagedHelmet 91,3 → 93,0 (54 → 188 s), GradientBox 83,4 → 85,5, Duck 98,7 → 98,8;
  zestawy CMY na hełmie 75,7 → 84,3. „Tylko zestawy CMY” domyślnie WYŁ. (wyłącza przeszukiwanie biblioteki).
- Czas względny: pole × liczba szpul receptury; pola jednoszpulowe tańsze o stosunek warstw przy „grubych warstwach”.
- Wtapianie nakładek: próbki z alfą < 1 mieszane z najbliższą nieprzezroczystą powierzchnią (zasięg 1% rozmiaru, min. 0,5 mm).
- Podstawki (`bases.py`): siatka biegunowa na elipsie obejmującej model, osobna część obiektu, model zagłębiony 0,6 mm. Wybór podstawki nie jest zapamiętywany (`UNSAVED` w app.js): każdy nowy model startuje bez niej.
- Kalibracja (`calibration.py`): zdjęcie karty → po jednym wzmocnieniu na kanał w świetle liniowym (ekspozycja i balans bieli z czystych pól; bogatsza mapa ściągała szpule na kolor z katalogu) → model kwadratowy w OKLab. Wyrazy modelu należą do szpul i par, więc każdy zestaw o zmierzonych parach korzysta z nich w dowolnej kolejności. Zapisywana tylko, gdy błąd na pominiętych polach jest mniejszy niż modelu i niż ΔE 8 (próg przyjęty z testów na sztucznych zdjęciach, do sprawdzenia na prawdziwej karcie); JSON trzyma też surowe pomiary.
- Nieszczelne siatki: liczba nawinięć + marching cubes; kolory zawsze z oryginału.

## Format eksportu (sprawdzony z Orca 2.4.0 CLI)

- Mieszanka: `1,2,1,1,<mix_b>,0,g<sloty>,w<wagi>,m2,z0,xa0,xb0,d0,o0,u<id>,<wzorzec>`; przy a=1, b=2 cyfry wzorca to sloty.
  Dwie receptury: `zewnętrzna,wewnętrzna`.
- Kilka części 3MF (np. warstwowy litofan) zostaje osobnymi częściami z własną szpulą.
- `paint_color` stan 0 = szpula obiektu (tło za kolorowym pasem: CMY+W → Magenta, CMY+K → Yellow).
- Wieża czyszcząca (`export_3mf.tower_spot`): róg stołu wolny od modelu, liczony dla wieży 60×90 mm, bo Orca kotwiczy
  ją w lewym przednim rogu i powiększa z liczbą mieszanek; domyślne (15, 220) w GUI dawało „G-code path goes beyond
  plate boundaries”.
- Pomalowane 3MF: bryły nieszczelne już w źródle (krawędź nie dzielona przez dokładnie 2 trójkąty, liczone na
  oryginalnych indeksach w `source._broken_bodies`, zanim malowanie potnie trójkąty) są przebudowywane
  (`geometry.mend_bodies`: winding number + marching cubes 0,12 mm + Taubin), kolor z najbliższego trójkąta; reszta
  bez zmian. Tygrys: 10 brył (opuszki, poduszki łap), Orca zgłaszała „428 non-manifold edges”. Wierzchołki zapisywane z 9 cyframi.
- Kolor w głąb = `mmu_segmented_region_max_width` 0. Splot na dachach maluje dachy podziałami trójkątów.
  Splot (`export_3mf._weave_plans`) bierze 1–2 szpule, których kropki uśrednione jak światło (liniowe RGB, nie model
  warstw) są najbliżej koloru ścian, z karą za kontrast; pary o kontraście > 0,2 OKLab (np. niebieski+żółty) i dachy,
  których kropki nie trafiają w kolor (> 0,08 OKLab, np. brąz z WBRY), zostają mieszanką warstw. Wcześniej Bayer ze
  wszystkich szpul receptury robił pstrokate konfetti (tygrys z podstawką, 2026-10-27). Domyślny poziom 6 (1,3 mm).
  Splot tylko na prawdziwych dachach (`_flat_roofs`): spójny obszar z normalną z > 0,985, ≥ 25 mm², rozrzut wysokości
  ≤ 0,15 mm. Wcześniej (normalna > 0,95) prawie płaskie czubki kopuł i łap dostawały samotne łaty kratki.
- Profile warstw (VOL, grube warstwy, cienkie pod dachami) w `Metadata/layer_heights_profile.txt`: oba końce każdej
  warstwy przypięte, ostatni punkt na górze obiektu. Każdy profil wymusza podpory `tree_hybrid`.
- Cienkie warstwy pod dachami tylko dla płaskich dachów (normal z > 0,985) o ≥ 25 mm² na danej wysokości; inaczej
  zaokrąglone modele dostawały tysiące cienkich warstw i druk się wydłużał.
- Warstwy < 0,08 mm: projekt i maszyna w CLI dostają `min_layer_height` 0,04.
- Zatopienie: CLI kładzie zatopiony obiekt z powrotem na stół, więc eksport tnie siatkę (`manifold3d.trim_by_plane`).

Pułapki Orca 2.4 CLI: `<plate>` w `model_settings.config` = crash (issue #935); profile systemowe trzeba spłaszczyć;
„floating regions” bez podpór to błąd; nieszczelna siatka z mieszankami psuje wieżę czyszczącą; dwie receptury ścian
z podporami na szpuli 0 wywalają `WipeTower2::prime`, więc podpory dostają szpulę tła.

## Testy

```
.venv/bin/python -m pytest -q tests/test_core.py
.venv/bin/python tests/orca_slice.py projekt.3mf     # tnie prawdziwą Orcą
node tests/e2e.mjs /tmp/out model.fbx tekstura.png   # przy działającym serwerze, Playwright
```

Modele testowe (`tests/samples`) nie są w repozytorium, bo mają własne licencje. Testy, które ich wymagają, są pomijane.

Czcionki w `frontend/fonts`, licencje obok: Antykwa Toruńska (GUST Font License, nagłówki i liczby), M PLUS 1 i Shippori Mincho (SIL OFL 1.1, tekst i znaki japońskie, ograniczone do potrzebnych znaków).

Wygląd: czerwień i biel wspólne dla flag Polski i Japonii, kobalt jak ceramika z Bolesławca i japońskie indygo. `logo.svg` łączy biało-czerwony pas z dyskiem, `fala.svg` to seigaiha z oczek bolesławieckich, `herb.svg` to kamon cięty jak wycinanka. Tło podglądu jest neutralnie szare, żeby nie zmieniać odbioru kolorów.
