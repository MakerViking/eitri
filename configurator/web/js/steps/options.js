import { put } from '../api.js';
import { h, icon } from '../dom.js';
import { store, patchState } from '../store.js';
import { alertBox, segmented, switchControl, toast } from '../ui.js';

export const meta = {
  id: 'options', title: 'Options', hint: 'Your hardware',
  headline: 'Tell us what you *built*.',
  lead: 'Sensible defaults for a stock X-Plus 4. Change only what differs on your machine; every choice here becomes a value in the printer.cfg that is written for review.',
};

export function enter(ctx) {
  const schema = store.boot.options_schema;
  const values = { ...store.state.options };
  const rows = new Map();      // field id -> row element
  const errors = new Map();
  let timer = null;
  const fields = Object.fromEntries(schema.flatMap((g) => g.fields).map((f) => [f.id, f]));

  const save = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      errors.clear();
      try { await patchState({ options: values }); }
      catch (e) { Object.entries(e.data?.fields || {}).forEach(([k, v]) => errors.set(k, v)); if (!e.data?.fields) toast(e.message, 'bad'); }
      sync();
    }, 220);
  };

  const set = (id, v) => { values[id] = v; if (id === 'chamber_heater' && !v) values.chamber_ack = false; if (id === 'camera' && !v) values.go2rtc = false; sync(); save(); };

  function control(f) {
    if (f.type === 'bool') return switchControl({ checked: values[f.id], label: f.label, onChange: (v) => set(f.id, v) });
    if (f.type === 'choice') return segmented({ name: f.id, value: values[f.id], choices: f.choices, onChange: (v) => set(f.id, v) });
    const input = h('input', { class: `input ${f.mono ? 'mono' : ''}`, value: values[f.id], placeholder: '/dev/ttyS1', 'aria-label': f.label, spellcheck: false, autocomplete: 'off', onInput: () => set(f.id, input.value) });
    return h('div', { style: { minWidth: 'min(16rem, 100%)' } }, input);
  }

  function row(f) {
    const inline = f.type === 'bool' || f.type === 'text';
    const err = h('div', { class: 'field-error' });
    const el = h('div', { class: `opt ${inline ? 'inline' : 'stacked'} ${f.caution ? 'caution' : ''} ${f.depends_on ? 'dep' : ''}`, dataset: { id: f.id } },
      h('div', { class: 'opt-text' }, h('div', { class: 'opt-label' }, f.caution && icon('warn'), f.label), h('p', { class: 'help' }, f.help), err),
      h('div', { class: 'opt-ctl' }, control(f)));
    rows.set(f.id, { el, err });
    return el;
  }

  // Dependent rows appear only when their parent is on; the chamber heater explains itself when enabled.
  function sync() {
    for (const [id, { el, err }] of rows) {
      const f = fields[id];
      el.hidden = !!f.depends_on && !values[f.depends_on];
      err.textContent = errors.get(id) || '';
    }
    const cautionOn = values.chamber_heater;
    rows.get('chamber_ack').el.classList.toggle('needed', cautionOn && !values.chamber_ack);
    upstream.hidden = values.channel !== 'upstream';
    const invalid = values.chamber_heater && !values.chamber_ack;
    ctx.actions.set({ next: { disabled: !!invalid, onClick: async () => { clearTimeout(timer); await patchState({ options: values }).catch((e) => { toast(e.message, 'bad'); throw e; }); await ctx.advance(); } },
      note: invalid ? 'Acknowledge the chamber interlock to continue.' : null });
  }

  const upstream = alertBox('warn', h('p', null, h('strong', null, 'Upstream mode installs a compiler add-on (about 300 MB) on /data.'),
    ' You get plain git pull and any branch, and MCU firmware is built on this printer. Update bundles and one-click rollback no longer apply.'));

  const groups = schema.map((g) => h('section', { class: 'card opt-group' },
    h('div', { class: 'opt-group-head' }, h('h2', { class: 'card-title' }, g.title), h('p', { class: 'card-sub' }, g.blurb)),
    g.fields.map(row)));

  sync();
  return { nodes: [h('div', { class: 'stack' }, groups), upstream] };
}
