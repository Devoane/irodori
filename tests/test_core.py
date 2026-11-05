import io
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest
import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from color import blend_multi, hex_to_rgb, mixer_lerp  # noqa: E402
from export_3mf import mixed_definition_row, vol_schedule, write_project  # noqa: E402
from paint import decode_triangle, encode_state, encode_tree, subdivide_states  # noqa: E402
from palette import Settings  # noqa: E402
from recipes import Recipe, build_candidates, generate_words, max_cyclic_run  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "samples"
CMYW = ["#00C3FF", "#F54399", "#FAE727", "#F5F4F2"]


def test_mixer_matches_orca_header_example():
    # filament_mixer_model.h: blue + yellow -> (47, 141, 56)
    assert mixer_lerp(np.array([0, 33, 133]), np.array([252, 211, 0]), 0.5).tolist() == [47, 141, 56]


@pytest.mark.parametrize("state,code", [(1, "4"), (2, "8"), (3, "0C"), (4, "1C"), (5, "2C"), (17, "EC"),
                                        (18, "0FC"), (33, "0FFC"), (255, "C" + "F" * 16 + "C")])
def test_paint_encode_golden(state, code):
    assert encode_state(state) == code
    assert decode_triangle(code, [0, 0, 0], [1, 0, 0], [0, 1, 0])[0][1] == state


def test_paint_tree_roundtrip():
    def at(p):
        return 2 if p[0] < 1 else 7

    tree = subdivide_states([0, 0, 0], [4, 0, 0], [0, 4, 0], at, 0.5)
    leaves = decode_triangle(encode_tree(tree), [0, 0, 0], [4, 0, 0], [0, 4, 0])
    assert all(at(np.mean(t, axis=0)) == s for t, s in leaves)
    area = sum(0.5 * np.linalg.norm(np.cross(t[1] - t[0], t[2] - t[0])) for t, _ in leaves)
    assert area == pytest.approx(8.0)


def test_paint_tree_default_state_is_unpainted():
    assert encode_tree(3, default_state=3) == "0"
    assert encode_tree(1, default_state=3) == "4"


def test_words_respect_max_run():
    for w in generate_words(4, 20, 2):
        assert max_cyclic_run(w) <= 2 and len(set(w)) >= 2
    for w in generate_words(4, 6, 3):
        assert max_cyclic_run(w) <= 3


def test_candidates_match_orca_blend():
    P = np.array([hex_to_rgb(h) for h in CMYW])
    recipes, rgb = build_candidates(P, 12, 2)
    for r, c in list(zip(recipes, rgb))[:200]:
        slots = sorted(r.counts)
        assert np.allclose(blend_multi(P[[s - 1 for s in slots]], [r.counts[s] for s in slots]), c)


def test_dual_candidates_have_inner_words():
    P = np.array([hex_to_rgb(h) for h in CMYW])
    recipes, rgb = build_candidates(P, 12, 2, dual=True)
    assert len(recipes) == len(rgb)
    assert all(r.inner is None for r in recipes[:4])
    assert all(r.inner is not None and max_cyclic_run(r.inner) <= 3 for r in recipes[4:])


def _parse_row(row: str):
    """The parts of Orca's parse_row_definition we rely on."""
    tok = row.split(",")
    meta, pattern = {}, []
    for t in tok[6:]:
        key = t[:2] if t[:2] in ("xa", "xb", "cm") else t[0]
        if key in ("g", "w", "m", "z", "d", "o", "u", "xa", "xb", "cm"):
            meta[key] = t
        else:
            pattern.append(t)
    return tuple(int(x) for x in tok[:4]), meta, pattern


def test_mixed_definition_row():
    head, meta, pattern = _parse_row(mixed_definition_row(Recipe((3, 4, 3, 1, 4)), 1007))
    assert head == (1, 2, 1, 1)               # cycle mode: tokens are literal slots
    assert pattern == ["34314"]
    assert meta["u"] == "u1007" and meta["g"] == "g134"
    _, _, pattern = _parse_row(mixed_definition_row(Recipe((3, 4, 4), (1, 2)), 5))
    assert pattern == ["344", "12"]          # outer wall group first


def test_vol_schedule():
    sched = vol_schedule(0.1, 20.0, 0.1)
    heights = [h for _, h in sched[1:41]]
    assert sorted(set(np.round(heights, 3))) == [0.08, 0.1, 0.12]
    assert np.mean(heights) == pytest.approx(0.1)


def _project(**kw):
    m = trimesh.creation.box((10, 10, 10))
    states = np.full(len(m.faces), 2)
    states[:4] = 5
    fil = [{"hex": h, "profile": "Snapmaker PLA Full Spectrum @U1 0.4 nozzle"} for h in CMYW]
    recipes = [Recipe((i,)) for i in range(1, 5)] + [Recipe((1, 2))]
    data = write_project(m, states, fil, recipes, "box", Settings(**kw), backing_slot=2)
    z = zipfile.ZipFile(io.BytesIO(data))
    obj = [n for n in z.namelist() if n.startswith("3D/Objects/")][0]
    return z, json.loads(z.read("Metadata/project_settings.config")), z.read(obj).decode()


def test_write_project_structure():
    z, cfg, xml = _project(take_interior=False, interweave=0)
    assert cfg["filament_colour"] == CMYW
    assert cfg["printer_settings_id"] == "Snapmaker U1 (0.4 nozzle)"
    assert cfg["mixed_filament_definitions"].endswith(",12")
    assert xml.count('paint_color="2C"') == 4          # virtual state 5
    assert xml.count("paint_color") == 4               # backing slot 2 stays unpainted
    ms = z.read("Metadata/model_settings.config").decode()
    assert 'key="extruder" value="2"' in ms and "<plate>" not in ms
    assert cfg["mmu_segmented_region_max_width"] != "0"


def test_weave_plans():
    from export_3mf import _weave_plans
    wbry = np.array([hex_to_rgb(h) for h in ("#F5F5F5", "#0055CC", "#D01010", "#FFDD00")], float)
    orange, grey, brown = Recipe((3, 4, 4, 4)), Recipe((2, 4)), Recipe((2, 3, 4, 2, 3, 4, 3))
    p_orange, p_grey, p_brown = _weave_plans([orange, grey, brown], wbry)
    assert p_orange[0] == [3, 4]               # red dots in yellow
    assert p_grey is None or p_grey[0] != [2, 4]   # no blue/yellow confetti
    assert p_brown is None                     # side-by-side dots cannot make brown


def test_flat_roofs():
    from export_3mf import _flat_roofs
    box = trimesh.creation.box((10, 10, 10))
    assert _flat_roofs(box)[box.face_normals[:, 2] > 0.99].all()
    assert not _flat_roofs(trimesh.creation.icosphere(5, radius=40)).any()   # a dome cap is no roof


def test_tower_spot_clear_of_model():
    from export_3mf import BED_CENTER, TOWER_OFFSET, TOWER_SIZE, tower_spot
    x, y = tower_spot(trimesh.creation.box((120, 120, 20)))
    assert x - TOWER_OFFSET >= 0 and y - TOWER_OFFSET + TOWER_SIZE[1] <= 270     # grows inside the bed
    assert x - TOWER_OFFSET + TOWER_SIZE[0] < BED_CENTER[0] - 60 or y - TOWER_OFFSET + TOWER_SIZE[1] < BED_CENTER[1] - 60


def test_broken_bodies_flagged():
    from source import _broken_bodies
    good = trimesh.creation.box((1, 1, 1)).faces
    holed = trimesh.creation.icosphere(1).faces[1:] + good.max() + 1          # one face missing
    ids = _broken_bodies(np.concatenate([good, holed]), 100)
    assert (ids[:len(good)] == -1).all() and (ids[len(good):] >= 100).all()


def test_write_project_options():
    _, cfg, _ = _project(take_interior=True, subdivide_mix=True, interweave=0)
    assert cfg["mmu_segmented_region_max_width"] == "0"
    assert cfg["dithering_local_z_mode"] == "1"
    z, cfg, _ = _project(vol=True, supports=True)
    prof = z.read("Metadata/layer_heights_profile.txt").decode()
    assert prof.startswith("object_id=1|")
    zs = [float(v) for v in prof.split("|")[1].split(";")[::2]]
    assert zs[-1] == pytest.approx(10.0) and all(a < b for a, b in zip(zs, zs[1:]))
    assert cfg["support_style"] == "tree_hybrid"
    assert cfg["support_filament"] == cfg["support_interface_filament"] == "2"


@pytest.mark.skipif(not (SAMPLES / "Duck.glb").exists(), reason="sample model not downloaded")
def test_pipeline_duck():
    from geometry import build_print_mesh, face_adjacency_graph
    from palette import Targets, build_row, preset_rows
    from source import load_source
    src, warnings = load_source({"Duck.glb": (SAMPLES / "Duck.glb").read_bytes()}, "Duck.glb")
    s = Settings(max_triangles=50_000, color_threshold=3)
    mesh, frgb = build_print_mesh(src, s.max_triangles)
    name, fil, fam, _ = preset_rows(s)[0]
    row = build_row(name, fil, Targets(frgb, mesh.area_faces), s, fam,
                    adjacency=face_adjacency_graph(mesh), face_area=mesh.area_faces)
    assert not warnings
    assert row.metrics["match"] > 70
    assert len(row.recipes) <= 255 and row.labels.max() < len(row.recipes)


def test_write_project_keeps_overlapping_parts():
    # layered lithophane: a white slab with a coloured slab inside must stay two parts
    a = trimesh.creation.box(extents=(10, 10, 2.5))
    b = trimesh.creation.box(extents=(8, 8, 0.6))
    b.apply_translation([0, 0, -0.6])
    m = trimesh.util.concatenate([a, b])
    states = np.r_[np.full(len(a.faces), 4), np.full(len(b.faces), 1)]
    part = np.r_[np.zeros(len(a.faces), int), np.ones(len(b.faces), int)]
    fil = [{"hex": h, "profile": "Snapmaker PLA Full Spectrum @U1 0.4 nozzle"} for h in CMYW]
    data = write_project(m, states, fil, [Recipe((i,)) for i in range(1, 5)], "litho", Settings(), 4, part)
    z = zipfile.ZipFile(io.BytesIO(data))
    ms = z.read("Metadata/model_settings.config").decode()
    assert ms.count("<part ") == 2
    assert '<part id="1"' in ms and '<part id="2"' in ms
    assert ms.index('value="4"', ms.index('<part id="1"')) < ms.index('<part id="2"')
    assert 'key="extruder" value="1"' in ms.split('<part id="2"')[1]
    obj = z.read([n for n in z.namelist() if n.startswith("3D/Objects/")][0]).decode()
    assert obj.count("<object ") == 2 and "paint_color" not in obj
    assert z.read("3D/3dmodel.model").decode().count("<component ") == 2


def test_auto_capacity_stops_early():
    from palette import Targets, select_virtual
    from color import rgb_to_oklab
    rng = np.random.default_rng(0)
    face = np.repeat(np.array([[200, 40, 40], [40, 160, 220], [240, 220, 60]], float), 200, axis=0)
    face += rng.normal(0, 1.5, face.shape)
    t = Targets(np.clip(face, 0, 255), np.ones(len(face)))
    P = np.array([hex_to_rgb(h) for h in CMYW])
    _, rgb = build_candidates(P, 12, 2)
    ok = rgb_to_oklab(rgb)
    full = select_virtual(t, ok, 4, 251)
    auto = select_virtual(t, ok, 4, 251, auto=True)
    assert 1 <= len(auto) < len(full)
    assert auto == full[:len(auto)]


def test_lock_physical():
    from color import rgb_to_lab
    from palette import lock_physical
    P = np.array([hex_to_rgb(h) for h in CMYW])
    face = np.array([P[2], P[2] + [1, -1, 0], [128, 128, 128]])
    assert lock_physical(rgb_to_lab(face), rgb_to_lab(P)).tolist() == [2, 2, -1]


def test_adjust_and_rainbow():
    from adjust import adjust_colors, hsv_rgb, rainbow_colors
    assert np.allclose(hsv_rgb(np.array([0.0, 120.0, 240.0])), [[255, 0, 0], [0, 255, 0], [0, 0, 255]])
    rgb = np.array([[120.0, 80.0, 60.0]])
    assert adjust_colors(rgb).tolist() == rgb.tolist()
    assert adjust_colors(rgb, brightness=50).sum() > rgb.sum() > adjust_colors(rgb, brightness=-50).sum()
    grey = adjust_colors(rgb, saturation=-100)[0]
    assert np.ptp(grey) < 2
    pts = np.array([[1, 0, 0], [0, 1, 0], [-1, 0, 0], [0, 0, 5.0]])
    rb = rainbow_colors(pts, vertical=0, horizontal=0)
    assert len({tuple(np.round(c)) for c in rb[:3]}) == 3          # hue turns around the axis
    assert np.allclose(rainbow_colors(pts, white=75).min(), 0.75 * 255)


def test_adaptive_schedule():
    from export_3mf import adaptive_schedule
    m = trimesh.creation.box((10, 10, 10))
    m.apply_translation([0, 0, 5])
    recipes = [Recipe((i,)) for i in range(1, 5)] + [Recipe((1, 2))]
    states = np.ones(len(m.faces), int)
    mid = (m.triangles_center[:, 2] > 3) & (m.triangles_center[:, 2] < 7) & (np.abs(m.face_normals[:, 2]) < 0.5)
    assert adaptive_schedule(m, states, recipes, 0.1, 0.2) is None
    assert adaptive_schedule(m, states, recipes, 0.1, 0.2, coarse=0.2)[5][1] == pytest.approx(0.2)
    states[mid] = 5                      # side faces cover the whole height: everything stays mixed
    prof = adaptive_schedule(m, states, recipes, 0.1, 0.2, coarse=0.2)
    assert prof is None or all(h == pytest.approx(0.1) for _, h in prof[1:])
    prof = adaptive_schedule(m, np.ones(len(m.faces), int), recipes, 0.1, 0.2, fine=0.06, top_depth=0.6)
    hs = dict(prof)
    assert all(h == pytest.approx(0.06) for zz, h in prof[1:] if zz > 9.45)
    assert any(h == pytest.approx(0.1) for zz, h in prof[1:] if zz < 9)
    assert prof[-1][0] + prof[-1][1] == pytest.approx(10, abs=1e-3) and hs


def test_place_mesh_and_first_layer():
    from export_3mf import first_layer, place_mesh
    m = trimesh.creation.box((10, 20, 30))
    out, dz = place_mesh(m, Settings(rotate=True, rot_x=90, z_offset=-2))
    assert np.allclose(out.extents, [10, 30, 20], atol=1e-6) and out.bounds[0][2] == pytest.approx(0)
    assert dz == -2
    with pytest.raises(ValueError):
        place_mesh(m, Settings(z_offset=-31))
    assert first_layer(0.08) == 0.2 and first_layer(0.24) == 0.24


def test_export_layer_profiles_and_limits():
    z, cfg, _ = _project(layer_height=0.04)
    assert cfg["initial_layer_print_height"] == "0.2" and cfg["min_layer_height"][0] == "0.04"
    z, cfg, _ = _project(variable_layer=0.2, detailed_top=0.06)
    prof = z.read("Metadata/layer_heights_profile.txt").decode().split("|")[1].split(";")
    heights = {round(float(h), 3) for h in prof[1::2]}
    assert {0.2, 0.06} <= heights <= {0.2, 0.1, 0.06} | {round(float(h), 3) for h in prof[1::2] if float(h) < 0.2}
    assert cfg["min_layer_height"][0] == "0.04"
    z, _, _ = _project(variable_layer=0.2, subdivide_mix=True)
    assert "Metadata/layer_heights_profile.txt" not in z.namelist()
    fil = [{"hex": h, "profile": "Generic PLA"} for h in CMYW + ["#000000"]]
    with pytest.raises(ValueError):
        write_project(trimesh.creation.box((5, 5, 5)), np.ones(12, int), fil, [Recipe((i,)) for i in range(1, 6)],
                      "x", Settings())


def test_diverse_groups_similar_sets():
    from palette import diverse
    ranking = [{"ids": list("ABCD"), "match": 90}, {"ids": list("ABCE"), "match": 89},
               {"ids": list("ABCF"), "match": 88}, {"ids": list("WXYZ"), "match": 70}]
    out = diverse(ranking, 2, 4)
    assert [o["ids"] for o in out] == [list("ABCD"), list("WXYZ")]
    assert out[0]["grouped"] == 2 and out[1]["grouped"] == 0


def test_obj_with_mtl_and_texture(tmp_path):
    from PIL import Image
    from source import load_source
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (220, 30, 30)).save(buf, "PNG")
    obj = ("mtllib m.mtl\nv 0 0 0\nv 10 0 0\nv 0 10 0\nv 0 0 10\nvt 0 0\nvt 1 0\nvt 0 1\nusemtl red\n"
           "f 1/1 3/3 2/2\nf 1/1 2/2 4/3\nf 1/1 4/3 3/3\nf 2/2 3/3 4/3\n")
    mtl = "newmtl red\nKd 1 1 1\nmap_Kd textures\\red.png\n"
    src, warn = load_source({"a.obj": obj.encode(), "m.mtl": mtl.encode(), "red.png": buf.getvalue()}, "a.obj")
    assert not warn
    assert np.allclose(src.sample_rgb.mean(axis=0), [220, 30, 30], atol=3)
    assert max(np.ptp(src.vertices, axis=0)) == pytest.approx(10)        # auto size: kept from the file
    _, warn = load_source({"a.obj": obj.encode(), "m.mtl": mtl.encode()}, "a.obj")
    assert warn == ["Missing file: red.png"]


def test_settings_validation():
    from server import _settings
    s = _settings(json.dumps({"toolheads": 9, "capacity": 7, "layer_height": 0.04, "vol": True,
                              "variable_layer": 0.02, "detailed_top": 0.08, "z_offset": 3}))
    assert s.toolheads == 8 and s.no_dither and not s.vol
    assert s.variable_layer == 0 and s.detailed_top == 0 and s.z_offset == 0
    s = _settings(json.dumps({"vol": True, "subdivide_mix": True, "variable_layer": 0.2}))
    assert s.vol and not s.subdivide_mix and s.variable_layer == 0
    assert _settings("{}").capacity == 0 and _settings("{}").max_colors == 255


def test_bake_overlays():
    from source import bake_overlays
    base = np.array([[x, 0.0, 0.0] for x in range(10)])
    decal = np.array([[x, 0.0, 0.2] for x in range(3)])
    pts = np.r_[base, decal]
    rgb = np.r_[np.tile([255.0, 0, 0], (10, 1)), np.tile([0, 0, 255.0], (3, 1))]
    alpha = np.r_[np.ones(10), np.full(3, 0.5)]
    out = bake_overlays(pts, rgb, alpha, reach=0.5)
    assert np.allclose(out[10:], [127.5, 0, 127.5])          # decal over red
    assert np.allclose(out[:3], [127.5, 0, 127.5])           # red under the decal
    assert np.allclose(out[5:10], [255, 0, 0])               # out of reach: untouched
    assert np.array_equal(bake_overlays(pts, rgb, np.ones(13), 0.5), rgb)


def test_bases_are_closed_and_hold_the_model():
    from bases import EMBED, STYLES, attach_base, make_base
    model = trimesh.creation.box((30, 12, 20))
    model.apply_translation([5, 0, 10])
    for style in STYLES:
        base, rgb = make_base(model.vertices, style, 1)
        assert base.is_watertight and base.volume > 0, style
        assert rgb.shape == (len(base.faces), 3) and np.isfinite(rgb).all(), style
        lo, hi = base.bounds
        assert lo[0] < model.bounds[0][0] and hi[0] > model.bounds[1][0], style
    mesh, rgb, is_base, lift = attach_base(model, np.full((len(model.faces), 3), 200.0), "round", 0)
    assert mesh.bounds[0][2] == pytest.approx(0) and is_base.sum() == len(mesh.faces) - len(model.faces)
    top_of_base = mesh.submesh([np.nonzero(is_base)[0]], append=True).bounds[1][2]
    assert mesh.submesh([np.nonzero(~is_base)[0]], append=True).bounds[0][2] == pytest.approx(top_of_base - EMBED)
    fil = [{"hex": h, "profile": "Snapmaker PLA Full Spectrum @U1 0.4 nozzle"} for h in CMYW]
    data = write_project(mesh, np.where(is_base, 4, 1), fil, [Recipe((i,)) for i in range(1, 5)], "b", Settings(),
                         1, is_base.astype(int))
    ms = zipfile.ZipFile(io.BytesIO(data)).read("Metadata/model_settings.config").decode()
    assert ms.count("<part ") == 2


def test_cut_at_plate_keeps_colours_and_parts():
    from export_3mf import cut_at_plate
    a = trimesh.creation.box((20, 20, 10)); a.apply_translation([0, 0, 5])
    b = trimesh.creation.box((6, 6, 6)); b.apply_translation([0, 0, 12])
    m = trimesh.util.concatenate([a, b])
    states = np.r_[np.full(len(a.faces), 2), np.full(len(b.faces), 3)]
    part = np.r_[np.zeros(len(a.faces), int), np.ones(len(b.faces), int)]
    out, st, pt = cut_at_plate(m, -4, states, part)
    assert out.bounds[0][2] == pytest.approx(0) and out.bounds[1][2] == pytest.approx(11)
    for p in (0, 1):
        assert out.submesh([np.nonzero(pt == p)[0]], append=True).is_watertight
    assert set(st[pt == 0]) == {2} and set(st[pt == 1]) == {3}
    z, _, _ = _project(z_offset=-3, rotate=True, rot_z=30)
    root = z.read("3D/3dmodel.model").decode()
    assert '135.0 135.0 0"' in root
    obj = z.read([n for n in z.namelist() if n.startswith("3D/Objects/")][0]).decode()
    zs = [float(v) for v in __import__("re").findall(r'z="([^"]+)"', obj)]
    assert min(zs) == pytest.approx(0, abs=1e-4) and max(zs) == pytest.approx(7, abs=1e-3)


def _orca_swatch(recipe, P, walls):
    """compute_mixed_filament_display_color for a manual pattern, written as in Orca's C++."""
    from math import lcm
    groups = recipe.groups()
    if len(groups) == 1:
        seq = list(groups[0])
    else:
        cycle = 1
        for g in groups:
            cycle = min(lcm(cycle, len(g)), 48)
        seq = [groups[min(w, len(groups) - 1)][k % len(groups[min(w, len(groups) - 1)])]
               for k in range(cycle) for w in range(max(1, walls))]
    ids = sorted(set(seq))
    return blend_multi(P[[i - 1 for i in ids]], [seq.count(i) for i in ids])


def test_slicer_swatch_matches_orca():
    from recipes import slicer_colors
    P = np.array([hex_to_rgb(h) for h in CMYW])
    recs = [Recipe((1, 2, 1)), Recipe((3,)), Recipe((3, 2, 3, 2, 3, 2, 3, 2, 3), (3,)), Recipe((1, 2), (4, 4, 3)),
            Recipe(tuple([1, 2] * 5 + [3]), (2, 4, 4, 3, 1))]
    for walls in (1, 2, 5):
        got = slicer_colors(recs, P, walls)
        for r, g in zip(recs, got):
            assert np.allclose(g, _orca_swatch(r, P, walls)), (r, walls)


def test_dual_candidates_show_the_printed_colour_in_orca():
    from color import rgb_to_oklab
    from recipes import SLICER_TOL, slicer_colors
    P = np.array([hex_to_rgb(h) for h in CMYW])
    recipes, rgb = build_candidates(P, 12, 2, dual=True, walls=5)
    sw = slicer_colors(recipes, P, 5)
    assert np.linalg.norm(rgb_to_oklab(rgb) - rgb_to_oklab(sw), axis=1).max() < SLICER_TOL + 1e-6
    assert len(recipes) > 1000


def _card_photo(ids_n=4, gain=(0.8, 0.9, 1.1), noise=0.0):
    """A synthetic photo: quadratic 'true' print colours seen through a camera with these gains."""
    from calibration_card import layout
    from color import linear_to_srgb, oklab_to_rgb, rgb_to_oklab, srgb_to_linear
    lay = layout(ids_n)
    fr = np.array([t["fractions"] for t in lay["tiles"]])
    pure = [next(i for i, t in enumerate(lay["tiles"]) if t["pure"] == k) for k in range(ids_n)]
    nominal = np.array([hex_to_rgb(h) for h in CMYW[:ids_n]], dtype=np.float64)
    lab = fr @ rgb_to_oklab(nominal)
    lab[:, 0] -= 0.25 * fr[:, 0] * fr[:, 1]     # darker than a linear mix: what a real print adds
    true_rgb = np.clip(oklab_to_rgb(lab), 0, 255)
    rng = np.random.default_rng(1)
    seen = np.clip(srgb_to_linear(true_rgb) * np.array(gain), 0, 1)
    return fr, np.clip(linear_to_srgb(seen) + rng.normal(0, noise, seen.shape), 0, 255), nominal, pure, true_rgb


def test_calibration_recovers_print_colours_through_camera_gains():
    import calibration
    from color import delta_e2000, rgb_to_lab
    fr, photo, nominal, pure, true_rgb = _card_photo(noise=1.0)
    coef, info = calibration.fit(fr, photo, nominal, pure)
    got = calibration.Correction(coef).apply_many(fr * 12)
    assert delta_e2000(rgb_to_lab(got), rgb_to_lab(true_rgb)).mean() < 3.0
    assert info["heldout_de"] < info["model_de"]


def test_calibration_keeps_spool_that_differs_from_catalogue():
    import calibration
    from color import delta_e2000, rgb_to_lab
    fr, photo, nominal, pure, true_rgb = _card_photo(gain=(1, 1, 1))
    wrong = nominal.copy()
    wrong[0] = wrong[0] * 0.8      # the catalogue says a darker cyan than the spool really is
    coef, info = calibration.fit(fr, photo, wrong, pure)
    got = calibration.Correction(coef).apply_many(fr[[pure[0]]] * 12)
    assert delta_e2000(rgb_to_lab(got), rgb_to_lab(true_rgb[[pure[0]]]))[0] < 6.0
    assert info["spool_de"][0] > 3.0


def test_calibration_terms_are_reused_for_reordered_and_smaller_sets(tmp_path, monkeypatch):
    import calibration
    monkeypatch.setattr(calibration, "CAL_DIR", tmp_path)
    fr, photo, nominal, pure, _ = _card_photo()
    coef, info = calibration.fit(fr, photo, nominal, pure)
    calibration.save(["a", "b", "c", "d"], coef, info, fr, photo)
    full = calibration.load_correction(["a", "b", "c", "d"]).apply_many(np.array([[1, 2, 0, 0], [0, 0, 3, 1]]))
    swapped = calibration.load_correction(["d", "c", "b", "a"]).apply_many(np.array([[0, 0, 2, 1], [1, 3, 0, 0]]))
    assert np.allclose(full, swapped)
    subset = calibration.load_correction(["b", "a"]).apply_many(np.array([[2, 1]]))
    assert np.allclose(subset[0], full[0])
    assert calibration.load_correction(["a", "e"]) is None


def test_calibration_rejects_scrambled_photo():
    import calibration
    fr, photo, nominal, pure, _ = _card_photo()
    _, good = calibration.fit(fr, photo, nominal, pure)
    _, bad = calibration.fit(fr, np.random.default_rng(3).permutation(photo), nominal, pure)
    assert calibration.acceptable(good)
    assert not calibration.acceptable(bad)


def test_translations_cover_the_same_keys_and_placeholders():
    import re
    folder = Path(__file__).resolve().parent.parent / "frontend" / "i18n"
    marks = lambda s: sorted(re.findall(r"\{\w+\}|<[^>]+>", s))
    locales = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.json") if p.stem != "en"}
    keys = set(next(iter(locales.values())))
    for code, data in locales.items():
        assert set(data) == keys, code
        for key, value in data.items():
            if isinstance(value, str):
                assert marks(key) == marks(value), (code, key)
            else:
                assert "other" in value and all("{n}" in v for v in value.values()), (code, key)


def test_every_string_the_interface_uses_has_a_translation():
    import re
    root = Path(__file__).resolve().parent.parent / "frontend"
    keys = set(json.loads((root / "i18n" / "ja.json").read_text(encoding="utf-8")))
    js = (root / "app.js").read_text(encoding="utf-8")
    used = set(re.findall(r"\bt\('((?:[^'\\]|\\.)+)'", js)) | set(re.findall(r"\btn\('([^']+)'", js))
    used |= set(re.findall(r"setStatus\('([^']+)'", js))
    assert not {u.replace("\\'", "'") for u in used} - keys


def _guard_status(method="GET", **headers):
    """Status of one request through LocalOnly, around an app that always answers 200."""
    import asyncio

    from guard import LocalOnly
    from starlette.responses import PlainTextResponse

    guarded = LocalOnly(PlainTextResponse("ok"), policy="default-src 'self'")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": "/", "raw_path": b"/",
             "scheme": "http", "query_string": b"", "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 1),
             "headers": [(k.replace("_", "-").lower().encode(), v.encode()) for k, v in {"host": "127.0.0.1:8765", **headers}.items()]}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(guarded(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


@pytest.mark.parametrize("headers", [
    {},
    {"origin": "http://127.0.0.1:8765", "sec_fetch_site": "same-origin"},
    {"host": "localhost:8765", "origin": "http://localhost:8765"},
    {"host": "[::1]:8765", "origin": "http://[::1]:8765"},
    {"sec_fetch_site": "none"},
])
def test_guard_lets_the_app_itself_through(headers):
    assert _guard_status("POST", **headers) == 200


@pytest.mark.parametrize("host", ["evil.com", "evil.com:8765", "127.0.0.1.evil.com", "127.0.0.1@evil.com", "evil.com@127.0.0.1",
                                  "2130706433", "0.0.0.0", ""])
def test_guard_refuses_a_foreign_host(host):
    assert _guard_status(host=host) == 421


@pytest.mark.parametrize("headers", [
    {"origin": "https://evil.com"},
    {"origin": "http://127.0.0.1.evil.com"},
    {"origin": "http://evil.com@127.0.0.1:8765"},
    {"origin": "https://127.0.0.1:8765"},
    {"origin": "http://localhost:9999"},
    {"origin": "http://127.0.0.1:9999", "sec_fetch_site": "same-site"},
    {"origin": "null"},
    {"origin": "file://"},
    {"sec_fetch_site": "cross-site"},
    {"sec_fetch_site": "same-site"},
])
def test_guard_refuses_other_sites_and_other_local_ports(headers):
    assert _guard_status("POST", **headers) == 403


def test_guard_size_limit_cannot_be_avoided():
    assert _guard_status("POST", content_length=str((1 << 30) + 1)) == 413
    assert _guard_status("POST", content_length="abc") == 413
    assert _guard_status("POST", transfer_encoding="chunked") == 411


@pytest.mark.parametrize("raw", ["[]", "1", '"x"', "null", '{"layer_height": null}', '{"layer_height": NaN}', '{"colored_walls": Infinity}',
                                 '{"rot_x": 1e999}', '{"colored_walls": 99999999999999999999}', '{"base_size": []}'])
def test_invalid_settings_are_refused(raw):
    from server import _settings
    with pytest.raises(ValueError, match="Invalid settings"):
        _settings(raw)


def test_settings_are_kept_in_range():
    from server import _settings
    s = _settings('{"max_recipe_len": 1000000, "max_run": 1000000, "variable_layer": 1000, "z_offset": -100000}')
    assert (s.max_recipe_len, s.max_run, s.variable_layer, s.z_offset) == (40, 4, 0.0, -1000.0)


@pytest.mark.parametrize("name", ["smok_żółć.glb", "彩り.glb", 'a"; x=y\r\nX-Evil: 1.glb'])
def test_downloads_accept_any_file_name(name):
    from server import _download
    value = _download(b"", name, "model/gltf-binary").headers["content-disposition"]
    assert value.startswith("attachment; filename*=UTF-8''") and '"' not in value and "\r" not in value


def test_obj_in_another_encoding_loads(tmp_path):
    from source import obj_to_glb
    obj = "# żółć\n".encode("cp1250") + b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"
    assert obj_to_glb({"a.obj": obj}, "a.obj")[0][:4] == b"glTF"


@pytest.mark.parametrize("reference", ["{0}", "../../../../../../..{0}"])
def test_obj_cannot_read_files_outside_its_upload(tmp_path, reference):
    secret = tmp_path / "secret.mtl"
    secret.write_text("newmtl m\nKd 1 0 0\n")
    reads = []

    def watch(event, args):
        if event == "open" and args and str(secret) in str(args[0]):
            reads.append(args[0])

    sys.addaudithook(watch)
    from source import obj_to_glb
    obj = f"mtllib {reference.format(secret)}\nusemtl m\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n".encode()
    assert obj_to_glb({"a.obj": obj}, "a.obj")[1] == ["secret.mtl"]
    assert not reads
