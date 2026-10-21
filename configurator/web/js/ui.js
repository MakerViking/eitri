// Small shared UI pieces.
import { h, icon } from './dom.js';

export function toast(message, kind = '') {
  const t = h('div', { class: `toast ${kind}` }, message);
  document.getElementById('toasts').append(t);
  setTimeout(() => { t.style.transition = 'opacity .4s'; t.style.opacity = '0'; setTimeout(() => t.remove(), 450); }, kind === 'bad' ? 7000 : 3500);
}

export function alertBox(kind, ...kids) {
  const ic = { warn: 'warn', bad: 'warn', ok: 'check', info: 'info' }[kind] || 'info';
  return h('div', { class: `alert ${kind}`, role: kind === 'bad' ? 'alert' : undefined }, icon(ic), h('div', { class: 'grow' }, ...kids));
}

export function button({ label, kind = '', icon: ic, iconAfter, onClick, disabled, size = '', type = 'button' }) {
  const b = h('button', { class: `btn ${kind ? 'btn-' + kind : ''} ${size ? 'btn-' + size : ''}`.trim(), type, disabled, onClick },
    ic && icon(ic), label && h('span', null, label), iconAfter && icon(iconAfter));
  return b;
}

export async function withBusy(btn, fn) {
  btn.classList.add('busy');
  try { return await fn(); } finally { btn.classList.remove('busy'); }
}

export function switchControl({ checked, onChange, disabled, label }) {
  const input = h('input', { type: 'checkbox', checked, disabled, 'aria-label': label, onChange: () => onChange(input.checked) });
  return h('span', { class: 'switch' }, input, h('span', { class: 'track' }), h('span', { class: 'thumb' }));
}

export function segmented({ name, value, choices, onChange }) {
  return h('div', { class: 'seg', role: 'radiogroup' }, choices.map((c) => {
    const input = h('input', { type: 'radio', name, value: c.value, checked: c.value === value, onChange: () => onChange(c.value) });
    return h('label', null, input, h('span', null, c.label, c.help && h('small', null, c.help)));
  }));
}

export const fmt = {
  mb: (n) => (n == null ? '-' : n >= 1024 ? `${(n / 1024).toFixed(1)} GB` : `${n} MB`),
  uptime: (s) => { if (s == null) return '-'; const m = Math.floor(s / 60); return m < 60 ? `${m || '<1'} min` : `${Math.floor(m / 60)} h ${m % 60} min`; },
  time: (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false }),
};

export function statusDot(kind) { return h('span', { class: `dot ${kind || ''}` }); }

// Console helper: append timestamped lines, keep the view pinned to the newest unless the user scrolled up.
export function makeConsole(title = 'Console') {
  const body = h('div', { class: 'console-body', role: 'log', 'aria-live': 'off' });
  const count = h('span', { class: 'faint' }, '');
  const root = h('div', { class: 'console' }, h('div', { class: 'console-head' }, icon('wave'), h('span', { class: 'grow' }, title), count), body);
  let lines = 0;
  return {
    root,
    add({ t, level = 'info', message, board }) {
      const pinned = body.scrollTop + body.clientHeight >= body.scrollHeight - 24;
      body.append(h('div', { class: `ln ${level}` }, h('time', null, fmt.time(t || Date.now() / 1000)), board && h('span', { class: 'tag' }, board), h('span', null, message)));
      while (body.childElementCount > 600) body.firstChild.remove();
      if (pinned) body.scrollTop = body.scrollHeight;
      count.textContent = `${++lines} lines`;
    },
    clear() { body.replaceChildren(); lines = 0; count.textContent = ''; },
  };
}
