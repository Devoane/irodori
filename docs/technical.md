# Irodori: technical details

Colored prints from a few spools on toolchanger printers. From a textured or painted model it picks 2–4 spools (preview
up to 8), builds up to 251 layer mixes from them, shows the predicted print and saves a project for Snapmaker Orca 2.4 or
G-code. The export template and the tests target the Snapmaker U1 (`backend/templates`); another toolchanger needs its own
template and profile mapping. It works locally, without internet. User guide: [README](../README.md) and `frontend/help.html`.

## Environment variables

`IRODORI_PORT`, `IRODORI_NO_BROWSER`, `IRODORI_ORCA`, `IRODORI_BLENDER`, `IRODORI_ORCA_CONFIG`, `IRODORI_CONTAINER`
(a distrobox with the tools, when the server runs outside it). Tool and folder lookup: `backend/tools.py`.

## Security

- The server listens on 127.0.0.1 only. `backend/guard.py` rejects requests with a foreign `Host` header (DNS rebinding), requests from any other origin, including other ports on this computer (`Origin`, `Sec-Fetch-Site`), and bodies without a declared size or over 1 GiB (`Content-Length`). A strict CSP blocks outside resources.
  a foreign `Origin` or `Sec-Fetch-Site: cross-site` (CSRF), and bodies larger than 1 GiB.
- CSP `default-src 'self'` with a hash for the import map. Fonts and libraries live in `frontend/`; the page connects to nothing outside.
- Uploaded file names are reduced to the bare name (no folders, no `..`). A 3MF that unpacks to more than 4 GiB is rejected.
- Blender starts with `--factory-startup --disable-autoexec`. Orca and Blender are called with an argument list, without a shell.
- Dependencies have version ranges in `requirements.txt`.

## Translations

English is the main language and the default for every new user; the choice in the header menu is remembered. English is also the source language: a string in the code is its own key. `frontend/i18n/<lang>.json` holds the other
languages, and `frontend/i18n.js` applies them to the static page, to `t()` and `tn()` calls and to server messages.
A key with `{name}` placeholders also translates a matching message from the server, so the backend only speaks English.
Plural entries are objects with `one`, `few`, `many` and `other` forms, picked with `Intl.PluralRules`.

To add a language: copy `frontend/i18n/pl.json`, translate the values, add the language to `LANGUAGES` in
`frontend/i18n.js`, then run `tools/subset_fonts.py` if it uses new non-Latin characters. `tests/test_core.py` checks that
every locale has the same keys and placeholders.

## How it works

- A recipe is a cycle of slots; one digit is one layer. The same spool appears at most 2 times in a row (inner wall: 3).
- A recipe's color is predicted with FilamentMixer from Orca (`filament_mixer_model.h`, MIT), mixed in slot order like
  `blend_color_multi`, so the color of a single recipe is identical to the swatch in Orca.
- "In slicer" is `compute_mixed_filament_display_color` from Orca, ported to `recipes.slicer_colors`: with two pattern groups
  Orca averages the outer group on 1 wall and the inner group on the rest (`wall_loops`) over a cycle of min(NWW, 48).
  Without a filter the Orca swatch differed from the print by up to 0.15 OKLab, so two wall recipes only take pairs
  with a difference below 0.035, and one recipe is the default.
- Mixes: greedy facility location weighted by area. "As many as needed" cuts the list when the rest improves the mean
  error by less than 1% (at least 0.0005 OKLab / 0.05 ΔE).
- "Highest" accuracy (`Settings.precise`, default): mixes are chosen and assigned in ΔE2000, the same measure as the match %
  (assignment takes the 6 nearest in OKLab and recomputes them in ΔE2000). The ranking scores 200 sets on 1200 buckets and refines the
  best 6 plus the CMY and T1–T4 sets by swapping single spools for any in the library (`palette._improve`). Full palettes are built
  for 2× more suggestions and the best are kept. Measured match %, fast → highest: DamagedHelmet 91.3 → 93.0 (54 → 188 s),
  GradientBox 83.4 → 85.5, Duck 98.7 → 98.8; CMY sets on the helmet 75.7 → 84.3. "CMY sets only" is off by default.
- Relative time: area × number of spools in the recipe. Single-spool areas are cheaper by the layer ratio with "thick layers".
- Overlay blending: samples with alpha < 1 are mixed with the nearest opaque surface (reach 1% of the size, at least 0.5 mm).
- Bases (`bases.py`): a polar grid on an ellipse around the model, a separate part of the object, model sunk 0.6 mm. The chosen base is not remembered (`UNSAVED` in `app.js`): every new model starts without one.
- Calibration (`calibration.py`): photo of the card → one gain per channel in linear light (exposure and white balance from the pure tiles; a richer map pulled spools onto their catalog color) → a quadratic model in OKLab. The terms belong to spools and pairs, so any set whose pairs were measured can use them in any order. It is saved only if the error on held-out tiles beats the model and ΔE 8. That threshold comes from tests on synthetic photos and has not been checked on a real card. The JSON also stores the raw measurements.
- Leaky meshes: winding number + marching cubes; colors always come from the original.

## Export format (checked with the Orca 2.4.0 CLI)

- Mix: `1,2,1,1,<mix_b>,0,g<slots>,w<weights>,m2,z0,xa0,xb0,d0,o0,u<id>,<pattern>`; with a=1, b=2 the pattern digits are slots.
  Two recipes: `outer,inner`.
- Several 3MF parts (for example a layered lithophane) stay separate parts with their own spool.
- `paint_color` state 0 = the object's spool (background behind the colored band: CMY+W → Magenta, CMY+K → Yellow).
- Wipe tower (`export_3mf.tower_spot`): a bed corner clear of the model, computed for a 60×90 mm tower, because Orca anchors
  it at the front-left corner and grows it with the number of mixes. The default (15, 220) gave "G-code path goes beyond plate boundaries" in the GUI.
- Painted 3MF: solids already leaky in the source (an edge not shared by exactly 2 triangles, counted on the original indices in
  `source._broken_bodies`, before painting splits triangles) are rebuilt (`geometry.mend_bodies`: winding number + marching cubes 0.12 mm + Taubin),
  with the color of the nearest triangle. Vertices are written with 9 digits.
- Color all the way in = `mmu_segmented_region_max_width` 0. Weave paints flat tops by subdividing triangles.
  `export_3mf._weave_plans` takes 1–2 spools whose dots, averaged like light (linear RGB, not the layer model), are closest to the wall color,
  with a penalty for contrast. Pairs with contrast > 0.2 OKLab and tops whose dots miss the color (> 0.08 OKLab) stay a layer mix. Default level 6 (1.3 mm).
  Weave only on real flat tops (`_flat_roofs`): a connected area with normal z > 0.985, ≥ 25 mm², height spread ≤ 0.15 mm.
- Layer profiles (VOL, thick layers, thin layers under tops) in `Metadata/layer_heights_profile.txt`: both ends of each layer are pinned, the last point
  is at the top of the object. Every profile forces `tree_hybrid` supports.
- Thin layers under tops only for flat tops (normal z > 0.985) of ≥ 25 mm² at that height; otherwise rounded models got thousands of thin layers.
- Layers < 0.08 mm: the project and the machine in the CLI get `min_layer_height` 0.04.
- Sinking: the CLI puts a sunk object back on the bed, so the export cuts the mesh (`manifold3d.trim_by_plane`).

Orca 2.4 CLI traps: `<plate>` in `model_settings.config` crashes it (issue #935); system profiles must be flattened;
"floating regions" without supports is an error; a leaky mesh with mixes breaks the wipe tower; two wall recipes
with supports on spool 0 crash `WipeTower2::prime`, so supports get the background spool.

## Tests

```
.venv/bin/python -m pytest -q tests/test_core.py
.venv/bin/python tests/orca_slice.py project.3mf                  # slices with the real Orca
node tests/e2e.mjs /tmp/out model.fbx texture.png                 # with the server running, needs Playwright
```

Test models (`tests/samples`) are not in the repository because they have their own licenses. Tests that need them are skipped.

Fonts in `frontend/fonts`, licenses beside them: Antykwa Toruńska (GUST Font License, headings and numbers), M PLUS 1, M PLUS 1p and
Shippori Mincho (SIL OFL 1.1, text and Japanese, subset to the characters in use by `tools/subset_fonts.py`).

Look: the red and white shared by the flags of Poland and Japan, cobalt like Bolesławiec pottery and Japanese indigo. `logo.svg` joins the white and red bands with a rising disc, `fala.svg` is seigaiha built from Bolesławiec peacock eyes, `herb.svg` is a kamon cut like a Polish paper cutout. The preview background is neutral gray so it does not change how colors read.
