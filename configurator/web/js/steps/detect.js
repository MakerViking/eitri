import { followJob, latestJob, post } from '../api.js';
import { h, icon, mount } from '../dom.js';
import { refreshKlarity, store } from '../store.js';
import { alertBox, button, makeConsole, toast } from '../ui.js';

export const meta = {
  id: 'detect', title: 'Detect', hint: 'Printer and boards',
  headline: 'Meet the *boards*.',
  lead: 'Eitri talks to each serial port and asks who is on the other end. Which UART carries the mainboard and which the toolhead is established by what answers, never assumed.',
};

// [label, tone, recovery note, short label]
export const BOOTLOADER = {
  katapult: ['Katapult', 'ok', 'Recoverable over serial', 'Katapult'],
  'sdcard-serial': ['SD-card serial bootloader', 'ok', 'Recoverable over serial', 'SD-card serial'],
  'stm32-rom': ['ROM bootloader only', 'bad', 'Needs hardware access', 'ROM only'],
  klipper: ['Klipper only', 'warn', 'Recovery unproven', 'No bootloader'],
  unknown: ['Unidentified', 'warn', 'Recovery unproven', 'Unidentified'],
};
export const ROLE_LABEL = { mainboard: 'Mainboard', toolhead: 'Toolhead', unknown: 'Unknown board' };

const STAGES = [
  ['host', 'cpu', 'Host'],
  ['mcus', 'chip', 'Control boards'],
  ['camera', 'camera', 'Camera'],
  ['screen', 'screen', 'Screen'],
];

export async function enter(ctx) {
  const stage = Object.fromEntries(STAGES.map(([id]) => [id, { status: 'idle', detail: '' }]));
  const ports = new Map();        // port -> latest detect.port record, in arrival order
  let result = store.state.detect;
  let scanning = false;
  const konsole = makeConsole('Probe log');

  const stageHost = h('div', { class: 'stage-tiles' });
  const topoHost = h('div', { class: 'topo' });
  const alertHost = h('div', { class: 'stack' });

  const statusDot = (s) => ({ done: 'ok', warn: 'warn', fail: 'bad', start: 'live' }[s] || '');

  function paintStages() {
    mount(stageHost, STAGES.map(([id, ic, label]) => {
      const s = stage[id];
      return h('div', { class: `tile ${s.status}` },
        h('span', { class: 'tile-ico' }, icon(s.status === 'done' ? 'check' : s.status === 'warn' || s.status === 'fail' ? 'warn' : ic)),
        h('div', { class: 'grow' }, h('div', { class: 'tile-label' }, label), h('div', { class: 'tile-detail' }, s.detail || ({ idle: 'Waiting', start: 'Checking...' }[s.status] || ''))),
        h('span', { class: `dot ${statusDot(s.status)}` }));
    }));
  }

  function boardCard(rec) {
    if (!rec) return h('div', { class: 'card board skeleton' }, h('div', { class: 'shimmer', style: { height: '1.2rem', width: '40%' } }), h('div', { class: 'shimmer', style: { height: '.9rem', width: '70%', marginTop: '.8rem' } }));
    if (rec.state === 'silent') return h('div', { class: 'card board silent' }, h('div', { class: 'row' }, h('span', { class: 'link-ico' }, icon('wave')),
      h('div', { class: 'grow' }, h('h3', { class: 'card-title' }, 'No answer'), h('p', { class: 'card-sub mono' }, rec.port)), h('span', { class: 'chip' }, 'Silent')));
    const final = rec.bootloader !== undefined;           // live probe events carry no role yet
    const known = rec.role && rec.role !== 'unknown';
    const [blLabel, blKind, blNote] = BOOTLOADER[rec.bootloader] || BOOTLOADER.unknown;
    return h('div', { class: `card board ${final ? 'found' : 'probing'}` },
      h('div', { class: 'row' },
        h('span', { class: 'link-ico' }, icon(rec.role === 'toolhead' ? 'flame' : 'cpu')),
        h('div', { class: 'grow' }, h('h3', { class: 'card-title' }, known ? ROLE_LABEL[rec.role] : final ? 'Board, role unclear' : 'A board answered'),
          h('p', { class: 'card-sub mono' }, `${rec.port}${rec.baud ? ` @ ${rec.baud}` : ''}`)),
        final ? h('span', { class: `chip ${known ? 'ok' : 'warn'}` }, h('span', { class: `dot ${known ? 'ok' : 'warn'}` }), known ? 'Identified' : 'Unassigned') : h('span', { class: 'chip ember' }, h('span', { class: 'dot live' }), 'Answered')),
      h('dl', { class: 'board-facts' },
        h('div', null, h('dt', null, 'MCU'), h('dd', { class: 'mono' }, rec.chip || '-')),
        h('div', null, h('dt', null, 'Running'), h('dd', { class: 'mono' }, rec.version || '-')),
        final && h('div', null, h('dt', null, 'Bootloader'), h('dd', null, h('span', { class: `chip ${blKind}` }, blLabel))),
        final && h('div', null, h('dt', null, 'Recovery'), h('dd', { class: blKind === 'bad' ? 'field-error' : 'muted' }, blNote))),
      rec.evidence?.length ? h('p', { class: 'board-why' }, `Matched by: ${rec.evidence[0]}`) : null);
  }

  function paintTopo() {
    const recs = result ? [...result.boards, ...result.ports.filter((p) => p.state !== 'klipper' && p.state !== 'katapult')] : [...ports.values()];
    const rows = recs.length >= 2 ? recs : [0, 1].map((i) => recs[i]);
    const live = scanning;
    mount(topoHost,
      h('div', { class: 'card host' },
        h('span', { class: 'link-ico big' }, icon('cpu')),
        h('div', null, h('h3', { class: 'card-title' }, 'Host'), h('p', { class: 'card-sub' }, store.boot.system.board.model),
          h('p', { class: 'help' }, 'Linux, Klipper, Moonraker')),
        h('span', { class: 'host-glow', 'aria-hidden': 'true' })),
      h('div', { class: 'wires', style: { '--n': String(rows.length) } }, rows.map((rec) => h('div', { class: `wire ${rec?.state === 'found' && result ? 'on' : live ? 'scan' : ''}` },
        h('span', { class: 'wire-label mono' }, rec?.port || 'UART'), h('span', { class: 'wire-line' }, h('i'))))),
      h('div', { class: 'boards' }, rows.map(boardCard)));
  }

  function paintAlerts() {
    if (!result) return mount(alertHost);
    const warn = (result.warnings || []).map((w) => h('p', { class: 'faint' }, `${w.board ? w.board + ': ' : ''}${w.message}`));
    const parts = [];
    if (result.blockers.length) {
      parts.push(alertBox('bad', h('p', null, h('strong', null, 'Flashing is blocked.')),
        ...result.blockers.map((b) => h('div', null, h('p', null, b.message), b.remedy && h('p', { class: 'faint' }, b.remedy))),
        h('p', { class: 'faint' }, 'Nothing has been changed on the printer.')));
    } else {
      parts.push(alertBox('ok', h('p', null, h('strong', null, 'Both boards are identified, and the recovery path is checked.')),
        h('p', null, 'Their current firmware is backed up before anything is replaced.'), ...warn));
    }
    if (result.needs_assignment) parts.push(assignCard());
    mount(alertHost, ...parts);
  }

  function assignCard() {
    const live = (result.ports || []).filter((p) => p.state === 'klipper' || p.state === 'katapult').map((p) => p.port);
    const all = [...new Set([...live, ...result.boards.map((b) => b.port)])].filter(Boolean);
    const mk = (role, def) => h('select', { class: 'input mono', 'aria-label': `${ROLE_LABEL[role]} port`, id: `assign-${role}` }, all.map((p) => h('option', { value: p, selected: p === def }, p)));
    const main = mk('mainboard', store.state.assign?.mainboard || all[0]);
    const tool = mk('toolhead', store.state.assign?.toolhead || all[1] || all[0]);
    const go = button({ label: 'Use this mapping', kind: 'primary', size: 'sm', onClick: () => { if (main.value === tool.value) return toast('Pick two different ports.', 'bad'); scan({ mainboard: main.value, toolhead: tool.value }); } });
    return h('div', { class: 'card assign' },
      h('div', null, h('h3', { class: 'card-title' }, 'Which UART is which?'),
        h('p', { class: 'card-sub' }, 'Probing found boards, but nothing proves which one is the mainboard and which the toolhead (the answer differs between sources). Only assign this if you have checked, for example against your original printer.cfg or by tracing the toolhead cable. A wrong answer would flash the wrong firmware to a board.')),
      h('div', { class: 'assign-row' }, h('div', { class: 'field' }, h('label', { for: 'assign-mainboard' }, 'Mainboard'), main), h('div', { class: 'field' }, h('label', { for: 'assign-toolhead' }, 'Toolhead'), tool)),
      h('div', null, go));
  }

  function setActions() {
    const ready = !!result && result.flash_ready;
    ctx.actions.set({
      next: { disabled: scanning || !ready },
      note: scanning ? h('span', null, h('span', { class: 'dot live' }), 'Probing serial ports...') : result && !ready ? 'Resolve the issue above to continue.' : result ? 'Hardware confirmed.' : null,
    });
  }

  function onEvent(ev) {
    if (ev.type === 'detect.stage') { stage[ev.stage] = { status: ev.status, detail: ev.detail || '' }; paintStages(); }
    else if (ev.type === 'detect.port') { ports.set(ev.port, ev); paintTopo(); }
    else if (ev.type === 'log') konsole.add(ev);
  }

  async function attach(jobId, since = 0) {
    scanning = true; window.__eitriBusy = true; setActions(); paintTopo();
    const end = await followJob(jobId, onEvent, since);
    scanning = false; window.__eitriBusy = false;
    if (end.result?.boards) {
      result = end.result;
      store.state.detect = result;       // the server already persisted it
    } else toast(end.result?.error || 'Detection failed', 'bad');
    paintTopo(); paintAlerts(); setActions(); refreshKlarity();
  }

  async function scan(assign) {
    result = null; ports.clear(); konsole.clear();
    STAGES.forEach(([id]) => { stage[id] = { status: 'idle', detail: '' }; });
    paintStages(); paintAlerts();
    try {
      const { job } = await post('detect', assign ? { assign } : {});
      await attach(job.id);
    } catch (e) { toast(e.message, 'bad'); scanning = false; setActions(); }
  }

  // Resume: a scan already in flight (reload / second device), or a stored result.
  const running = await latestJob('detect');
  paintStages(); paintTopo(); paintAlerts();
  if (running && running.status === 'running') {
    running.events.forEach(onEvent);
    attach(running.id, running.last_seq);
  } else if (result) {
    STAGES.forEach(([id]) => { stage[id] = { status: 'done', detail: '' }; });
    stage.host.detail = result.host.model; stage.mcus.detail = `${result.boards.length} board(s) identified`;
    stage.camera = { status: result.camera.present ? 'done' : 'warn', detail: result.camera.detail || (result.camera.present ? 'present' : 'not found') };
    stage.screen = { status: result.screen.connected ? 'done' : 'warn', detail: result.screen.display || '' };
    paintStages(); paintTopo(); paintAlerts(); setActions();
  } else {
    scan();
  }
  setActions();

  const printer = h('div', { class: 'card printer-card' },
    h('span', { class: 'link-ico big' }, icon('printer')),
    h('div', { class: 'grow' }, h('h3', { class: 'card-title' }, 'QIDI X-Plus 4'), h('p', { class: 'card-sub' }, 'The only printer Eitri targets today. Detection below confirms the hardware matches.')),
    h('span', { class: 'chip ok' }, icon('check'), 'Selected'));

  const rescan = button({ label: 'Scan again', kind: '', size: 'sm', icon: 'refresh', onClick: () => { if (!scanning) scan(); } });
  const log = h('details', { class: 'disclosure' }, h('summary', null, 'Probe log'), konsole.root);

  return {
    nodes: [printer, stageHost, topoHost, alertHost, h('div', { class: 'row wrap detect-foot' }, rescan), log],
    destroy() { window.__eitriBusy = false; },
  };
}
