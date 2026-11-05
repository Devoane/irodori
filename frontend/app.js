import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { computeBoundsTree, disposeBoundsTree, acceleratedRaycast } from 'three-mesh-bvh';
import { initLanguage, num as fmt, onLanguage, t, tn } from './i18n.js';

await initLanguage();

THREE.BufferGeometry.prototype.computeBoundsTree = computeBoundsTree;
THREE.BufferGeometry.prototype.disposeBoundsTree = disposeBoundsTree;
THREE.Mesh.prototype.raycast = acceleratedRaycast;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const hexRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));

// colour math, mirrors backend/adjust.py
const SRGB2LIN = new Float32Array(256).map((_, i) => { const c = i / 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
const toLin = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
const fromLin = (l) => { l = Math.min(1, Math.max(0, l)); return 255 * (l <= 0.0031308 ? 12.92 * l : 1.055 * l ** (1 / 2.4) - 0.055); };
function rgb2oklab([r, g, b]) {
  const R = toLin(r), G = toLin(g), B = toLin(b);
  const l = Math.cbrt(0.4122214708 * R + 0.5363325363 * G + 0.0514459929 * B);
  const m = Math.cbrt(0.2119034982 * R + 0.6806995451 * G + 0.1073969566 * B);
  const s = Math.cbrt(0.0883024619 * R + 0.2817188376 * G + 0.6299787005 * B);
  return [0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s, 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s];
}
function oklab2rgb([L, a, b]) {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3, m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3,
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3;
  return [fromLin(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s), fromLin(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    fromLin(-0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)];
}
function adjustRgb(rgb, br, sa) {
  if (!br && !sa) return rgb;
  const lab = rgb2oklab(rgb), b = br / 100;
  lab[0] = b > 0 ? lab[0] + b * (1 - lab[0]) : lab[0] * (1 + b);
  lab[1] *= 1 + sa / 100; lab[2] *= 1 + sa / 100;
  return oklab2rgb(lab);
}
const hsv = (h) => [5, 3, 1].map((n) => { const k = (((n + h / 60) % 6) + 6) % 6; return 255 * (1 - Math.max(0, Math.min(k, 4 - k, 1))); });
function rainbowColors(cen, s) {
  const n = cen.length / 3, lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < n; i++) for (let k = 0; k < 3; k++) { lo[k] = Math.min(lo[k], cen[i * 3 + k]); hi[k] = Math.max(hi[k], cen[i * 3 + k]); }
  const cx = (lo[0] + hi[0]) / 2, cy = (lo[1] + hi[1]) / 2, H = Math.max(hi[2] - lo[2], 1e-9);
  const r = new Float32Array(n), ang = new Float32Array(n);
  let rmax = 1e-9;
  for (let i = 0; i < n; i++) {
    const dx = cen[i * 3] - cx, dy = cen[i * 3 + 1] - cy;
    r[i] = Math.hypot(dx, dy); rmax = Math.max(rmax, r[i]); ang[i] = Math.atan2(dy, dx) * 180 / Math.PI;
  }
  const out = new Float32Array(n * 3), w = Math.min(75, Math.max(0, s.rainbow_white)) / 100;
  for (let i = 0; i < n; i++) {
    const hue = s.rainbow_offset + (s.rainbow_ccw ? 1 : -1) * ang[i] + 360 * s.rainbow_h * r[i] / rmax + 360 * s.rainbow_v * (cen[i * 3 + 2] - lo[2]) / H;
    const c = hsv(hue);
    for (let k = 0; k < 3; k++) out[i * 3 + k] = c[k] + (255 - c[k]) * w;
  }
  return out;
}

const state = {
  files: [], newFiles: false, sid: null, version: -1, rows: [], selected: -1,
  view: 'source', opaque: false, brushOn: false, brush: 'paint', brushSize: 6,
  mesh: null,            // { nV, nF, src: source rgb per face, cen: face centroids }
  labels: null, focus: null, focusDirty: false,
  analysed: null, diag: null, library: null, orig: {}, brandsOff: new Set(), gjobs: new WeakMap(),
};

const analysedHeads = () => state.analysed?.toolheads || readSettings().toolheads;

// ---------------------------------------------------------------- viewer
const wrap = $('canvasWrap');
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
renderer.localClippingEnabled = true;
wrap.prepend(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color(0xa9a9a7);
const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 5000);
camera.up.set(0, 0, 1);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
scene.add(new THREE.HemisphereLight(0xffffff, 0x3a4050, 1.5));
const sun = new THREE.DirectionalLight(0xffffff, 1.5);
camera.add(sun); sun.position.set(0.4, 0.6, 1);
scene.add(camera);
const grid = new THREE.GridHelper(270, 27, 0x7f7f7d, 0x949492);
grid.rotation.x = Math.PI / 2;
scene.add(grid);
const plateClip = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
const model = new THREE.Group();                 // print pose: rotation + Z offset
scene.add(model);

let printMesh = null, sourceObj = null, bbox = new THREE.Box3();
const brushRing = new THREE.Mesh(new THREE.RingGeometry(0.92, 1, 48), new THREE.MeshBasicMaterial({ color: 0x22d3ee, side: THREE.DoubleSide, depthTest: false, transparent: true, opacity: 0.95 }));
brushRing.visible = false; brushRing.renderOrder = 10;
scene.add(brushRing);

function resize() {
  const r = wrap.getBoundingClientRect();
  renderer.setSize(r.width, r.height, false);
  camera.aspect = r.width / Math.max(r.height, 1);
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(wrap);
(function loop() { controls.update(); renderer.render(scene, camera); requestAnimationFrame(loop); })();

function setCam(kind) {
  if (bbox.isEmpty()) return;
  const c = bbox.getCenter(new THREE.Vector3());
  const d = bbox.getSize(new THREE.Vector3()).length() * 1.35;
  const dirs = { iso: [0.6, -0.8, 0.55], top: [0, -0.001, 1], front: [0, -1, 0.05], back: [0, 1, 0.05], left: [-1, 0, 0.05], right: [1, 0, 0.05] };
  camera.position.copy(c).add(new THREE.Vector3(...dirs[kind]).normalize().multiplyScalar(d));
  controls.target.copy(c);
  camera.near = d / 200; camera.far = d * 20; camera.updateProjectionMatrix();
}
$('cams').addEventListener('click', (e) => { const c = e.target.dataset.c; if (c) setCam(c); });
$('resetView').addEventListener('click', () => setCam('iso'));

function printMaterial() {
  const clip = (Number(readSettings().z_offset) || 0) < 0 ? [plateClip] : [];
  return new THREE.MeshLambertMaterial({ vertexColors: true, clippingPlanes: clip });
}

// rotation about the printer axes in X, Y, Z order (export_3mf.place_mesh), then centred and lowered by the Z offset
function applyPose() {
  const s = readSettings();
  model.rotation.set(...[s.rot_x, s.rot_y, s.rot_z].map((a) => (s.rotate ? a : 0) * Math.PI / 180), 'ZYX');
  model.position.set(0, 0, 0);
  model.updateMatrixWorld(true);
  const ref = printMesh || sourceObj;
  if (!ref) return;
  const b = new THREE.Box3().setFromObject(ref, true);
  const z = Math.min(0, s.z_offset);
  model.position.set(-(b.min.x + b.max.x) / 2, -(b.min.y + b.max.y) / 2, -b.min.z + z);
  model.updateMatrixWorld(true);
  bbox = new THREE.Box3().setFromObject(ref, true);
  if (z < 0) bbox.min.z = 0;
  const clip = z < 0 ? [plateClip] : [];
  model.traverse((o) => { if (o.isMesh) o.material.clippingPlanes = clip; });
}

function buildPrintMesh(buf) {
  const dv = new DataView(buf);
  const nV = dv.getUint32(0, true), nF = dv.getUint32(4, true);
  let o = 8;
  const verts = new Float32Array(buf, o, nV * 3); o += nV * 12;
  const faces = new Uint32Array(buf, o, nF * 3); o += nF * 12;
  const src = new Uint8Array(buf, o, nF * 3);
  const pos = new Float32Array(nF * 9), cen = new Float32Array(nF * 3);
  for (let f = 0; f < nF; f++) {
    for (let k = 0; k < 3; k++) {
      const vi = faces[f * 3 + k] * 3;
      for (let a = 0; a < 3; a++) { pos[f * 9 + k * 3 + a] = verts[vi + a]; cen[f * 3 + a] += verts[vi + a] / 3; }
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('color', new THREE.BufferAttribute(new Float32Array(nF * 9), 3));
  g.computeVertexNormals();
  g.computeBoundsTree();
  if (printMesh) { model.remove(printMesh); printMesh.geometry.disposeBoundsTree(); printMesh.geometry.dispose(); }
  printMesh = new THREE.Mesh(g, printMaterial());
  model.add(printMesh);
  state.mesh = { nV, nF, src, cen, adj: null, adjKey: '' };
  state.focus = new Uint8Array(nF);
  state.labels = null;
  $('empty').hidden = true;
  applyPose();
  setCam('iso');
  refreshColors();
}

// source colours as the analysis sees them (brightness, saturation, rainbow)
function sourceColors() {
  const m = state.mesh, s = readSettings();
  const key = JSON.stringify([s.brightness, s.saturation, s.rainbow, s.rainbow_v, s.rainbow_h, s.rainbow_ccw, s.rainbow_offset, s.rainbow_white]);
  if (m.adjKey === key) return m.adj;
  let base = m.src;
  if (s.rainbow) base = rainbowColors(m.cen, s);
  if (!s.brightness && !s.saturation && !s.rainbow) { m.adj = null; m.adjKey = key; return null; }
  const out = new Float32Array(m.nF * 3);
  for (let f = 0; f < m.nF; f++) {
    const c = adjustRgb([base[f * 3], base[f * 3 + 1], base[f * 3 + 2]], s.brightness, s.saturation);
    out[f * 3] = c[0]; out[f * 3 + 1] = c[1]; out[f * 3 + 2] = c[2];
  }
  m.adj = out; m.adjKey = key;
  return out;
}

function refreshColors() {
  if (!printMesh) return;
  const m = state.mesh, col = printMesh.geometry.attributes.color.array;
  const row = state.rows[state.selected];
  const predicted = state.view !== 'source' && row && state.labels;
  const ent = predicted ? row.entries.map((e) => hexRgb(state.view === 'slicer' ? e.slicer : e.hex)) : null;
  const adj = predicted ? null : sourceColors();
  const src = adj || m.src;
  const tint = state.brushOn && state.focus && state.focus.some((x) => x);
  for (let f = 0; f < m.nF; f++) {
    let lr, lg, lb;
    if (predicted) { const c = ent[state.labels[f]]; lr = SRGB2LIN[c[0]]; lg = SRGB2LIN[c[1]]; lb = SRGB2LIN[c[2]]; }
    else { lr = toLin(src[f * 3]); lg = toLin(src[f * 3 + 1]); lb = toLin(src[f * 3 + 2]); }
    if (tint) {
      if (state.focus[f]) { lr = lr * 0.55 + 0.1; lg = lg * 0.55 + 0.4; lb = lb * 0.55 + 0.45; }
      else { const y = (lr + lg + lb) / 3; lr = lg = lb = y * 0.35; }
    }
    for (let k = 0; k < 3; k++) { const i = f * 9 + k * 3; col[i] = lr; col[i + 1] = lg; col[i + 2] = lb; }
  }
  printMesh.geometry.attributes.color.needsUpdate = true;
  const showGlb = state.view === 'source' && sourceObj && !state.brushOn && !adj;
  if (sourceObj) sourceObj.visible = !!showGlb;
  printMesh.visible = !showGlb;
}

async function loadSource(sid) {
  const gltf = await new GLTFLoader().loadAsync(`/api/source/${sid}.glb`);
  if (sourceObj) model.remove(sourceObj);
  sourceObj = gltf.scene;
  sourceObj.traverse((o) => {
    if (!o.isMesh) return;
    const mat = o.material;
    if (!o.geometry.attributes.normal) o.geometry.computeVertexNormals();
    o.material = new THREE.MeshLambertMaterial({ map: mat.map || null, color: mat.color || 0xffffff, vertexColors: !!o.geometry.attributes.color });
  });
  model.add(sourceObj);
}

$('viewMode').addEventListener('click', (e) => {
  const v = e.target.dataset.v; if (!v) return;
  state.view = v;
  for (const b of $('viewMode').children) b.classList.toggle('on', b.dataset.v === v);
  refreshColors();
});
$('matMode').addEventListener('click', (e) => {
  const m = e.target.dataset.m; if (!m) return;
  state.opaque = m === 'opaque';
  for (const b of $('matMode').children) b.classList.toggle('on', b.dataset.m === m);
  saveSettings(); paintJobs(); updateDirty();
});

// ---------------------------------------------------------------- priority brush
const ray = new THREE.Raycaster(); ray.firstHitOnly = true;
const ndc = new THREE.Vector2();
let painting = false;
function setBrush(on) {
  state.brushOn = on;
  $('brushToggle').classList.toggle('on', on);
  controls.enabled = !on;
  wrap.classList.toggle('paint', on);
  brushRing.visible = false;
  refreshColors();
}
$('brushToggle').addEventListener('click', () => setBrush(!state.brushOn));
$('brushMode').addEventListener('click', (e) => {
  const b = e.target.dataset.b; if (!b) return;
  state.brush = b;
  for (const x of $('brushMode').children) x.classList.toggle('on', x.dataset.b === b);
  if (!state.brushOn) setBrush(true);
});
$('brushSize').addEventListener('input', (e) => { state.brushSize = +e.target.value; $('brushSizeVal').textContent = `${state.brushSize} mm`; });
$('brushClear').addEventListener('click', () => { if (!state.focus) return; state.focus.fill(0); state.focusDirty = true; refreshColors(); updateFocusInfo(); });

function hitAt(ev) {
  if (!printMesh) return null;
  const r = renderer.domElement.getBoundingClientRect();
  ndc.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(ndc, camera);
  const vis = printMesh.visible; printMesh.visible = true;
  const hits = ray.intersectObject(printMesh);
  printMesh.visible = vis;
  return hits[0] || null;
}
function paintAt(hit) {
  const c = state.mesh.cen, R2 = state.brushSize ** 2, p = printMesh.worldToLocal(hit.point.clone());
  const val = state.brush === 'paint' ? 1 : 0;
  for (let f = 0; f < state.mesh.nF; f++) {
    const dx = c[f * 3] - p.x, dy = c[f * 3 + 1] - p.y, dz = c[f * 3 + 2] - p.z;
    if (dx * dx + dy * dy + dz * dz <= R2) state.focus[f] = val;
  }
  state.focusDirty = true;
}
renderer.domElement.addEventListener('pointerdown', (ev) => {
  if (!state.brushOn || ev.button !== 0 || !printMesh) return;
  painting = true; renderer.domElement.setPointerCapture(ev.pointerId);
  const h = hitAt(ev);
  if (h) { paintAt(h); refreshColors(); }
});
renderer.domElement.addEventListener('pointermove', (ev) => {
  if (!state.brushOn) return;
  const h = hitAt(ev);
  if (!h) { brushRing.visible = false; return; }
  brushRing.visible = true;
  brushRing.position.copy(h.point);
  brushRing.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), h.face.normal.clone().transformDirection(printMesh.matrixWorld));
  brushRing.scale.setScalar(state.brushSize);
  if (painting) { paintAt(h); refreshColors(); }
});
renderer.domElement.addEventListener('pointerup', () => { if (painting) { painting = false; updateFocusInfo(); } });
function updateFocusInfo() {
  const n = state.focus ? state.focus.reduce((a, b) => a + b, 0) : 0;
  $('focusInfo').textContent = n ? tn('priority: {n} triangles', n) : t('whole model');
  updateDirty();
}

// ---------------------------------------------------------------- settings
// kind: bool (select 0/1), int, float, str, auto (empty = automatic -> 0)
const SETTINGS = {
  toolheads: 'int', capacity: 'int', samples: 'int', source: 'str', front_only: 'bool', longest_side: 'auto',
  max_triangles: 'int', repair: 'str', bake_overlays: 'bool', color_threshold: 'int', texture_optimization: 'bool', lock_colors: 'bool',
  cmyk_optimized: 'bool', precise: 'bool', dual_wall: 'bool', max_recipe_len: 'int', max_run: 'int', backing: 'float',
  base: 'str', base_size: 'int', brightness: 'int', saturation: 'int', rainbow: 'bool', rainbow_v: 'float', rainbow_h: 'float', rainbow_ccw: 'bool',
  rainbow_offset: 'float', rainbow_white: 'float',
  layer_height: 'float', variable_layer: 'float', detailed_top: 'float', colored_walls: 'int', take_interior: 'bool',
  interweave: 'int', vol: 'bool', subdivide_mix: 'bool', supports: 'bool', rot_x: 'auto', rot_y: 'auto', rot_z: 'auto', z_offset: 'auto',
};
const ANALYSIS = ['toolheads', 'capacity', 'samples', 'source', 'front_only', 'longest_side', 'max_triangles', 'repair', 'bake_overlays',
  'color_threshold', 'texture_optimization', 'lock_colors', 'cmyk_optimized', 'precise', 'dual_wall', 'max_recipe_len', 'max_run', 'backing',
  'base', 'base_size', 'brightness', 'saturation', 'rainbow', 'rainbow_v', 'rainbow_h', 'rainbow_ccw', 'rainbow_offset', 'rainbow_white'];
const EXPORT = Object.keys(SETTINGS).filter((k) => !ANALYSIS.includes(k));
const STORE = 'irodori.settings';
// per model: a base chosen once must not land on the next project
const UNSAVED = ['base', 'base_size'];

function readSettings() {
  const s = {};
  for (const [k, kind] of Object.entries(SETTINGS)) {
    const v = $('s_' + k).value;
    s[k] = kind === 'bool' ? v === '1' : kind === 'str' ? v : kind === 'auto' ? (v === '' ? 0 : Number(v)) : Number(v);
  }
  s.rotate = ['rot_x', 'rot_y', 'rot_z'].some((k) => $('s_' + k).value !== '');
  if (s.capacity < 0) s.capacity = s.toolheads;
  s.opaque = state.opaque;
  return s;
}
const pick = (s, keys) => Object.fromEntries(keys.concat(['rotate', 'opaque']).filter((k) => k in s).map((k) => [k, s[k]]));
function saveSettings() {
  const raw = Object.fromEntries(Object.keys(SETTINGS).filter((k) => !UNSAVED.includes(k)).map((k) => [k, $('s_' + k).value]));
  raw._opaque = state.opaque;
  raw._v = 4;
  try { localStorage.setItem(STORE, JSON.stringify(raw)); } catch { /* storage unavailable */ }
}
function loadSaved() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORE) || '{}');
    // v2: library search on by default, it is where the accuracy comes from
    if (!(raw._v >= 2)) delete raw.cmyk_optimized;
    // v3: the old default weave level 1 painted 1.8 mm confetti
    if (!(raw._v >= 3) && raw.interweave === '1') delete raw.interweave;
    // v4: 0.08 mm became the default, and thin layers under flat tops no longer apply to it
    if (!(raw._v >= 4)) { delete raw.layer_height; delete raw.detailed_top; }
    for (const [k, v] of Object.entries(raw)) if ($('s_' + k) && !UNSAVED.includes(k)) $('s_' + k).value = v;
    if (raw._opaque) { state.opaque = true; for (const b of $('matMode').children) b.classList.toggle('on', b.dataset.m === 'opaque'); }
  } catch { /* storage unavailable */ }
}
function syncLabels() {
  const s = readSettings();
  $('ctVal').textContent = s.color_threshold;
  $('iwVal').textContent = s.interweave;
  $('brVal').textContent = `${s.brightness}%`;
  $('saVal').textContent = `${s.saturation}%`;
  $('rvVal').textContent = s.rainbow_v; $('rhVal').textContent = s.rainbow_h;
  $('roVal').textContent = s.rainbow_offset; $('rwVal').textContent = s.rainbow_white;
  $('rainbowBox').hidden = !s.rainbow;
  $('capNoDither').textContent = tn('only {n} spools', s.toolheads);
  $('rotState').textContent = s.rotate ? `X ${s.rot_x}° · Y ${s.rot_y}° · Z ${s.rot_z}°` : t('Orientation from the file');
  $('zState').textContent = s.z_offset < 0 ? t('{n} mm below the bed (cut off)', { n: fmt(-s.z_offset, 2) }) : t('Bottom on the bed');
  renderBases();
  // Orca cannot combine these layer schedules
  const locked = s.vol || s.subdivide_mix;
  $('s_variable_layer').disabled = $('s_detailed_top').disabled = locked;
  $('s_subdivide_mix').disabled = s.vol;
  $('s_vol').disabled = s.layer_height < 0.06;
}
function onSetting(k) {
  if (k === 'vol' && $('s_vol').value === '1') $('s_subdivide_mix').value = '0';
  if (k === 'rainbow' && $('s_rainbow').value === '1') $('s_take_interior').value = '1';
  if (k === 'layer_height' && Number($('s_layer_height').value) < 0.06) $('s_vol').value = '0';
  syncLabels();
  saveSettings();
  if (['rot_x', 'rot_y', 'rot_z', 'z_offset'].includes(k)) applyPose();
  if (k.startsWith('rainbow') || k === 'brightness' || k === 'saturation') refreshColors();
  if (k === 'variable_layer' || k === 'layer_height') renderRows();
  paintJobs();
  updateDirty();
}
loadSaved();
syncLabels();
for (const k of Object.keys(SETTINGS)) $('s_' + k).addEventListener('input', () => onSetting(k));
$('rotAuto').addEventListener('click', () => { for (const a of ['rot_x', 'rot_y', 'rot_z']) $('s_' + a).value = ''; onSetting('rot_x'); });
$('zAuto').addEventListener('click', () => { $('s_z_offset').value = ''; onSetting('z_offset'); });
$('rainbowReset').addEventListener('click', () => {
  for (const [k, v] of [['rainbow_v', 1], ['rainbow_h', 1], ['rainbow_ccw', 0], ['rainbow_offset', 0], ['rainbow_white', 0]]) $('s_' + k).value = v;
  onSetting('rainbow_v');
});
document.querySelectorAll('[data-reset]').forEach((b) => b.addEventListener('click', () => { $(b.dataset.reset).value = 0; onSetting(b.dataset.reset.slice(2)); }));

function analysisDirty() {
  if (!state.analysed) return false;
  const s = readSettings(), a = state.analysed;
  for (const k of ANALYSIS) {
    if (k.startsWith('rainbow_') && !s.rainbow && !a.rainbow) continue;
    if (typeof s[k] === 'number' ? Math.abs(s[k] - a[k]) > 1e-6 : s[k] !== a[k]) return true;
  }
  if (s.dual_wall && (s.colored_walls !== a.colored_walls || s.opaque !== a.opaque)) return true;
  return false;
}
function updateDirty() {
  const dirty = !!state.sid && !state.newFiles && (analysisDirty() || state.focusDirty || state.calDirty);
  $('btnReanalyze').hidden = !dirty;
  const btn = $('btnAnalyze');
  if (state.busy) { btn.disabled = true; return; }
  if (state.sid && !state.newFiles) { btn.textContent = t('Recalculate'); btn.disabled = !dirty; }
  else { btn.textContent = t('Analyze'); btn.disabled = !state.files.length || !depsReady(); }
}

// display base: a style chip adds it, clicking it again cycles the size
function renderBases() {
  const lib = state.library; if (!lib) return;
  const cur = $('s_base').value, size = Number($('s_base_size').value);
  $('baseList').innerHTML = lib.bases.map((b) => `<button type="button" data-base="${b.id}" class="${b.id === cur ? 'on' : ''}"
    title="${b.id === cur ? t('Click to change the size') : esc(t('Base "{name}"', { name: t(b.name) }))}">${esc(t(b.name))}${b.id === cur ? ` · ${esc(t(lib.base_sizes[size]))}` : ''}</button>`).join('');
  $('baseRemove').hidden = !cur;
}
$('baseList').addEventListener('click', (e) => {
  const id = e.target.dataset.base; if (!id) return;
  if ($('s_base').value === id) $('s_base_size').value = (Number($('s_base_size').value) + 1) % state.library.base_sizes.length;
  else { $('s_base').value = id; $('s_base_size').value = 0; }
  onSetting('base');
});
$('baseRemove').addEventListener('click', () => { $('s_base').value = ''; $('s_base_size').value = 0; onSetting('base'); });

// ---------------------------------------------------------------- files and texture dependencies
const drop = $('drop');
const MODEL_RE = /\.(glb|3mf|fbx|obj)$/i;
const IMG_RE = /\.(png|jpe?g|tga|tiff?|webp|bmp)$/i;
const base = (p) => p.replace(/\\/g, '/').split('/').pop();
let deps = { need: [], kind: '' };
$('file').addEventListener('change', (e) => setFiles([...e.target.files]));
drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
drop.addEventListener('dragleave', () => drop.classList.remove('over'));
drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); setFiles([...e.dataTransfer.files]); });
$('depFiles').addEventListener('change', (e) => addFiles([...e.target.files]));
$('depFolder').addEventListener('change', (e) => addFiles([...e.target.files].filter((f) => IMG_RE.test(f.name) || /\.mtl$/i.test(f.name))));
$('depAllow').addEventListener('change', () => { renderDeps(); updateDirty(); });

async function setFiles(files) {
  const main = files.find((f) => MODEL_RE.test(f.name));
  if (!main) { setStatus('Choose a GLB, FBX, OBJ or 3MF file', false, true); return; }
  state.files = [main, ...files.filter((f) => f !== main)];
  state.newFiles = true;
  $('depAllow').checked = false;
  $('s_base').value = ''; $('s_base_size').value = 0; renderBases();
  await scanDeps();
  showFile();
  setStatus('Model ready.');
  updateDirty();
}
async function addFiles(more) {
  const names = new Set(state.files.map((f) => f.name));
  state.files.push(...more.filter((f) => !names.has(f.name)));
  state.newFiles = true;
  await scanDeps();
  showFile();
  updateDirty();
}
// referenced textures: names inside a (binary or ASCII) FBX, mtllib + map_* lines for OBJ
async function scanDeps() {
  const main = state.files[0], name = main.name.toLowerCase();
  deps = { need: [], kind: '' };
  if (name.endsWith('.fbx')) {
    const text = new TextDecoder('latin1').decode(await main.arrayBuffer());
    const found = text.match(/[\w\-. ()\\/:]+\.(png|jpe?g|tga|tiff?|webp|bmp)(?=[^\w.]|$)/gi) || [];
    deps = { need: [...new Set(found.map((p) => base(p.trim())))].filter(Boolean), kind: 'fbx' };
  } else if (name.endsWith('.obj')) {
    const text = await main.text();
    const libs = [...text.matchAll(/^\s*mtllib\s+(.+?)\s*$/gm)].map((m) => base(m[1]));
    const need = [...libs];
    for (const lib of libs) {
      const f = state.files.find((x) => x.name === lib);
      if (!f) continue;
      const mtl = await f.text();
      for (const m of mtl.matchAll(/^\s*(?:map_\w+|bump|disp|decal|refl)\s+(.+?)\s*$/gm)) need.push(base(m[1].replace(/^(?:[-\d.]+\s+)+/, '').replace(/^(?:-\S+\s+\S+\s+)+/, '')));
    }
    deps = { need: [...new Set(need)], kind: 'obj' };
  }
  renderDeps();
}
const missing = () => deps.need.filter((n) => !state.files.some((f) => f.name.toLowerCase() === n.toLowerCase()));
const depsReady = () => !missing().length || $('depAllow').checked;
function renderDeps() {
  $('deps').hidden = !deps.need.length;
  const miss = new Set(missing());
  $('depList').innerHTML = deps.need.map((n) => `<li class="${miss.has(n) ? 'miss' : 'ok'}">${esc(n)}</li>`).join('');
}
function showFile() {
  const main = state.files[0];
  const mb = state.files.reduce((a, f) => a + f.size, 0) / 1048576;
  drop.classList.add('has');
  $('dropTitle').textContent = main.name;
  $('dropSub').textContent = `${fmt(mb)} MB${state.files.length > 1 ? ` · ${tn('{n} extra files', state.files.length - 1)}` : ''} · ${t('click to choose another')}`;
  $('fileInfo').textContent = `${main.name} · ${fmt(mb)} MiB`;
}

let lastStatus = ['', false, false];
function setStatus(msg, busy = false, err = false) {
  lastStatus = [msg, busy, err];
  const el = $('status');
  el.classList.toggle('err', err);
  el.innerHTML = (busy ? '<span class="spin"></span>' : '') + esc(t(msg));
}
function setProgress(p) {
  $('progBar').style.width = `${Math.round(100 * p)}%`;
  $('progPct').textContent = `${Math.round(100 * p)}%`;
}

// ---------------------------------------------------------------- analysis
let resultsTitle = ['Working…'];
function setResultsTitle(...args) {
  resultsTitle = args;
  $('resultsTitle').textContent = t(...args);
}
async function analyze() {
  if (state.sid && !state.newFiles) return reanalyze();
  if (!state.files.length) return;
  const fd = new FormData();
  for (const f of state.files) fd.append('files', f);
  fd.append('settings', JSON.stringify(readSettings()));
  setStatus('Sending the model to the local server…', true);
  state.busy = true; updateDirty();
  try {
    const r = await fetch('/api/analyze', { method: 'POST', body: fd });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const { session } = await r.json();
    Object.assign(state, { sid: session, version: -1, selected: -1, newFiles: false, rows: [], orig: {} });
    $('results').hidden = false;
    $('rows').innerHTML = '';
    setResultsTitle('Working…');
    poll();
  } catch (e) { setStatus(e.message, false, true); state.busy = false; updateDirty(); }
}
async function reanalyze() {
  const fd = new FormData();
  fd.append('settings', JSON.stringify(readSettings()));
  if (state.focus && state.focus.some((x) => x)) {
    const bits = new Uint8Array(Math.ceil(state.focus.length / 8));
    state.focus.forEach((v, i) => { if (v) bits[i >> 3] |= 1 << (i & 7); });
    let s = ''; for (let i = 0; i < bits.length; i += 0x8000) s += String.fromCharCode(...bits.subarray(i, i + 0x8000));
    fd.append('focus', btoa(s));
  }
  state.busy = true; updateDirty();
  const r = await fetch(`/api/reanalyze/${state.sid}`, { method: 'POST', body: fd });
  if (!r.ok) { setStatus((await r.json()).detail, false, true); state.busy = false; updateDirty(); return; }
  state.focusDirty = false; state.calDirty = false; state.orig = {};
  setResultsTitle('Recalculating…');
  poll();
}
$('btnAnalyze').addEventListener('click', analyze);
$('btnReanalyze').addEventListener('click', reanalyze);

async function poll() {
  const r = await fetch(`/api/status/${state.sid}`);
  if (!r.ok) { setStatus((await r.json()).detail, false, true); state.busy = false; updateDirty(); return; }
  const st = await r.json();
  setStatus(st.message, st.busy, !!st.error);
  setProgress(st.progress || 0);
  $('warnings').textContent = (state.warnings = st.warnings || []).map((w) => t(w)).join(' · ');
  if (st.version !== state.version && st.version > 0 && ['targets', 'search', 'rows', 'done'].includes(st.stage)) {
    state.version = st.version;
    const keep = state.focus;
    const [mb] = await Promise.all([fetch(`/api/mesh/${state.sid}.bin`).then((x) => x.arrayBuffer()), loadSource(state.sid)]);
    buildPrintMesh(mb);
    if (keep && keep.length === state.focus.length) state.focus.set(keep);
  }
  if (st.busy) { setTimeout(poll, 400); return; }
  state.busy = false;
  if (st.error) { setResultsTitle('That did not work'); updateDirty(); return; }
  if (st.stage === 'done') {
    state.rows = st.rows;
    state.analysed = st.settings;
    state.diag = st.diag;
    setResultsTitle('Results · {n}', { n: state.rows.length });
    renderBrands();
    mineIdx = null;
    renderMine();
    await selectRow(0);
    scheduleMine(0);
    loadRanking();
    if (state.view === 'source') $('viewMode').querySelector('[data-v=predicted]').click();
  }
  updateDirty();
}

// ---------------------------------------------------------------- result rows
const FAMILY_NOTE = {
  loaded: 'Spools from T1–T4',
  custom: 'Your colors',
};
function matchClass(m) { return m >= 80 ? 'good' : m >= 70 ? 'mid' : 'bad'; }
function timeCost(row, s) {
  const t = row.metrics.time; if (!t) return null;
  const k = s.variable_layer > s.layer_height ? s.layer_height / s.variable_layer : 1;
  return t.single * k + t.mixed;
}
function timeNote(row, s) {
  const share = row.metrics.time, vlh = s.variable_layer > s.layer_height;
  return [t('Mix on {n}% of the surface. Calculated from area × number of spools in the recipe.', { n: fmt(share.mixed_share) }),
    vlh ? t('Areas without mixing at {n} mm.', { n: s.variable_layer }) : '',
    t('1× = the fastest result of this analysis. Exact time: the G-code button.')].filter(Boolean).join(' ');
}
// virtual colours sorted from F1 toward Fn (mean slot of the recipe)
function stripOrder(entries) {
  const meanSlot = (p) => { const d = p.replace(/,/g, '').split('').map(Number); return d.reduce((a, b) => a + b, 0) / d.length; };
  return entries.filter((e) => !e.physical).map((e) => [meanSlot(e.pattern), e]).sort((a, b) => a[0] - b[0]).map((x) => x[1]);
}
function recipeText(pattern) {
  return pattern.split(',').map((g, gi) => {
    const c = {}; for (const d of g) c[d] = (c[d] || 0) + 1;
    const txt = Object.keys(c).sort().map((d) => `F${d} ${Math.round(100 * c[d] / g.length)}%`).join(' + ');
    return (pattern.includes(',') ? t(gi ? 'inner: ' : 'outer: ') : '') + t('{txt} (cycle {n})', { txt, n: g.length });
  }).join(' | ');
}
function capText(m) {
  return m.capacity === 0 ? t('as many as needed') : m.capacity <= 8 ? t('spools only') : t('up to {n}', { n: m.capacity });
}
function rowSub(row) {
  if (FAMILY_NOTE[row.family]) return t(FAMILY_NOTE[row.family]);
  if (row.family === 'search') return '';
  const m = row.metrics;
  return `CMY${m.calibrated ? ` · ${t('from your calibration')}` : ''}${m.dual ? ` · ${t('two wall recipes')}` : ''}`;
}

function renderRows() {
  const el = $('rows');
  if (!state.rows.length) { el.innerHTML = ''; return; }
  const s = readSettings();
  const costs = state.rows.map((r) => timeCost(r, s));
  const fastest = Math.min(...costs.filter((c) => c > 0));
  const heads = analysedHeads();
  const lib = new Map((state.library?.filaments || []).map((f) => [f.id, f]));
  el.innerHTML = state.rows.map((row, i) => {
    const m = row.metrics, n = row.filaments.length;
    const rel = costs[i] > 0 ? costs[i] / fastest : 1;
    const virt = stripOrder(row.entries);
    const orig = state.orig[i], job = visibleJob(row);
    const hasAlts = Object.values(row.alternatives || {}).some((a) => a.length > 1);
    const sw = row.filaments.map((f, k) => {
      const alts = (row.alternatives || {})[k];
      const altHtml = alts && alts.length > 1 ? `<div class="alts">${alts.map((id, a) => {
        const af = lib.get(id);
        return `<button type="button" data-alt="${k}" data-id="${id}" class="${id === f.id ? 'on' : ''}" title="${esc(`${'ABC'[a]}: ${af?.brand} ${af?.name}${af?.td ? ` · TD ${af.td} mm` : ''}`)}">${'ABC'[a]}</button>`;
      }).join('')}</div>` : hasAlts ? '<div class="alts"></div>' : '';
      return `<div class="swc" title="${esc(`${f.brand} ${f.name}${f.td ? ` · TD ${f.td} mm` : ''}`)}">${altHtml}
        <div class="chip" style="background:${f.hex}"></div>
        <b>F${k + 1} · ${esc(t(f.name))}</b>
        <div class="hexl"><span>${f.hex}</span>
          <label class="ico" title="${t('Edit {hex}', { hex: f.hex })}">◎<input type="color" value="${f.hex.toLowerCase()}" data-k="${k}"></label>
          ${orig ? `<button type="button" class="ico" data-reset-row="${i}" title="${t('Back to the suggested ones')}">↺</button>` : ''}
</div>
        ${m.usage ? `<span class="use" title="${t('Share of the spool in the colored surface')}">${fmt(m.usage[k])}%</span>` : ''}</div>`;
    }).join('');
    const adv = [['Mean ΔE2000 · median', `${m.mean_de} · ${m.median}`], ['ΔE p95 · worst', `${m.p95} · ${m.worst}`],
      ['Mixes', t('{used} ({cap}) of {total} possible', { used: m.virtual_used, cap: capText(m), total: fmt(m.candidates ?? 0, 0) })],
      ['Walls', t(m.dual ? 'two wall recipes' : 'one recipe')], ['Predicted colors', t(m.calibrated ? 'from card calibration' : 'Orca mixing model')],
      ...(state.diag?.exact_colors ? [['Colors in the file', state.diag.exact_colors]] : [])]
      .map(([a, b]) => `<dt>${t(a)}</dt><dd>${b ?? '—'}</dd>`).join('');
    const no3mf = heads > 4 ? ` disabled title="${t('Export works for 2–4 toolheads: project and G-code only for 2–4 filaments')}"` : '';
    return `<article class="rowcard${i === state.selected ? ' sel' : ''}" data-i="${i}" ${row.cmy_brand && state.brandsOff.has(row.cmy_brand) ? 'hidden' : ''}>
      <div class="rc-head"><div class="t"><h3>${esc(t(row.title))}</h3>${rowSub(row) ? `<p>${esc(rowSub(row))}</p>` : ''}</div>
        <div class="score"><span class="m ${matchClass(m.match)}" title="${t('Color match with the model')}">${fmt(m.match)}%</span>
          ${row.grouped ? `<small>+${tn('{n} similar', row.grouped)}</small>` : ''}
          ${m.time ? `<span class="time" title="${esc(timeNote(row, s))}">${t('time ≈{n}×', { n: fmt(rel, 1) })} <i>ⓘ</i></span>` : ''}</div>
        <button class="collapse" type="button" data-col-row="${i}" title="${t('Collapse')}">−</button></div>
      <div class="body">
        <div class="sw4" style="--n:${n}">${sw}</div>
        <div class="strip" title="${tn('{n} mixes', virt.length)}${t(', from F1 to F{n}', { n })}">${virt.map((e) => `<i style="background:${e.hex}" title="${e.hex} ${esc(recipeText(e.pattern))}"></i>`).join('')}</div>
        <div class="rc-foot"><details><summary>${t('Advanced')}</summary><dl class="dl">${adv}</dl></details>
          <div class="dlbtns"><button class="btn" type="button" data-act="glb" title="${t('Model with the predicted colors')}">GLB</button>
            <button class="btn" type="button" data-act="3mf" title="${t('Project for Snapmaker Orca')}"${no3mf}>3MF</button>
            <button class="btn" type="button" data-act="gcode" title="${t('Slice in Snapmaker Orca on this computer')}"${no3mf || (job?.busy ? ' disabled' : '')}>G-code</button></div></div>
        <div class="gstat${job?.err ? ' err' : ''}" id="gstat${i}">${job ? job.html() : ''}</div>
      </div></article>`;
  }).join('');
}

$('rows').addEventListener('click', async (e) => {
  const card = e.target.closest('.rowcard'); if (!card) return;
  const i = +card.dataset.i;
  const t = e.target;
  if (t.dataset.colRow !== undefined) { card.classList.toggle('collapsed'); t.textContent = card.classList.contains('collapsed') ? '+' : '−'; return; }
  if (t.dataset.act) return exportRow(i, t.dataset.act);
  if (t.dataset.alt !== undefined) return swapAlt(i, +t.dataset.alt, t.dataset.id);
  if (t.dataset.resetRow !== undefined) return resetRow(i);
  if (t.closest('a, label, details, input')) return;
  if (i !== state.selected) selectRow(i);
});
$('rows').addEventListener('change', (e) => {
  const inp = e.target; if (inp.type !== 'color') return;
  const i = +inp.closest('.rowcard').dataset.i, row = state.rows[i];
  const hexes = row.filaments.map((f) => f.hex);
  hexes[+inp.dataset.k] = inp.value.toUpperCase();
  recolor(i, hexes, true);
});

async function selectRow(i) {
  state.selected = i;
  renderRows();
  renderDiag();
  const buf = await fetch(`/api/labels/${state.sid}/${i}.bin`).then((x) => x.arrayBuffer());
  state.labels = new Uint16Array(buf);
  refreshColors();
}
async function recolor(i, hexes, replace, keepOrig = true, select = true) {
  setStatus('Calculating for your colors…', true);
  const fd = new FormData(); fd.append('colors', JSON.stringify(hexes)); fd.append('replace', replace ? '1' : '0');
  const r = await fetch(`/api/recolor/${state.sid}/${i}`, { method: 'POST', body: fd });
  if (!r.ok) { setStatus((await r.json()).detail, false, true); return null; }
  const j = await r.json();
  if (replace) { if (keepOrig && !state.orig[i]) state.orig[i] = state.rows[i]; state.rows[i] = j.row; }
  else state.rows.push(j.row);
  setStatus('Done');
  if (select) await selectRow(j.index); else renderRows();
  return j.index;
}
async function resetRow(i) {
  const orig = state.orig[i]; if (!orig) return;
  await recolor(i, orig.filaments.map((f) => f.hex), true);
  state.rows[i] = orig; delete state.orig[i];
  renderRows();
}
async function swapAlt(i, slot, id) {
  const row = state.rows[i];
  const ids = row.filaments.map((f) => f.id); ids[slot] = id;
  setStatus('Calculating with another spool…', true);
  const fd = new FormData(); fd.append('ids', ids.join(',')); fd.append('replace', String(i));
  const r = await fetch(`/api/set/${state.sid}`, { method: 'POST', body: fd });
  if (!r.ok) { setStatus((await r.json()).detail, false, true); return; }
  state.rows[i] = (await r.json()).row;
  setStatus('Done');
  await selectRow(i);
}

const exportSettings = () => JSON.stringify(pick(readSettings(), EXPORT));
// A finished G-code result only applies to the export settings it was sliced with.
function visibleJob(row) {
  const job = state.gjobs.get(row);
  return job && (job.busy || job.settings === exportSettings()) ? job : null;
}
function paintJob(row) {
  const at = state.rows.indexOf(row), el = $(`gstat${at}`), job = visibleJob(row);
  if (!el) return;
  el.innerHTML = job ? job.html() : '';
  el.classList.toggle('err', !!job?.err);
  document.querySelector(`.rowcard[data-i="${at}"] [data-act=gcode]`).disabled = !!job?.busy;
}
function paintJobs() {
  for (const row of state.rows) if (state.gjobs.has(row)) paintJob(row);
}
async function exportRow(i, kind) {
  if (kind === 'glb') { location.href = `/api/export/${state.sid}/${i}.glb`; return; }
  if (kind === '3mf') { location.href = `/api/export/${state.sid}/${i}.3mf?settings=${encodeURIComponent(exportSettings())}`; return; }
  const row = state.rows[i];
  const settings = exportSettings();
  const show = (html, { err = false, busy = false } = {}) => { state.gjobs.set(row, { html, err, busy, settings }); paintJob(row); };
  show(() => '', { busy: true });
  const fd = new FormData(); fd.append('settings', settings);
  const r = await fetch(`/api/gcode/${state.sid}/${i}`, { method: 'POST', body: fd });
  if (!r.ok) { const { detail } = await r.json(); show(() => esc(t(detail)), { err: true }); return; }
  const { job } = await r.json();
  const url = `/api/gcode/${state.sid}/job/${job}`;
  for (;;) {
    const j = await fetch(url).then((x) => x.json());
    if (j.busy) { show(() => `<span class="spin"></span>${esc(t(j.message))}`, { busy: true }); await new Promise((ok) => setTimeout(ok, 1500)); continue; }
    if (j.error) show(() => esc(t(j.message)), { err: true });
    else {
      const res = j.result, tools = Object.entries(res.tools).map(([k, v]) => `T${k}: ${v}`).join(', ');
      show(() => `${esc(t('Print time {time}, {grams} g, filament changes {changes} ({tools}).', { time: res.time, grams: res.grams, changes: res.changes, tools }))} <a href="${url}/gcode">${t('Download G-code')}</a> · <a href="${url}/3mf">${t('Download sliced 3MF')}</a>`);
    }
    break;
  }
}

// CMY brand filter: hides rows, no recalculation
function renderBrands() {
  const brands = [...new Set(state.rows.map((r) => r.cmy_brand).filter(Boolean))];
  $('brands').hidden = brands.length < 2;
  $('brandList').innerHTML = brands.map((b) => `<label><input type="checkbox" value="${esc(b)}"${state.brandsOff.has(b) ? '' : ' checked'}> ${esc(b)}</label>`).join('');
}
$('brandList').addEventListener('change', (e) => {
  if (e.target.checked) state.brandsOff.delete(e.target.value); else state.brandsOff.add(e.target.value);
  renderRows();
});

// diagnostics
function renderDiag() {
  const d = state.diag, s = state.analysed; if (!d) return;
  const row = state.rows[state.selected];
  const plate = d.plate.join(' × ');
  const items = [
    ['Dimensions', `${d.dimensions.map((x) => fmt(x)).join(' × ')} mm`],
    ['Bed', d.fits ? t('Fits in {size} mm', { size: plate }) : t('Does not fit in {size} mm: reduce the longest side', { size: plate })],
    ['Triangles', t('{a} (of {b})', { a: fmt(d.triangles, 0), b: fmt(d.source_triangles, 0) })],
    ['Mesh repair', `${t(d.repaired ? 'Rebuilt' : 'Unchanged')} · ${t(d.watertight ? 'watertight' : 'not watertight')}`],
    ['Color samples', fmt(d.samples, 0)],
    ['Format', d.source_kind.toUpperCase() + (d.exact_colors ? ` · ${t('{n} exact colors', { n: d.exact_colors })}` : '')],
    ['Thicken thin walls', t({ auto: 'When needed', on: 'Always', off: 'Never' }[s.repair])],
    ['Base', d.base ? d.base.split(' · ').map((x) => t(x)).join(' · ') : t('none')],
    ['Blend overlays', t(s.bake_overlays ? 'On' : 'Off')],
    ['Source colors', s.rainbow ? t('rainbow') : (s.brightness || s.saturation) ? t('brightness {a}%, saturation {b}%', { a: s.brightness, b: s.saturation }) : t('unchanged')],
    ['Filaments', t(s.opaque ? 'opaque' : 'translucent')],
    ['Sets in the ranking', d.ranked_sets],
    ['Analysis time', `${fmt(d.seconds)} s`],
  ];
  if (row) items.push(['Selected result', `${t(row.title)}: ${tn('{n} mixes', row.metrics.virtual_used)} · ${capText(row.metrics)}`]);
  $('diag').innerHTML = items.map(([a, b]) => `<dt>${t(a)}</dt><dd>${esc(b)}</dd>`).join('');
}

// ---------------------------------------------------------------- use my colours
const MINE = 'irodori.mine';
let mine = [], mineIdx = null, mineTimer = 0, mineRunning = false, mineAgain = false;
try { mine = JSON.parse(localStorage.getItem(MINE) || '[]').filter((h) => typeof h === 'string'); } catch { /* storage unavailable */ }
const loadedHexes = (n) => {
  const by = new Map((state.library?.filaments || []).map((f) => [f.id, f])), ids = inv.data.loaded.slice(0, n);
  return ids.length === n && ids.every((i) => by.has(i)) ? ids.map((i) => by.get(i).hex.toUpperCase()) : null;
};
function renderMine() {
  const n = analysedHeads();
  mine = Array.from({ length: n }, (_, k) => mine[k] || '');
  if (!mine.some(validHex)) mine = loadedHexes(n) || mine;
  $('mineSlots').style.setProperty('--n', n);
  $('mineSlots').innerHTML = mine.map((h, k) => `<div class="swc">
    <div class="chip empty" data-chip="${k}">${t('+ color')}<input type="color" data-k="${k}" value="${validHex(h) ? h.toLowerCase() : '#808080'}"></div>
    <b>F${k + 1}</b><input class="hex" data-k="${k}" list="libList" placeholder="#RRGGBB" value="${h}"></div>`).join('');
  mine.forEach((_, k) => mineSlot(k));
}
const validHex = (h) => /^#[0-9A-F]{6}$/.test(h);
function mineSlot(k) {
  const chip = document.querySelector(`[data-chip="${k}"]`), h = mine[k];
  chip.classList.toggle('empty', !validHex(h));
  chip.style.background = validHex(h) ? h : '';
  chip.firstChild.textContent = validHex(h) ? '' : t('+ color');
  $('mineMsg').textContent = t(mine.every(validHex) ? 'Recalculates by itself after every change.' : 'Fill in all the spools.');
}
// recompute the "My colors" row shortly after the last change, replacing it instead of piling up rows
// after an analysis the row is added quietly; colours equal to T1–T4 are already the "Loaded in the printer" row
function scheduleMine(delay = 700) {
  clearTimeout(mineTimer);
  if (!state.sid || state.busy || !mine.every(validHex)) return;
  const quiet = delay === 0;
  if (quiet && mineIdx === null && mine.join() === (loadedHexes(mine.length) || []).join()) return;
  mineTimer = setTimeout(() => runMine(!quiet), delay);
}
async function runMine(select) {
  if (mineRunning) { mineAgain = true; return; }
  mineRunning = true;
  const fresh = mineIdx === null;
  const idx = await recolor(fresh ? 0 : mineIdx, mine, !fresh, false, select);
  mineRunning = false;
  if (idx === null) return;
  mineIdx = idx;
  if (fresh && select) document.querySelector(`.rowcard[data-i="${idx}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  if (mineAgain) { mineAgain = false; runMine(select); }
}
$('mineSlots').addEventListener('input', (e) => {
  const k = e.target.dataset.k; if (k === undefined) return;
  if (e.target.type === 'color') {
    mine[k] = e.target.value.toUpperCase();
    document.querySelector(`#mineSlots input.hex[data-k="${k}"]`).value = mine[k];
  } else {
    const m = e.target.value.match(/#[0-9A-F]{6}/i);
    mine[k] = m ? m[0].toUpperCase() : '';
  }
  try { localStorage.setItem(MINE, JSON.stringify(mine)); } catch { /* storage unavailable */ }
  mineSlot(+k);
  scheduleMine();
});
document.querySelector('[data-col=mineCard]').addEventListener('click', (e) => {
  $('mineCard').classList.toggle('collapsed'); e.target.textContent = $('mineCard').classList.contains('collapsed') ? '+' : '−';
});

// ---------------------------------------------------------------- ranking
async function loadRanking() {
  const list = await fetch(`/api/ranking/${state.sid}`).then((r) => r.json());
  $('rankCard').hidden = !list.length;
  const built = new Set(state.rows.map((r) => r.filaments.map((f) => f.id).join(',')));
  $('ranking').innerHTML = list.map((r, i) => {
    const key = r.ids.join(','), title = r.filaments.map((f, k) => `F${k + 1} ${f.brand} ${f.name} ${f.hex}`).join('\n');
    return `<div class="rk" title="${esc(title)}"><span class="n">${i + 1}.</span>
      <span class="chips">${r.filaments.map((f) => `<i style="background:${f.hex}"></i>`).join('')}</span>
      <b>${fmt(r.match)}%</b>
      ${built.has(key) ? '<span class="muted">✓</span>' : `<button class="mini" type="button" data-ids="${key}">${t('Calculate')}</button>`}</div>`;
  }).join('');
}
$('ranking').addEventListener('click', async (e) => {
  const ids = e.target.dataset.ids; if (!ids) return;
  e.target.disabled = true;
  setStatus('Calculating the full palette for the set…', true);
  const fd = new FormData(); fd.append('ids', ids);
  const r = await fetch(`/api/set/${state.sid}`, { method: 'POST', body: fd });
  if (!r.ok) { setStatus((await r.json()).detail, false, true); e.target.disabled = false; return; }
  const j = await r.json();
  state.rows.push(j.row);
  setStatus('Done');
  await selectRow(j.index);
  loadRanking();
  document.querySelector(`.rowcard[data-i="${j.index}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
});

// ---------------------------------------------------------------- library, my filaments
async function loadLibrary() {
  state.library = await fetch('/api/library').then((r) => r.json());
  inv.data = state.library.inventory;
  $('libList').innerHTML = state.library.filaments.map((f) => `<option value="${esc(`${f.brand} — ${f.name} ${f.hex}`)}">`).join('');
  renderBases();
}
loadLibrary();
updateDirty();

const inv = { data: { owned: [], custom: [], loaded: [] }, owned: null, custom: [] };
const libById = () => new Map([...state.library.filaments.filter((f) => f.brand !== 'Custom'), ...inv.custom].map((f) => [f.id, f]));
$('btnInventory').addEventListener('click', () => {
  if (!state.library) return;
  inv.owned = new Set(inv.data.owned);
  inv.custom = inv.data.custom.map((c) => ({ ...c }));
  const brands = [...new Set(state.library.filaments.filter((f) => f.brand !== 'Custom').map((f) => f.brand))].sort();
  $('invBrand').innerHTML = `<option value="">${t('All brands')}</option>` + brands.map((b) => `<option>${esc(b)}</option>`).join('');
  $('invOnly').checked = inv.owned.size > 0;
  renderLoaded(inv.data.loaded);
  renderInv();
  $('dlgInventory').showModal();
});
function filamentOptions(sel) {
  const all = [...libById().values()];
  const opt = (f) => `<option value="${f.id}"${f.id === sel ? ' selected' : ''}>${esc(`${f.name} · ${f.brand} ${f.hex}`)}</option>`;
  const mineF = all.filter((f) => inv.owned.has(f.id));
  return '<option value="">—</option>' + (mineF.length ? `<optgroup label="${t('Mine')}">${mineF.map(opt).join('')}</optgroup>` : '') +
    `<optgroup label="${t('Library')}">${all.filter((f) => !inv.owned.has(f.id)).map(opt).join('')}</optgroup>`;
}
function renderLoaded(loaded) {
  const by = libById();
  $('invLoaded').innerHTML = [0, 1, 2, 3].map((k) => `<label>T${k + 1}<span class="dot" style="background:${by.get(loaded[k])?.hex || 'transparent'}"></span>
    <select data-k="${k}">${filamentOptions(loaded[k])}</select></label>`).join('');
}
const loadedNow = () => [...$('invLoaded').querySelectorAll('select')].map((x) => x.value);
$('invLoaded').addEventListener('change', (e) => {
  if (e.target.value) inv.owned.add(e.target.value);
  renderLoaded(loadedNow());
  renderInv();
});
function renderInv() {
  const q = $('invSearch').value.trim().toLowerCase(), brand = $('invBrand').value, only = $('invOnly').checked;
  const groups = new Map();
  for (const f of libById().values()) {
    if (brand && f.brand !== brand) continue;
    if (only && !inv.owned.has(f.id)) continue;
    if (q && !`${f.brand} ${f.name} ${f.hex}`.toLowerCase().includes(q)) continue;
    if (!groups.has(f.brand)) groups.set(f.brand, []);
    groups.get(f.brand).push(f);
  }
  $('invList').innerHTML = [...groups].map(([b, fs]) => `<h4>${esc(b)}</h4>` + fs.map((f) => `
    <label><input type="checkbox" data-id="${f.id}"${inv.owned.has(f.id) ? ' checked' : ''}><span class="dot" style="background:${f.hex}"></span>
      <span>${esc(f.name)}</span><span class="muted">${f.hex}${f.td ? ` · TD ${f.td}` : ''}</span>
      ${f.brand === 'Custom' ? `<button type="button" class="ghost x" data-del="${f.id}">${t('remove')}</button>` : '<span></span>'}</label>`).join('')).join('')
    || `<p class="muted" style="padding:8px 10px">${esc(t('No filaments. Untick "only mine" or change the search.'))}</p>`;
  $('invCount').textContent = `(${inv.owned.size})`;
}
$('invSearch').addEventListener('input', renderInv);
$('invBrand').addEventListener('change', renderInv);
$('invOnly').addEventListener('change', renderInv);
$('invList').addEventListener('change', (e) => {
  const id = e.target.dataset.id; if (!id) return;
  if (e.target.checked) inv.owned.add(id); else inv.owned.delete(id);
  $('invCount').textContent = `(${inv.owned.size})`;
  renderLoaded(loadedNow());
});
$('invList').addEventListener('click', (e) => {
  const id = e.target.dataset.del; if (!id) return;
  e.preventDefault();
  inv.custom = inv.custom.filter((c) => c.id !== id); inv.owned.delete(id);
  renderLoaded(loadedNow().map((x) => (x === id ? '' : x)));
  renderInv();
});
$('invAdd').addEventListener('click', () => {
  const hex = $('invHex').value.toUpperCase(), id = `custom-${hex.slice(1)}`;
  const td = $('invTd').value;
  inv.custom = inv.custom.filter((c) => c.id !== id);
  inv.custom.push({ id, brand: 'Custom', name: $('invName').value.trim() || hex, hex, td: td === '' ? null : Number(td) });
  inv.owned.add(id);
  $('invName').value = ''; $('invTd').value = '';
  renderLoaded(loadedNow());
  renderInv();
});
$('invSave').addEventListener('click', async (e) => {
  e.preventDefault();
  const loaded = loadedNow(), n = Math.min(4, readSettings().toolheads);
  const set = loaded.slice(0, n);
  if (set.some((x) => x) && set.some((x) => !x)) { alert(t('Set all of T1–T{n} or leave them empty.', { n })); return; }
  const body = { owned: [...inv.owned], custom: inv.custom, loaded: set.every((x) => x) ? loaded.filter((x) => x) : [] };
  const r = await fetch('/api/inventory', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) { alert(t((await r.json()).detail)); return; }
  const before = loadedHexes(analysedHeads());
  await loadLibrary();
  $('dlgInventory').close();
  updateDirty();
  if (state.sid && !state.busy) {
    const now = loadedHexes(analysedHeads());
    if (now && mine.join() === (before || []).join()) {
      mine = now;
      try { localStorage.setItem(MINE, JSON.stringify(mine)); } catch { /* storage unavailable */ }
      renderMine();
    }
    if (now) await loadedRow();
  }
});
// the "Loaded in the printer" row follows T1–T4 without a new analysis
async function loadedRow() {
  const ids = inv.data.loaded.slice(0, analysedHeads()), at = state.rows.findIndex((r) => r.family === 'loaded');
  if (at >= 0 && state.rows[at].filaments.map((f) => f.id).join() === ids.join()) return;
  setStatus('Calculating for the spools in T1–T4…', true);
  const fd = new FormData(); fd.append('ids', ids.join(',')); fd.append('loaded', '1'); fd.append('replace', String(at));
  const r = await fetch(`/api/set/${state.sid}`, { method: 'POST', body: fd });
  if (!r.ok) { setStatus((await r.json()).detail, false, true); return; }
  const j = await r.json();
  state.rows[j.index] = j.row;
  setStatus('Done');
  await selectRow(j.index);
}

// ---------------------------------------------------------------- calibration
const cal = { img: null, pts: [], layout: null, ids: null };
$('btnCalib').addEventListener('click', () => {
  const sel = $('calSet'); sel.innerHTML = '';
  const opts = (state.library?.presets || []).map((p) => ({ name: p.name, ids: p.filaments }));
  const row = state.rows[state.selected];
  if (inv.data.loaded.length) opts.unshift({ name: t('Loaded in the printer'), ids: inv.data.loaded });
  if (row && row.filaments.length <= 4) opts.unshift({ name: t('Selected palette: {names}', { names: row.filaments.map((f) => f.name).join(' + ') }), ids: row.filaments.map((f) => f.id) });
  opts.forEach((o) => { const op = document.createElement('option'); op.value = o.ids.join(','); op.textContent = o.name; sel.appendChild(op); });
  cal.pts = []; cal.img = null; drawCal(); $('calStatus').textContent = ''; $('calFit').disabled = true;
  $('dlgCalib').showModal();
});
$('calCard').addEventListener('click', () => {
  location.href = `/api/calibration/card.3mf?filaments=${encodeURIComponent($('calSet').value)}&layer_height=${readSettings().layer_height}`;
});
$('calPhoto').addEventListener('change', async (e) => {
  const f = e.target.files[0]; if (!f) return;
  cal.img = await createImageBitmap(f);
  cal.pts = [];
  cal.ids = $('calSet').value;
  cal.layout = await fetch(`/api/calibration/layout?filaments=${encodeURIComponent(cal.ids)}`).then((r) => r.json());
  drawCal();
  $('calStatus').textContent = t(CORNERS[0]);
});
const CORNERS = ['Click the top-left corner of the card front.', 'Click the top-right corner.', 'Click the bottom-right corner.', 'Click the bottom-left corner.'];
$('calCanvas').addEventListener('click', (e) => {
  if (!cal.img || cal.pts.length >= 4) return;
  const cv = $('calCanvas'), r = cv.getBoundingClientRect();
  cal.pts.push([(e.clientX - r.left) * cv.width / r.width, (e.clientY - r.top) * cv.height / r.height]);
  drawCal();
  if (cal.pts.length < 4) $('calStatus').textContent = t(CORNERS[cal.pts.length]);
  else { $('calStatus').textContent = t('Check that the dots sit in the middle of the tiles, then save the calibration.'); $('calFit').disabled = false; }
});
// projective map from unit card coordinates to image pixels (4 point pairs)
function homography(src, dst) {
  const A = [], b = [];
  for (let i = 0; i < 4; i++) {
    const [x, y] = src[i], [u, v] = dst[i];
    A.push([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.push(u);
    A.push([0, 0, 0, x, y, 1, -v * x, -v * y]); b.push(v);
  }
  for (let c = 0; c < 8; c++) {
    let p = c; for (let r = c + 1; r < 8; r++) if (Math.abs(A[r][c]) > Math.abs(A[p][c])) p = r;
    [A[c], A[p]] = [A[p], A[c]]; [b[c], b[p]] = [b[p], b[c]];
    for (let r = 0; r < 8; r++) {
      if (r === c) continue;
      const f = A[r][c] / A[c][c];
      for (let k = c; k < 8; k++) A[r][k] -= f * A[c][k];
      b[r] -= f * b[c];
    }
  }
  const h = b.map((v, i) => v / A[i][i]);
  return (x, y) => { const w = h[6] * x + h[7] * y + 1; return [(h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w]; };
}
function tileSamplePoints() {
  const L = cal.layout, H = homography([[0, 0], [1, 0], [1, 1], [0, 1]], cal.pts);
  return L.tiles.map((t) => {
    const [x0, z0, x1, z1] = t.rect, pts = [];
    for (let a = 0.25; a <= 0.751; a += 0.125) for (let c = 0.25; c <= 0.751; c += 0.125) {
      pts.push(H((x0 + (x1 - x0) * a) / L.width, 1 - (z0 + (z1 - z0) * c) / L.height));
    }
    return pts;
  });
}
function drawCal() {
  const cv = $('calCanvas'), ctx = cv.getContext('2d');
  if (!cal.img) { cv.width = 10; cv.height = 10; ctx.clearRect(0, 0, 10, 10); return; }
  const scale = Math.min(1, 1600 / cal.img.width);
  cv.width = cal.img.width * scale; cv.height = cal.img.height * scale;
  ctx.drawImage(cal.img, 0, 0, cv.width, cv.height);
  ctx.fillStyle = '#22d3ee';
  cal.pts.forEach(([x, y]) => { ctx.beginPath(); ctx.arc(x, y, 6, 0, 7); ctx.fill(); });
  if (cal.pts.length === 4) {
    ctx.fillStyle = '#fff';
    for (const pts of tileSamplePoints()) for (const [x, y] of pts) ctx.fillRect(x - 1, y - 1, 2, 2);
  }
}
$('calFit').addEventListener('click', async () => {
  const cv = $('calCanvas'), ctx = cv.getContext('2d');
  ctx.drawImage(cal.img, 0, 0, cv.width, cv.height);
  const data = ctx.getImageData(0, 0, cv.width, cv.height).data;
  const samples = tileSamplePoints().map((pts) => {
    const channels = [[], [], []];
    for (const [x, y] of pts) {
      const xi = Math.round(x), yi = Math.round(y);
      if (xi < 0 || yi < 0 || xi >= cv.width || yi >= cv.height) continue;
      const i = (yi * cv.width + xi) * 4;
      for (let k = 0; k < 3; k++) channels[k].push(data[i + k]);
    }
    // median, so a glint or a dark layer line does not shift the tile
    return channels.map((c) => c.sort((a, b) => a - b)[c.length >> 1] ?? 0);
  });
  drawCal();
  const fd = new FormData(); fd.append('filaments', cal.ids); fd.append('samples', JSON.stringify(samples));
  const r = await fetch('/api/calibration/fit', { method: 'POST', body: fd });
  const j = await r.json();
  if (!r.ok) { $('calStatus').textContent = t(j.detail); return; }
  const spools = j.names.map((n, i) => `${n} ${j.spool_de[i] ?? '–'}`).join(', ');
  const result = t('Error on check tiles: ΔE {a} (model alone: ΔE {b}). Spools against the catalog: {c}.', { a: j.heldout_de, b: j.model_de, c: spools });
  if (!j.saved) { $('calStatus').textContent = t('Not saved because the measurement is unreliable. {r} Take the photo in better light or check the corners.', { r: result }); return; }
  $('calStatus').textContent = t('Saved. {r} Use "Recalculate" to apply it.', { r: result });
  await loadLibrary();
  state.calDirty = true; updateDirty();
});

// ---------------------------------------------------------------- language
onLanguage(() => {
  setStatus(...lastStatus);
  $('warnings').textContent = (state.warnings || []).map((w) => t(w)).join(' · ');
  setResultsTitle(...resultsTitle);
  syncLabels();
  updateFocusInfo();
  if (state.files.length) showFile();
  renderRows();
  renderDiag();
  if (state.sid) { renderMine(); loadRanking(); }
});
