import { h, icon } from '../dom.js';
import { patchState, store } from '../store.js';
import { alertBox, toast } from '../ui.js';

export const meta = {
  id: 'flavor', title: 'Firmware', hint: 'Klipper or Kalico',
  headline: 'Pick your *flavor*.',
  lead: 'Both are installed as a pristine, unmodified clone. Eitri only adds small modules beside them, so either one updates with a plain git pull.',
};

export function enter(ctx) {
  let chosen = store.state.flavor;
  const cards = new Map();

  const card = (f) => {
    const input = h('input', { type: 'radio', name: 'flavor', value: f.id, checked: f.id === chosen, class: 'sr-only',
      onChange: async () => {
        chosen = f.id; paint();
        try { await patchState({ flavor: f.id }); } catch (e) { toast(e.message, 'bad'); }
      } });
    const el = h('label', { class: 'flavor-card', dataset: { flavor: f.id } },
      input,
      f.recommended && h('span', { class: 'ribbon' }, 'Recommended'),
      h('div', { class: 'flavor-top' }, h('span', { class: 'chip mono' }, f.tag), h('span', { class: 'flavor-check' }, icon('check'))),
      h('h2', { class: 'display flavor-name' }, f.name),
      h('p', { class: 'flavor-sum' }, f.summary),
      h('ul', { class: 'tradeoffs' },
        f.pros.map((t) => h('li', { class: 'pro' }, h('span', { class: 'mk' }, '+'), t)),
        f.cons.map((t) => h('li', { class: 'con' }, h('span', { class: 'mk' }, '−'), t))),
      h('div', { class: 'flavor-meta mono' }, h('span', null, f.version), f.commit && h('span', { class: 'faint' }, f.commit)));
    cards.set(f.id, el);
    return el;
  };

  function paint() {
    cards.forEach((el, id) => el.classList.toggle('selected', id === chosen));
  }

  const grid = h('div', { class: 'grid two flavor-grid', role: 'radiogroup', 'aria-label': 'Firmware flavor' }, store.boot.flavors.map(card));
  paint();
  return {
    nodes: [
      grid,
      alertBox('info', h('p', null, h('strong', null, 'You can change this later,'), ' but the host and MCU firmware are version-locked to each other. Switching flavor means flashing both boards again.'),
        h('p', { class: 'faint' }, 'Everything else in this wizard (config, Klarity, camera, calibration) is the same for both.')),
    ],
  };
}
