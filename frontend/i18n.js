// UI translation. English is the source text: a string is its own key, and frontend/i18n/<lang>.json
// holds the other languages. Keys with {name} placeholders also translate messages from the server.
export const LANGUAGES = { en: 'English', pl: 'Polski', es: 'Español', de: 'Deutsch', fr: 'Français', ru: 'Русский', ja: '日本語' };
// the app was called Irodori: carry saved settings over once
for (const key of ['settings', 'mine', 'lang']) {
  try {
    const old = localStorage.getItem(`irodori.${key}`);
    if (old !== null && localStorage.getItem(`kaicho.${key}`) === null) localStorage.setItem(`kaicho.${key}`, old);
    localStorage.removeItem(`irodori.${key}`);
  } catch { /* storage unavailable */ }
}
const STORE = 'kaicho.lang';
const SKIP = 'script,style,.kanji,.ja';
const ATTRS = ['title', 'placeholder', 'aria-label'];
let lang = 'en', dict = {}, patterns = [], rules = new Intl.PluralRules('en');
const listeners = [], textNodes = [], attrs = [], blocks = [];

const fill = (s, params) => (params ? s.replace(/\{(\w+)\}/g, (m, k) => (k in params ? params[k] : m)) : s);
const squeeze = (s) => s.replace(/\s+/g, ' ').trim();

function compile() {
  patterns = Object.keys(dict).filter((k) => typeof dict[k] === 'string' && k.includes('{')).map((k) => {
    const names = [...k.matchAll(/\{(\w+)\}/g)].map((m) => m[1]);
    const source = k.split(/\{\w+\}/).map((p) => p.replace(/[.*+?^$()|[\]\\{}]/g, '\\$&')).join('(.+?)');
    return { re: new RegExp(`^${source}$`), names, to: dict[k] };
  });
}
function fromPattern(s) {
  for (const { re, names, to } of patterns) {
    const m = s.match(re);
    if (m) return fill(to, Object.fromEntries(names.map((n, i) => [n, t(m[i + 1])])));
  }
  return null;
}
export function t(s, params) {
  if (s === null || s === undefined || s === '') return s ?? '';
  const known = dict[s];
  if (typeof known === 'string') return fill(known, params);
  if (params) return fill(s, params);
  return fromPattern(String(s)) ?? s;
}
export function tn(key, n, params = {}) {
  const forms = dict[key] || {};
  const text = forms[rules.select(n)] ?? forms.other ?? key;
  return fill(text, { n: num(n, 0), ...params });
}
export const num = (x, digits = 1) => Number(x).toLocaleString(lang, { maximumFractionDigits: digits });
export const currentLang = () => lang;
export const onLanguage = (fn) => listeners.push(fn);

function collect() {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (n.parentElement.closest(SKIP) || n.parentElement.closest('[data-i18n-html]') || !n.nodeValue.trim()) continue;
    textNodes.push({ node: n, source: n.nodeValue });
  }
  for (const a of ATTRS) for (const el of document.querySelectorAll(`[${a}]`)) attrs.push({ el, a, source: el.getAttribute(a) });
  for (const el of document.querySelectorAll('[data-i18n-html]')) blocks.push({ el, source: squeeze(el.innerHTML) });
}
function apply() {
  document.documentElement.lang = lang;
  for (const { node, source } of textNodes) {
    const key = source.trim();
    if (node.isConnected) node.nodeValue = source.replace(key, t(key));
  }
  for (const { el, a, source } of attrs) el.setAttribute(a, t(source));
  // own trusted strings from frontend/i18n, they carry <b> and <code>
  for (const { el, source } of blocks) el.innerHTML = t(source);
  const title = document.documentElement.dataset.title;
  if (title) document.title = t(title);
}
async function load(code) {
  const r = await fetch(`i18n/${code}.json`);
  dict = r.ok ? await r.json() : {};
  lang = code;
  rules = new Intl.PluralRules(code);
  compile();
}
function pickLanguage() {
  let saved = null;
  try { saved = localStorage.getItem(STORE); } catch { /* storage unavailable */ }
  return saved in LANGUAGES ? saved : 'en';
}
export async function setLanguage(code) {
  await load(code);
  try { localStorage.setItem(STORE, code); } catch { /* storage unavailable */ }
  apply();
  listeners.forEach((fn) => fn());
}
export async function initLanguage() {
  await load(pickLanguage());
  collect();
  apply();
  const select = document.getElementById('lang');
  if (!select) return;
  select.innerHTML = Object.entries(LANGUAGES).map(([k, v]) => `<option value="${k}" lang="${k}">${v}</option>`).join('');
  select.value = lang;
  select.addEventListener('change', () => setLanguage(select.value));
}
