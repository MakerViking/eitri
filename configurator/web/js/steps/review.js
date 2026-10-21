import { followJob, post } from '../api.js';
import { h, icon, mount } from '../dom.js';
import { refreshKlarity, store } from '../store.js';
import { alertBox, makeConsole, toast } from '../ui.js';

export const meta = {
  id: 'review', title: 'Review', hint: 'What will change',
  headline: 'Here is what we will *write*.',
  lead: 'Nothing has been changed yet. Every file below is shown as a diff against what is on the printer now, and anything replaced is backed up first.',
};

const ACTION_ICON = { flash: 'chip', 'flavor-switch': 'layers', toolchain: 'wave', hostname: 'wifi' };
const STATUS = { new: ['New', 'ok'], modified: ['Changed', 'accent'], unchanged: ['Up to date', ''] };

function diffView(file) {
  if (!file.hunks.length) return h('p', { class: 'help', style: { padding: '1rem' } }, 'No differences.');
  return h('div', { class: 'diff', role: 'table', 'aria-label': `Changes to ${file.path}` }, file.hunks.map((hunk, i) =>
    h('div', { class: 'hunk' },
      i > 0 && h('div', { class: 'hunk-gap mono' }, `@@ line ${hunk.old_start} @@`),
      hunk.lines.map((l) => h('div', { class: `dl ${l.t === '+' ? 'add' : l.t === '-' ? 'del' : 'ctx'}`, role: 'row' },
        h('span', { class: 'no mono' }, l.a ?? ''), h('span', { class: 'no mono' }, l.b ?? ''),
        h('span', { class: 'sg mono' }, l.t === ' ' ? '' : l.t), h('code', { class: 'tx' }, l.s || ' '))))));
}

function fileCard(file, open) {
  const [label, kind] = STATUS[file.status];
  const body = h('div', { class: 'file-body' }, file.status === 'unchanged' ? null : diffView(file));
  const wrap = h('div', { class: `card file ${open ? 'open' : ''}`, dataset: { path: file.path } });
  const head = h('button', { class: 'file-head', type: 'button', 'aria-expanded': String(open), disabled: file.status === 'unchanged', onClick: () => {
    const o = !wrap.classList.contains('open'); wrap.classList.toggle('open', o); head.setAttribute('aria-expanded', String(o)); } },
    icon('file'), h('code', { class: 'file-path' }, file.path), h('span', { class: 'grow' }),
    file.added > 0 && h('span', { class: 'delta add mono' }, `+${file.added}`), file.removed > 0 && h('span', { class: 'delta del mono' }, `−${file.removed}`),
    h('span', { class: `chip ${kind}` }, label), file.status !== 'unchanged' && icon('chevronDown', 'caret'));
  wrap.append(head);
  if (file.foreign) wrap.append(h('div', { class: 'file-note' }, icon('warn'), h('span', null, 'This file was not written by Eitri. It will be replaced, and your original is kept in the backup. Calibration results saved by Klipper (the SAVE_CONFIG block) are carried over.')));
  wrap.append(body);
  return wrap;
}

export async function enter(ctx) {
  const pv = await post('preview');
  const changed = pv.files.filter((f) => f.status !== 'unchanged');
  const opts = store.state.options;
  const progress = h('div', { class: 'apply-steps', hidden: true });
  const konsole = makeConsole('Output');
  const outputBox = h('details', { class: 'disclosure', open: true, hidden: true }, h('summary', null, 'Output'), konsole.root);

  const actions = h('ul', { class: 'actions-list' }, pv.actions.map((a) =>
    h('li', null, h('span', { class: 'act-ico' }, icon(ACTION_ICON[a.kind] || 'chevron')), h('span', null, a.text))));

  const steps = new Map();
  function onEvent(ev) {
    if (ev.type === 'step') {
      let li = steps.get(ev.id);
      if (!li) { li = h('li', null); steps.set(ev.id, li); progress.append(li); }
      const st = ev.status;
      mount(li, h('span', { class: `dot ${st === 'done' ? 'ok' : st === 'fail' ? 'bad' : 'live'}` }), h('span', null, ev.title));
      li.className = st;
    } else if (ev.type === 'log') konsole.add(ev);
  }

  async function write() {
    progress.hidden = false; outputBox.hidden = false; window.__eitriBusy = true;
    ctx.actions.set({ back: { disabled: true }, note: 'Writing configuration...' });
    const { job } = await post('apply', { confirm: true });
    const end = await followJob(job.id, onEvent);
    window.__eitriBusy = false;
    if (!end.ok) {
      toast(end.result?.error || 'Writing the configuration failed', 'bad');
      ctx.actions.set({ back: { disabled: false }, note: 'Writing failed. See the output above.' });
      throw new Error('apply failed');
    }
    refreshKlarity();
    await ctx.advance();
  }

  const upToDate = changed.length === 0 && !!store.state.applied;
  ctx.actions.set({ next: { label: upToDate ? 'Continue' : 'Write configuration', icon: upToDate ? undefined : 'hammer', onClick: upToDate ? undefined : write },
    note: upToDate ? 'Configuration is already written.' : `${changed.length} file${changed.length === 1 ? '' : 's'} will be written.` });

  const firstOpen = changed[0]?.path;
  return {
    nodes: [
      (pv.notes || []).length > 0 && alertBox('warn', ...pv.notes.map((n) => h('p', null, n))),
      h('div', null, h('div', { class: 'section-label', style: { marginTop: 0 } }, 'Actions'), actions),
      h('div', null, h('div', { class: 'section-label', style: { marginTop: 0 } }, 'Files'), h('div', { class: 'stack files' }, pv.files.map((f) => fileCard(f, f.path === firstOpen)))),
      opts.chamber_heater && alertBox('warn', h('p', null, h('strong', null, 'Chamber heater enabled.'), ' It is gated by the bed/chamber interlock, and heater runaway protection stays active.')),
      progress,
      outputBox,
    ].filter(Boolean),
  };
}
