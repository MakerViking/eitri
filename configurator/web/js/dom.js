// Tiny hyperscript helper. Children are always appended as text or nodes, never parsed
// as HTML, so nothing from the API (hostnames, SSIDs, log lines) can inject markup.

const SVG_NS = 'http://www.w3.org/2000/svg';

export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  apply(el, props);
  append(el, kids);
  return el;
}

function apply(el, props) {
  if (!props) return;
  for (const [k, v] of Object.entries(props)) {
    if (v === false || v == null) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k in el && k !== 'list' && typeof v !== 'object' && !k.includes('-')) { try { el[k] = v; } catch { el.setAttribute(k, v); } }
    else el.setAttribute(k, v === true ? '' : v);
  }
}

function append(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
}

// Static, trusted SVG markup only (icons, logo) - never interpolate API data into it.
export function svgFromString(markup) {
  const tpl = document.createElement('template');
  tpl.innerHTML = markup.trim();
  return tpl.content.firstElementChild;
}

export function clear(el) { while (el.firstChild) el.firstChild.remove(); return el; }
export function mount(el, ...kids) { clear(el); append(el, kids); return el; }

export function svgEl(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  for (const kid of kids.flat()) if (kid) el.append(kid);
  return el;
}

// ---- icons: 24px grid, stroke-only ----
const ICONS = {
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  warn: '<path d="M12 4l9 16H3z"/><path d="M12 10v4.5M12 17.6v.1"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.8v.1"/>',
  arrowRight: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  arrowLeft: '<path d="M19 12H5M11 6l-6 6 6 6"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M18.7 5.3l-1.6 1.6M6.9 17.1l-1.6 1.6"/>',
  moon: '<path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a6.800 6.800 0 0 0 10.500 10.500z"/>',
  wifi: '<path d="M2.500 9.200a14 14 0 0 1 19 0M5.600 12.500a9.500 9.500 0 0 1 12.800 0M8.800 15.700a5 5 0 0 1 6.400 0"/><path d="M12 19.200v.1"/>',
  ethernet: '<rect x="4" y="6" width="16" height="9" rx="2"/><path d="M8 15v3M12 15v3M16 15v3M8 6v3M12 6v3M16 6v3"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.500" y="9.500" width="5" height="5" rx=".8"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>',
  camera: '<path d="M4 8h3.500L9 5.500h6L16.500 8H20a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="3.500"/>',
  screen: '<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 21h8M12 17v4"/>',
  chip: '<rect x="7" y="7" width="10" height="10" rx="1.500"/><path d="M10 3.500v3.500M14 3.500v3.500M10 17v3.500M14 17v3.500M3.500 10H7M3.500 14H7M17 10h3.500M17 14h3.500"/>',
  flame: '<path d="M12 3c.5 3.500 5 5.500 5 10.500a5 5 0 0 1-10 0c0-2 1-3.200 2-4.200.2 1.500 1 2.200 1.800 2.500C10.600 8.800 10.800 5.800 12 3z"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.500-3.500M4 4v4h4M4 13a8 8 0 0 0 14.500 3.500M20 20v-4h-4"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  chevronDown: '<path d="M6 9l6 6 6-6"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  shield: '<path d="M12 3l8 3v5.500c0 4.500-3.200 8.200-8 9.500-4.800-1.300-8-5-8-9.500V6z"/><path d="M8.800 12l2.300 2.300 4.200-4.600"/>',
  undo: '<path d="M9 7L4 12l5 5"/><path d="M4 12h10a6 6 0 0 1 0 12"/>',
  layers: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5M3 17.500l9 5 9-5" opacity=".6"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  down: '<path d="M12 5v14M6 13l6 6 6-6"/>',
  wave: '<path d="M2 12c2-6 3-6 5 0s3 6 5 0 3-6 5 0 3 6 5 0"/>',
  grid: '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 9.300h16M4 14.700h16M9.300 4v16M14.700 4v16"/>',
  ruler: '<path d="M3 15L15 3l6 6L9 21z"/><path d="M7 11l2 2M10 8l2 2M13 5l2 2"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  printer: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M4 8h16M8 13h8"/>',
  eye: '<path d="M2 12s3.500-7 10-7 10 7 10 7-3.500 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  open: '<path d="M16 3h5v5M21 3l-8 8"/><path d="M19 13v6H5V5h6" />',
};

export function icon(name, cls = '') {
  const span = document.createElement('span');
  span.className = `ico ${cls}`.trim();
  span.setAttribute('aria-hidden', 'true');
  span.append(svgFromString(`<svg viewBox="0 0 24 24">${ICONS[name] || ''}</svg>`));
  return span;
}

// Elder Futhark runes, one per wizard step: drawn as straight strokes on a 24px grid.
export const RUNES = {
  welcome:   'M7 3v18M7 9l10-6M7 15l10-6',                       // fehu
  network:   'M5 4l14 16M19 4L5 20',                              // gebo
  detect:    'M7 3v18M7 7l10 5M7 12l10 5',                        // ansuz
  flavor:    'M5 4v16M19 4v16M5 4l14 16M19 4L5 20',               // dagaz
  options:   'M8 3l8 7-8 5 8 6',                                  // sowilo
  review:    'M6 3v18M6 3l10 5-10 5M10 13l7 8',                   // raidho
  flash:     'M16 4L7 12l9 8',                                    // kenaz: the torch
  calibrate: 'M12 21V3M6 9l6-6 6 6',                              // tiwaz
  done:      'M12 3l7 6-7 6-7-6zM9.500 12L5 21M14.500 12L19 21',  // othala
};

export function rune(step) {
  return svgFromString(`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${RUNES[step] || ''}"/></svg>`);
}

export const LOGO = `<svg viewBox="0 0 32 32" aria-hidden="true"><defs><linearGradient id="lg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffb347"/><stop offset=".55" stop-color="#ff6a1a"/><stop offset="1" stop-color="#b3260f"/></linearGradient></defs><path fill="url(#lg)" d="M2 12.6C5.2 12.3 7.2 11 10 11H28V14.6H22C22 17 21 18.6 19.8 19.6V21.2H24V26H8.2V21.2H12.4V19.6C11.2 18.6 10.4 17 10.4 14.6H9.2C6.2 14.6 3.6 13.8 2 12.6Z"/><path class="spark" fill="#ffd98a" d="M23.5 3.2L24.5 6.2 27.5 7.2 24.5 8.2 23.5 11 22.5 8.2 19.5 7.2 22.5 6.2Z"/></svg>`;
export const logo = () => svgFromString(LOGO);
