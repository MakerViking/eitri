import { h, icon } from '../dom.js';
import { store } from '../store.js';
import { alertBox, fmt } from '../ui.js';

export const meta = {
  id: 'welcome', title: 'Welcome', hint: 'What we are about to do',
  headline: 'Forge your ~X-Plus 4~ into something *open*.',
  lead: 'Eitri swaps the closed firmware for a clean, mainline Klipper stack: no patched forks, no closed binaries, and no touchscreen reflash. About fifteen minutes, start to finish.',
};

const PRINCIPLES = [
  ['shield', 'Mainline-clean', 'Klipper (or Kalico) stays an untouched clone. Everything Qidi-specific lives in small, separate modules you can read.'],
  ['undo', 'Reversible', 'Configs are backed up before every write, and each board’s current firmware is saved before it is replaced.'],
  ['screen', 'Stock screen, open driver', 'Klarity, our open screen daemon, drives the original display. The display itself is never reflashed.'],
];

const ROADMAP = [
  ['Network', 'Name the printer, check the connection', '1 min'],
  ['Detect', 'Find and identify both control boards', '1 min'],
  ['Choose', 'Klipper or Kalico, and your hardware', '2 min'],
  ['Flash', 'Update the mainboard and toolhead MCUs', '5 min'],
  ['Calibrate', 'Z-offset, input shaper, bed mesh', 'optional'],
];

export function enter(ctx) {
  const sys = store.boot.system;
  const spec = (k, v, sub) => h('div', null, h('dt', null, k), h('dd', null, v, sub && h('small', null, sub)));
  const plate = h('div', { class: 'plate' },
    h('dl', { class: 'specs' },
      spec('Board', sys.board.model, `${sys.board.soc}, ${sys.board.cores} cores`),
      spec('Memory', fmt.mb(sys.memory.total_mb), `${fmt.mb(sys.memory.available_mb)} free`),
      spec('Storage', sys.storage ? `${sys.storage.total_gb} GB` : '-', sys.storage ? `${sys.storage.free_gb} GB free` : ''),
      spec('System', sys.os.name, `Linux ${sys.os.kernel}`),
      spec('Uptime', fmt.uptime(sys.uptime_s), sys.cpu_temp_c != null ? `${sys.cpu_temp_c} °C SoC` : ''),
    ));

  const principles = h('div', { class: 'grid three' }, PRINCIPLES.map(([ic, title, text]) =>
    h('div', { class: 'card principle' }, h('span', { class: 'principle-icon' }, icon(ic)), h('h3', { class: 'card-title' }, title), h('p', { class: 'card-sub' }, text))));

  const road = h('ol', { class: 'roadmap' }, ROADMAP.map(([t, d, time], i) =>
    h('li', null, h('span', { class: 'rm-n mono' }, String(i + 1).padStart(2, '0')), h('span', { class: 'rm-t' }, t), h('span', { class: 'rm-d' }, d), h('span', { class: 'rm-time mono' }, time))));

  ctx.actions.set({ next: { label: 'Begin setup' }, note: 'About fifteen minutes. You can stop and resume at any step.' });
  return {
    nodes: [
      plate,
      principles,
      h('div', null, h('div', { class: 'section-label' }, 'The road ahead'), road),
      alertBox('info', h('p', null, h('strong', null, 'Before you start.'), ' Keep the printer powered and the Ethernet cable plugged in, and make sure nothing is printing. Flashing takes a few minutes and must not be interrupted.')),
    ],
  };
}
