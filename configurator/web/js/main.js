import { authEvents } from './api.js';
import { h, icon, logo, mount, rune, RUNES, svgFromString } from './dom.js';
import { gateOnDemand, showGate } from './gate.js';
import { load, patchState, refreshKlarity, store, subscribe } from './store.js';
import { toggleTheme } from './theme.js';
import { button, toast } from './ui.js';
import * as welcome from './steps/welcome.js';
import * as network from './steps/network.js';
import * as detect from './steps/detect.js';
import * as flavor from './steps/flavor.js';
import * as options from './steps/options.js';
import * as review from './steps/review.js';
import * as flash from './steps/flash.js';
import * as calibrate from './steps/calibrate.js';
import * as remote from './steps/remote.js';
import * as done from './steps/done.js';

const STEPS = [welcome, network, detect, flavor, options, review, flash, calibrate, remote, done];
const ids = STEPS.map((s) => s.meta.id);

let current = null;       // { id, destroy }
let actionCfg = {};
let shell = {};

// ---------------------------------------------------------------- shell
function klarityChip() {
  const k = store.klarity || {};
  const on = k.service === 'active';
  const kind = !on ? 'bad' : k.connected === false ? 'warn' : k.connected ? 'ok' : '';
  const text = !on ? 'screen daemon stopped' : k.connected ? 'screen online' : k.connected === false ? 'screen not responding' : 'running';
  return h('span', { class: `chip klarity-chip ${kind}`, title: k.display ? `${k.display}${k.version ? ' - Klarity ' + k.version : ''}` : 'Klarity screen daemon' },
    h('span', { class: `dot ${kind}` }), h('span', { class: 'k' }, 'Klarity'), h('span', null, text));
}

function themeButton() {
  return h('button', { class: 'icon-btn theme-toggle', type: 'button', 'aria-label': 'Toggle light and dark theme', title: 'Toggle theme', onClick: toggleTheme },
    icon('sun', 'sun'), icon('moon', 'moon'));
}

function brand(extra) {
  return h('a', { class: 'brand', href: '#/welcome', 'aria-label': 'Eitri' }, logo(),
    h('span', null, h('span', { class: 'brand-name' }, 'Eitri'), extra));
}

function buildShell() {
  const app = document.getElementById('app');
  const stepItems = STEPS.map((s, i) => {
    const btn = h('button', { class: 'step-btn', type: 'button', onClick: () => navigate(s.meta.id) },
      h('span', { class: 'rune' }, rune(s.meta.id)),
      h('span', null, h('span', { class: 'step-title' }, s.meta.title), h('span', { class: 'step-hint' }, s.meta.hint)));
    return h('li', { class: 'step-item', dataset: { step: s.meta.id } }, btn);
  });
  const line = h('div', { class: 'steps-line' });
  const klarityHost = h('div');
  const klarityHost2 = h('div');
  const meter = h('div', { class: 'meter', 'aria-hidden': 'true' }, STEPS.map(() => h('i')));
  const meterLabel = h('div', { class: 'meter-label' });
  const head = h('header', { class: 'step-head' });
  const body = h('div', { class: 'step-body' });
  const panel = h('section', { class: 'panel', 'aria-live': 'polite' }, head, body);
  const noteHost = h('div', { class: 'action-note' });
  const backHost = h('span');
  const nextHost = h('span', { style: { display: 'contents' } });

  const mockBadge = store.boot.app.mock ? h('span', { class: 'chip accent mono', title: 'Mock mode: fake printer hardware, nothing real is touched.' }, 'mock') : null;

  mount(app, h('div', { class: 'shell' },
    h('aside', { class: 'rail' },
      brand(h('span', { class: 'brand-sub' }, 'Open firmware')),
      h('ol', { class: 'steps', 'aria-label': 'Setup steps' }, line, stepItems),
      h('div', { class: 'rail-foot' },
        klarityHost,
        h('div', { class: 'rail-row' }, h('span', { class: 'version' }, `v${store.boot.app.version}`, mockBadge && ' '), mockBadge, themeButton()))),
    h('div', { class: 'stage' },
      h('div', { class: 'topbar' },
        h('div', { class: 'topbar-row' }, brand(), h('span', { class: 'topbar-spacer' }), klarityHost2, themeButton()),
        meter, meterLabel),
      h('div', { class: 'panel-wrap' }, panel),
      h('div', { class: 'actionbar' }, h('div', { class: 'actionbar-inner' }, backHost, noteHost, nextHost)))));

  shell = { stepItems, line, klarityHost, klarityHost2, meter, meterLabel, head, body, noteHost, backHost, nextHost };
  paintKlarity();
}

function paintKlarity() {
  if (!shell.klarityHost) return;
  mount(shell.klarityHost, klarityChip());
  mount(shell.klarityHost2, klarityChip());
}

function paintProgress(idx) {
  const furthest = Math.max(ids.indexOf(store.state.furthest), idx);
  shell.stepItems.forEach((li, i) => {
    li.classList.toggle('current', i === idx);
    li.classList.toggle('done', i < idx || (i <= furthest && i < idx));
    li.classList.toggle('reachable', i <= furthest);
    li.querySelector('.step-btn').disabled = i > furthest;
    if (i === idx) li.querySelector('.step-btn').setAttribute('aria-current', 'step'); else li.querySelector('.step-btn').removeAttribute('aria-current');
  });
  shell.line.style.setProperty('--step-progress', String(idx / (STEPS.length - 1)));
  [...shell.meter.children].forEach((el, i) => { el.className = i < idx ? 'on' : i === idx ? 'cur' : ''; });
  mount(shell.meterLabel, h('span', null, 'Step ', h('b', null, String(idx + 1).padStart(2, '0')), ` / ${String(STEPS.length).padStart(2, '0')}`), h('span', null, STEPS[idx].meta.title));
}

// ---------------------------------------------------------------- action bar
const ctx = {
  store,
  get index() { return ids.indexOf(current.id); },
  actions: {
    set(cfg) { actionCfg = { ...actionCfg, ...cfg }; paintActions(); },
  },
  async advance() {
    const nxt = ids[ctx.index + 1];
    if (!nxt) return;
    await patchState({ step: nxt });
    navigate(nxt);
  },
  back() { const prev = ids[ctx.index - 1]; if (prev) navigate(prev); },
  navigate,
  toast,
};

function paintActions() {
  const idx = ctx.index;
  const back = actionCfg.back || {};
  const next = actionCfg.next || {};
  mount(shell.backHost,
    idx > 0 && !back.hidden && button({ label: back.label || 'Back', kind: 'ghost', icon: 'arrowLeft', disabled: back.disabled, onClick: back.onClick || ctx.back }));
  const note = actionCfg.note;
  mount(shell.noteHost, note ? (typeof note === 'string' ? h('span', null, note) : note) : null);
  const last = idx === STEPS.length - 1;
  mount(shell.nextHost, next.hidden || (last && !next.onClick) ? null : (() => {
    const b = button({ label: next.label || 'Continue', kind: 'primary', iconAfter: next.icon === undefined ? 'arrowRight' : next.icon || undefined, disabled: next.disabled,
      onClick: async () => {
        if (!next.onClick) return ctx.advance();
        b.classList.add('busy');
        try { await next.onClick(); } finally { b.classList.remove('busy'); }
      } });
    if (next.busy) b.classList.add('busy');
    return b;
  })());
}

// ---------------------------------------------------------------- routing
async function navigate(id) {
  if (!ids.includes(id)) id = store.state.step;
  const idx = ids.indexOf(id);
  const furthest = ids.indexOf(store.state.furthest);
  if (idx > furthest) id = ids[furthest];
  if (location.hash !== `#/${id}`) { history.pushState(null, '', `#/${id}`); }
  await render(id);
}

async function render(id) {
  const idx = ids.indexOf(id);
  if (current?.destroy) current.destroy();
  current = { id, destroy: null };
  actionCfg = {};
  const mod = STEPS[idx];
  if (store.state.step !== id) patchState({ step: id }).catch(() => {});

  paintProgress(idx);
  mount(shell.head,
    svgFromString(`<svg class="ghost-rune" viewBox="0 0 24 24" aria-hidden="true"><path d="${RUNES[id]}"/></svg>`),
    h('span', { class: 'eyebrow' }, `Step ${String(idx + 1).padStart(2, '0')} · ${mod.meta.title}`),
    h('h1', { class: 'display' }, ...emphasise(mod.meta.headline)),
    mod.meta.lead && h('p', { class: 'lead' }, mod.meta.lead));
  mount(shell.body);
  const result = await mod.enter(ctx);
  // A step may retell its heading for what it found (the flash step on boards that keep their firmware).
  if (result?.headline) mount(shell.head.querySelector('h1'), ...emphasise(result.headline));
  if (result?.lead) shell.head.querySelector('.lead').textContent = result.lead;
  const nodes = Array.isArray(result?.nodes) ? result.nodes : result?.nodes ? [result.nodes] : [];
  current.destroy = result?.destroy || null;
  nodes.forEach((n, i) => { n.classList?.add('rise'); n.style?.setProperty('--i', String(i + 1)); });
  mount(shell.body, ...nodes);
  shell.head.classList.remove('rise'); void shell.head.offsetWidth; shell.head.classList.add('rise');
  paintActions();
  window.scrollTo({ top: 0 });
  document.title = `${mod.meta.title} · Eitri`;
}

// "Forge your *open* printer" -> italic accent on the starred words
function emphasise(text) {
  // *word* -> italic accent; ~words~ -> kept on one line
  return text.split(/(\*[^*]+\*|~[^~]+~)/).filter(Boolean).map((part) =>
    part.startsWith('*') ? h('em', null, part.slice(1, -1)) : part.startsWith('~') ? h('span', { style: { 'white-space': 'nowrap' } }, part.slice(1, -1)) : part);
}

// ---------------------------------------------------------------- boot
async function boot() {
  try {
    await load();
  } catch (err) {
    mount(document.getElementById('app'), h('div', { style: { padding: '3rem 1.5rem', maxWidth: '36rem', margin: '10vh auto' } },
      h('h1', { class: 'display', style: { fontSize: '3rem' } }, 'The configurator is ', h('em', null, 'unreachable')),
      h('p', { class: 'lead', style: { marginTop: '1rem' } }, err.message),
      h('p', { style: { marginTop: '1.5rem' } }, button({ label: 'Try again', kind: 'primary', onClick: () => location.reload() }))));
    return;
  }
  authEvents.addEventListener('gate', () => gateOnDemand());
  if (!store.boot.auth?.paired) { showGate(store.boot.auth || { pairing_open: true }); return; }
  buildShell();
  subscribe(paintKlarity);
  setInterval(() => { if (!document.hidden) refreshKlarity(); }, 8000);   // no polling from a background tab
  window.addEventListener('hashchange', () => {
    const id = location.hash.replace(/^#\//, '');
    if (id !== current?.id) navigate(id);
  });
  window.addEventListener('beforeunload', (e) => { if (window.__eitriBusy) { e.preventDefault(); e.returnValue = ''; } });
  const want = location.hash.replace(/^#\//, '');
  await navigate(ids.includes(want) ? want : store.state.step);
}

boot();
