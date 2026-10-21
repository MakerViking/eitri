import { followJob, latestJob, post } from '../api.js';
import { h, icon, mount } from '../dom.js';
import { patchState, store } from '../store.js';
import { alertBox, makeConsole, toast } from '../ui.js';
import { BOOTLOADER, ROLE_LABEL } from './detect.js';

export const meta = {
  id: 'flash', title: 'Flash', hint: 'Update the MCUs',
  headline: 'Strike while the *iron* is hot.',
  lead: 'Both control boards get firmware that exactly matches your chosen host. Each board is backed up first, written over its serial link, then read back and verified.',
};

const PHASES = [
  ['identify', 'Check', 5], ['build', 'Image', 10], ['backup', 'Backup', 15],
  ['flash', 'Write', 60], ['verify', 'Verify', 10],
];
const ORDER = ['mainboard', 'toolhead'];

const newBoard = (role, rec = {}) => ({ role, port: rec.port, chip: rec.chip, bootloader: rec.bootloader, phases: {}, pct: {}, message: '', result: null, started: false });

function overall(b) {
  let total = 0;
  for (const [id, , w] of PHASES) {
    if (b.phases[id] === 'done' || b.phases[id] === 'skip') total += w;
    else if (b.phases[id] === 'start') total += w * ((b.pct[id] ?? 0) / 100);
  }
  return b.result?.ok ? 100 : Math.min(99, total);
}

export async function enter(ctx) {
  const detect = store.state.detect;
  const flavorName = store.boot.flavors.find((f) => f.id === store.state.flavor).name;
  const boards = new Map(ORDER.map((r) => [r, newBoard(r, (detect?.boards || []).find((b) => b.role === r))]));
  const konsole = makeConsole('Flasher output');
  const consoleBox = h('details', { class: 'disclosure', open: true, hidden: true }, h('summary', null, 'Flasher output'), konsole.root);
  const boardsHost = h('div', { class: 'grid two flash-boards' });
  const statusHost = h('div');
  const preHost = h('div');
  let running = false;
  let finished = false;
  let ack = false;
  let skipOpen = false;

  const saved = store.state.flash;
  const complete = !!saved && ORDER.every((r) => saved[r]?.ok);
  if (complete) ORDER.forEach((r) => { const b = boards.get(r); b.result = { ok: true, version: saved[r].version }; PHASES.forEach(([id]) => { b.phases[id] = 'done'; }); });

  // ------------------------------------------------------------ painting
  function boardCard(b) {
    const pct = overall(b);
    const failed = b.result && !b.result.ok;
    const [, blKind, , blLabel] = BOOTLOADER[b.bootloader] || BOOTLOADER.unknown;
    const state = b.result ? (b.result.ok ? 'ok' : b.result.skipped ? 'skipped' : 'fail') : b.started ? 'live' : 'idle';
    return h('div', { class: `card fboard ${state}` },
      h('div', { class: 'row' },
        h('span', { class: 'link-ico' }, icon(b.role === 'toolhead' ? 'flame' : 'cpu')),
        h('div', { class: 'grow' }, h('h3', { class: 'card-title' }, ROLE_LABEL[b.role]), h('p', { class: 'card-sub mono' }, `${b.port || '-'} · ${b.chip || '-'}`)),
        b.result ? h('span', { class: `chip ${b.result.ok ? 'ok' : b.result.skipped ? '' : 'bad'}` }, b.result.ok ? 'Verified' : b.result.skipped ? 'Skipped' : 'Failed')
          : b.started ? h('span', { class: 'chip ember' }, h('span', { class: 'dot live' }), 'Flashing') : h('span', { class: `chip ${blKind}` }, blLabel)),
      h('div', { class: 'pct-row' }, h('span', { class: 'display pct' }, String(Math.floor(pct)), h('small', null, '%')),
        h('span', { class: 'pct-msg' }, b.result?.ok ? `Now running ${b.result.version || 'the new firmware'}` : b.message || (b.started ? 'Working...' : 'Waiting'))),
      h('div', { class: `forge ${b.result?.ok ? 'done' : b.started ? '' : 'idle'}`, style: { '--p': String(pct) }, role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': Math.floor(pct), 'aria-label': `${ROLE_LABEL[b.role]} progress` }, h('span', { class: 'fill' })),
      h('ol', { class: 'pipe' }, PHASES.map(([id, label]) => {
        const st = b.phases[id] || 'pending';
        return h('li', { class: st, title: label }, h('span', { class: 'pn' }, st === 'done' ? icon('check') : st === 'fail' ? icon('x') : null), h('span', { class: 'pl' }, label));
      })),
      failed && b.result.message && h('div', { class: 'ferror' }, h('strong', null, b.result.message), b.result.recovery && h('p', null, b.result.recovery)));
  }

  function paint() {
    consoleBox.hidden = !running && !finished;
    mount(boardsHost, ORDER.map((r) => boardCard(boards.get(r))));
    const failed = [...boards.values()].find((b) => b.result && !b.result.ok && !b.result.skipped);
    const allOk = [...boards.values()].every((b) => b.result?.ok);
    mount(statusHost,
      allOk && alertBox('ok', h('p', null, h('strong', null, 'Both boards are running matching firmware.')), h('p', null, `${flavorName} host and MCU firmware are in step, and Klipper has been restarted.`)),
      failed && alertBox('bad', h('p', null, h('strong', null, `${ROLE_LABEL[failed.role]} did not finish.`)), h('p', null, 'Your printer is safe: the bootloader was not touched, so the board can be flashed again. Klipper has been restarted so you keep a working interface.')));
    paintPre();
    paintActions(allOk, failed);
  }

  function paintPre() {
    if (running || finished || complete) return mount(preHost);
    const ready = !!detect?.flash_ready;
    const items = [
      ['Both boards identified', ORDER.map((r) => (detect?.boards || []).find((b) => b.role === r)).filter(Boolean).map((b) => `${ROLE_LABEL[b.role]} on ${b.port}`).join(' and ')],
      ['Recovery path proven', ORDER.map((r) => (detect?.boards || []).find((b) => b.role === r)).filter(Boolean).map((b) => (BOOTLOADER[b.bootloader] || BOOTLOADER.unknown)[0]).join(' and ')],
      ['Current firmware saved first', 'Stored on /data before anything is replaced'],
      ['Klipper stopped, then restarted', 'So the serial ports are free while flashing'],
    ];
    const check = h('input', { type: 'checkbox', checked: ack, onChange: () => { ack = check.checked; paintActions(); } });
    mount(preHost, h('div', { class: 'card preflight' },
      h('h3', { class: 'card-title' }, 'Pre-flight'),
      h('ul', { class: 'pre-list' }, items.map(([t, d]) => h('li', null, h('span', { class: 'pre-ok' }, icon('check')), h('div', null, h('strong', null, t), h('span', { class: 'faint' }, d))))),
      h('label', { class: 'ack' }, check, h('span', null, `Flash ${flavorName} firmware to both boards. I will keep the printer powered and connected until this finishes.`))),
      !ready && alertBox('bad', h('p', null, 'Detection did not clear this printer for flashing. Go back and scan again.')));
  }

  function paintActions(allOk = [...boards.values()].every((b) => b.result?.ok), failed = null) {
    if (running) return ctx.actions.set({ back: { disabled: true }, next: { disabled: true, label: 'Flashing...', icon: null, onClick: null }, note: h('span', null, h('span', { class: 'dot live' }), 'Do not power off the printer.') });
    if (allOk) return ctx.actions.set({ back: { disabled: false }, next: { label: 'Continue', disabled: false, icon: 'arrowRight', onClick: null }, note: 'Both boards verified.' });
    if (failed || finished) return ctx.actions.set({ back: { disabled: false }, next: { label: 'Retry', icon: 'refresh', disabled: false, onClick: () => start(true) }, note: 'Nothing was lost. Retry flashes the remaining board(s).' });
    ctx.actions.set({ back: { disabled: false }, next: { label: 'Start flashing', icon: 'flame', disabled: !ack || !detect?.flash_ready, onClick: () => start(false) }, note: ack ? null : 'Tick the box above to enable flashing.' });
  }

  // ------------------------------------------------------------ events
  function onEvent(ev) {
    const b = boards.get(ev.board);
    switch (ev.type) {
      case 'phase': {
        // The CLI reports preflight once for the whole run (no board); it applies to every board.
        const targets = b ? [b] : [...boards.values()];
        for (const t of targets) {
          t.phases[ev.phase] = ev.status; t.started = !t.result; if (ev.status === 'done') t.pct[ev.phase] = 100;
          if (ev.message && b) t.message = ev.message;
        }
        break;
      }
      case 'progress': if (b) { b.started = true; b.pct[ev.phase] = ev.percent; b.phases[ev.phase] = b.phases[ev.phase] === 'done' ? 'done' : 'start'; if (ev.message) b.message = ev.message; } break;
      case 'board.result': if (b) { b.result = { ok: ev.ok, version: ev.version, message: ev.message, recovery: ev.recovery, skipped: ev.skipped }; b.started = false;
        if (!ev.ok && ev.phase && !ev.skipped) b.phases[ev.phase] = 'fail'; } break;
      case 'step': konsole.add({ t: ev.t, level: 'info', message: `${ev.title}${ev.status === 'start' ? '...' : ''}` }); break;
      case 'log': konsole.add(ev); break;
      case 'check': konsole.add({ t: ev.t, level: ev.status === 'block' ? 'error' : ev.status === 'warn' ? 'warn' : 'info', message: `${ev.id}: ${ev.detail}`, board: ev.board }); break;
      case 'prompt': konsole.add({ t: ev.t, level: 'warn', message: ev.message, board: ev.board }); toast(ev.message); break;
      default: break;
    }
    if (ev.type !== 'log') paint();
  }

  async function follow(jobId, since = 0) {
    running = true; window.__eitriBusy = true; paint();
    const end = await followJob(jobId, onEvent, since);
    running = false; window.__eitriBusy = false; finished = true;
    const flashState = await fetch('api/state').then((r) => r.json()).catch(() => null);
    if (flashState) store.state = flashState;
    paint();
  }

  async function start(retry) {
    const roles = retry ? ORDER.filter((r) => !boards.get(r).result?.ok) : ORDER;
    if (!retry) boards.forEach((b, r) => Object.assign(b, newBoard(r, b)));
    try {
      const { job } = await post('flash', { confirm: true, boards: roles });
      finished = false;
      follow(job.id);
    } catch (e) {
      toast(e.message, 'bad');
      if (e.data?.blockers) toast(e.data.blockers.map((b) => b.message).join(' '), 'bad');
    }
  }

  async function skipFlash() {
    await post('services/restart', { service: 'klipper' }).catch(() => {});
    await ctx.advance();
  }

  // ------------------------------------------------------------ resume / init
  const prev = await latestJob('flash');
  if (prev && prev.status === 'running') { prev.events.forEach(onEvent); follow(prev.id, prev.last_seq); }
  else paint();

  const skip = h('div', { class: 'skip-line' });
  const paintSkip = () => mount(skip, running || complete ? null : skipOpen
    ? h('span', { class: 'row wrap' }, h('span', { class: 'help' }, 'Only skip if both boards already run firmware built for this exact host version.'),
        h('button', { class: 'link-btn', type: 'button', onClick: skipFlash }, 'Yes, skip'), h('button', { class: 'link-btn', type: 'button', onClick: () => { skipOpen = false; paintSkip(); } }, 'Cancel'))
    : h('button', { class: 'link-btn', type: 'button', onClick: () => { skipOpen = true; paintSkip(); } }, 'Skip flashing (advanced)'));
  paintSkip();

  return {
    nodes: [preHost, boardsHost, statusHost, consoleBox, skip],
    destroy() { window.__eitriBusy = false; },
  };
}
