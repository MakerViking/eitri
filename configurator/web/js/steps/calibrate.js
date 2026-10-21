import { followJob, get, post } from '../api.js';
import { h, icon, mount, svgEl } from '../dom.js';
import { patchState, store } from '../store.js';
import { alertBox, button, makeConsole, toast, withBusy } from '../ui.js';

export const meta = {
  id: 'calibrate', title: 'Calibrate', hint: 'Dial it in',
  headline: 'Now, *temper* it.',
  lead: 'Three short guided routines. Each one is optional, can be done later from Fluidd, and only runs while the printer is idle.',
};

const TOOLS = [
  { id: 'zoffset', icon: 'ruler', title: 'Z-offset', blurb: 'Set the nozzle-to-bed distance with the paper test.' },
  { id: 'shaper', icon: 'wave', title: 'Input shaper', blurb: 'Measure resonances and cancel ringing at speed.' },
  { id: 'mesh', icon: 'grid', title: 'Bed mesh', blurb: 'Map the bed surface so the first layer stays level.' },
];
const NUDGES = [-1, -0.5, -0.1, -0.05, -0.01, 0.01, 0.05, 0.1, 0.5, 1];
const fmtNudge = (n) => `${n > 0 ? '+' : '−'}${Math.abs(n)}`;

// ---------------------------------------------------------------- shared run helper
function stepList() {
  const host = h('ol', { class: 'apply-steps', hidden: true });
  const items = new Map();
  return {
    host,
    event(ev) {
      if (ev.type !== 'step') return;
      host.hidden = false;
      let li = items.get(ev.id);
      if (!li) { li = h('li'); items.set(ev.id, li); host.append(li); }
      li.className = ev.status;
      mount(li, h('span', { class: `dot ${ev.status === 'done' ? 'ok' : ev.status === 'fail' ? 'bad' : 'live'}` }), h('span', null, ev.title));
    },
    reset() { items.clear(); host.replaceChildren(); host.hidden = true; },
  };
}

async function runJob(path, steps, konsole) {
  const { job } = await post(path);
  window.__eitriBusy = true;
  const end = await followJob(job.id, (ev) => { steps.event(ev); if (ev.type === 'log') konsole.add(ev); });
  window.__eitriBusy = false;
  if (end.ok) steps.reset();
  return end;
}

// ---------------------------------------------------------------- heat map
function lerp(a, b, t) { return a.map((v, i) => Math.round(v + (b[i] - v) * t)); }
function meshColor(t) {
  const lo = [79, 134, 176], mid = [233, 220, 198], hi = [255, 106, 26];
  return t < 0.5 ? lerp(lo, mid, t * 2) : lerp(mid, hi, (t - 0.5) * 2);
}

function heatmap(m) {
  const rows = m.matrix.length, cols = m.matrix[0].length;
  const { min, max } = m.stats;
  const span = max - min || 1;
  const cell = 52;
  const svg = svgEl('svg', { viewBox: `0 0 ${cols * cell} ${rows * cell}`, class: 'heatmap', role: 'img', 'aria-label': `Bed mesh, ${rows} by ${cols} points, range ${m.stats.range.toFixed(3)} millimetres` });
  m.matrix.forEach((row, y) => row.forEach((v, x) => {
    const [r, g, b] = meshColor((v - min) / span);
    const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    const ry = rows - 1 - y;         // Klipper's origin is front-left; draw it that way up
    const rect = svgEl('rect', { x: x * cell + 1.5, y: ry * cell + 1.5, width: cell - 3, height: cell - 3, rx: 6, fill: `rgb(${r},${g},${b})` });
    rect.style.setProperty('animation', `rise .6s var(--ease) both`);
    rect.style.setProperty('animation-delay', `${(x + y) * 28}ms`);
    const label = svgEl('text', { x: x * cell + cell / 2, y: ry * cell + cell / 2 + 4, 'text-anchor': 'middle', fill: lum > 0.6 ? '#2a1a0c' : '#fff', 'font-size': 11, 'font-family': 'var(--font-mono)' });
    label.textContent = v.toFixed(2);
    svg.append(rect, label);
  }));
  return svg;
}

// ---------------------------------------------------------------- tool panels
function zoffsetPanel(ctx, status, mark, refresh) {
  const steps = stepList();
  const konsole = makeConsole('Klipper output');
  const host = h('div', { class: 'stack' });
  let phase = status.manual_probe?.is_active ? 'active' : 'idle';     // idle | starting | active | accepted
  let z = status.manual_probe?.z_position ?? null;

  const nudge = async (d, btn) => {
    await withBusy(btn, async () => {
      try { z = (await post('calibrate/zoffset/nudge', { delta: d })).z; paint(); }
      catch (e) { toast(e.message, 'bad'); }
    });
  };
  const paint = () => {
    if (phase === 'idle' || phase === 'starting') {
      mount(host,
        h('ol', { class: 'howto' },
          h('li', null, 'Slide a sheet of ordinary printer paper under the nozzle.'),
          h('li', null, 'Eitri homes the printer, lifts the nozzle clear, and starts Klipper’s manual probe.'),
          h('li', null, 'Lower the nozzle in small steps until the paper drags slightly, then accept.')),
        button({ label: phase === 'starting' ? 'Preparing...' : 'Start', kind: 'primary', icon: 'flame', disabled: phase === 'starting', onClick: async (e) => {
          phase = 'starting'; paint();
          steps.reset(); konsole.clear();
          const end = await runJob('calibrate/zoffset/start', steps, konsole);
          if (!end.ok) { toast(end.result?.error || 'Could not start', 'bad'); phase = 'idle'; paint(); return; }
          z = end.result.z; phase = end.result.active ? 'active' : 'idle'; paint();
        } }),
        steps.host);
    } else if (phase === 'active') {
      mount(host,
        h('div', { class: 'zread' }, h('span', { class: 'zlabel mono' }, 'Nozzle height'), h('span', { class: 'zval display' }, z == null ? '-' : z.toFixed(3), h('small', null, 'mm'))),
        h('div', { class: 'nudge', role: 'group', 'aria-label': 'Move the nozzle' }, NUDGES.map((d) => {
          const b = h('button', { class: `btn nudge-btn ${d < 0 ? 'down' : 'up'}`, type: 'button', title: `${d < 0 ? 'Lower' : 'Raise'} ${Math.abs(d)} mm`, onClick: () => nudge(d, b) }, icon(d < 0 ? 'down' : 'up'), h('span', { class: 'mono' }, fmtNudge(d)));
          return b;
        })),
        h('p', { class: 'help' }, 'Steps larger than 0.1 mm are refused close to the bed. Aim for slight drag on the paper, not a pinch.'),
        h('div', { class: 'row wrap' },
          button({ label: 'Accept this height', kind: 'primary', icon: 'check', onClick: async (e) => withBusy(e.currentTarget, async () => { try { await post('calibrate/zoffset/accept'); phase = 'accepted'; await refresh(); paint(); } catch (er) { toast(er.message, 'bad'); } }) }),
          button({ label: 'Abort', kind: 'ghost', onClick: async () => { await post('calibrate/zoffset/abort').catch(() => {}); phase = 'idle'; paint(); } })));
    } else {
      mount(host, alertBox('ok', h('p', null, h('strong', null, 'Height accepted.'), ' Save to write it into printer.cfg. Klipper restarts, which takes about ten seconds.')), saveButton());
    }
  };
  const saveButton = () => button({ label: 'Save and restart Klipper', kind: 'primary', icon: 'shield', onClick: async (e) => withBusy(e.currentTarget, async () => {
    const end = await runJob('calibrate/save', steps, konsole);
    if (end.ok) { await mark('zoffset', 'done'); toast('Z-offset saved', 'ok'); phase = 'idle'; await refresh(); paint(); } else toast(end.result?.error || 'Save failed', 'bad');
  }) });
  paint();
  return h('div', { class: 'stack' }, host, h('details', { class: 'disclosure' }, h('summary', null, 'Klipper output'), konsole.root));
}

function shaperPanel(ctx, status, mark, refresh) {
  const steps = stepList();
  const konsole = makeConsole('Klipper output');
  const host = h('div', { class: 'stack' });
  let result = null; let running = false; let noise = null;
  const noAccel = status.accelerometer === 'none';

  const axisCard = (axis, a) => h('div', { class: 'card axis' },
    h('div', { class: 'row' }, h('span', { class: 'axis-tag mono' }, axis.toUpperCase()), h('div', { class: 'grow' }, h('div', { class: 'muted', style: { fontSize: '.8rem' } }, 'Recommended'),
      h('div', { class: 'rec display' }, a.recommended.type.toUpperCase(), h('small', null, `${a.recommended.freq} Hz`)))),
    h('table', { class: 'fits' }, h('tbody', null, a.fits.map((f) => h('tr', { class: f.type === a.recommended.type ? 'best' : '' },
      h('td', { class: 'mono' }, f.type), h('td', { class: 'mono' }, `${f.freq} Hz`),
      h('td', { class: 'vbar' }, h('span', { class: 'vb', style: { '--v': Math.min(100, f.vibrations * 4) } }), h('span', { class: 'mono' }, `${f.vibrations}%`)),
      h('td', { class: 'mono faint' }, `~${f.smoothing}`))))));

  const paint = () => {
    mount(host,
      noAccel && alertBox('warn', h('p', null, 'No accelerometer is configured. Enable the toolhead ADXL345 in Options (Back) to run this.')),
      !result && h('p', { class: 'help' }, 'The toolhead shakes each axis while the accelerometer listens. It takes about two minutes and is loud. Keep fans off and hands clear.'),
      !result && button({ label: running ? 'Measuring...' : 'Measure resonances', kind: 'primary', icon: 'wave', disabled: running || noAccel, onClick: async () => {
        running = true; paint(); steps.reset(); konsole.clear();
        const end = await runJob('calibrate/shaper', steps, konsole);
        running = false;
        if (end.ok) { result = end.result; await refresh(); } else toast(end.result?.error || 'Measurement failed', 'bad');
        paint();
      } }),
      steps.host,
      result && h('div', { class: 'row wrap' }, Object.entries(result.noise || {}).map(([k, v]) => h('span', { class: `chip mono ${v < 100 ? 'ok' : 'bad'}` }, `noise ${k} ${v}`))),
      result && h('div', { class: 'grid two' }, Object.entries(result.axes).map(([k, a]) => axisCard(k, a))),
      result && h('div', { class: 'row wrap' },
        button({ label: 'Save and restart Klipper', kind: 'primary', icon: 'shield', onClick: async (e) => withBusy(e.currentTarget, async () => {
          const end = await runJob('calibrate/save', steps, konsole);
          if (end.ok) { await mark('shaper', 'done'); toast('Input shaper saved', 'ok'); await refresh(); } else toast(end.result?.error || 'Save failed', 'bad');
        }) }),
        button({ label: 'Measure again', kind: 'ghost', onClick: () => { result = null; paint(); } })));
  };
  paint();
  return h('div', { class: 'stack' }, host, h('details', { class: 'disclosure' }, h('summary', null, 'Klipper output'), konsole.root));
}

function meshPanel(ctx, status, mark, refresh) {
  const steps = stepList();
  const konsole = makeConsole('Klipper output');
  const host = h('div', { class: 'stack' });
  let mesh = null; let running = false;

  const paint = () => {
    mount(host,
      !mesh && h('p', { class: 'help' }, 'The nozzle probes a grid across the bed. The map stays in memory and Klipper applies it layer by layer; adaptive meshing re-probes only the printed area each job.'),
      button({ label: running ? 'Probing...' : mesh ? 'Probe again' : 'Probe the bed', kind: mesh ? '' : 'primary', icon: 'grid', disabled: running, onClick: async () => {
        running = true; paint(); steps.reset(); konsole.clear();
        const end = await runJob('calibrate/mesh', steps, konsole);
        running = false;
        if (end.ok && end.result.matrix) { mesh = end.result; await mark('mesh', 'done'); } else toast(end.result?.error || 'Probing failed', 'bad');
        paint();
      } }),
      steps.host,
      mesh && h('div', { class: 'mesh-wrap' },
        heatmap(mesh),
        h('div', { class: 'mesh-side' },
          h('div', { class: 'legend' }, h('span', { class: 'mono' }, `${mesh.stats.min.toFixed(3)}`), h('span', { class: 'ramp' }), h('span', { class: 'mono' }, `${mesh.stats.max.toFixed(3)}`)),
          h('dl', { class: 'specs mesh-stats' },
            h('div', null, h('dt', null, 'Range'), h('dd', null, `${mesh.stats.range.toFixed(3)} mm`)),
            h('div', null, h('dt', null, 'Std dev'), h('dd', null, `${mesh.stats.stddev.toFixed(3)} mm`))),
          h('span', { class: `chip ${mesh.stats.range < 0.3 ? 'ok' : 'warn'}` }, mesh.stats.range < 0.3 ? 'Nice and flat' : 'Noticeable bow: consider tramming'))));
  };
  paint();
  get('calibrate/mesh').then((m) => { if (m.matrix && !mesh) { mesh = m; paint(); } }).catch(() => {});
  return h('div', { class: 'stack' }, host, h('details', { class: 'disclosure' }, h('summary', null, 'Klipper output'), konsole.root));
}

// ---------------------------------------------------------------- step
export async function enter(ctx) {
  let status = await get('calibrate/status').catch((e) => ({ ready: false, message: e.message }));
  let active = null;
  const cards = h('div', { class: 'grid three tools' });
  const detail = h('div', { class: 'card tool-detail', hidden: true });
  const banner = h('div');

  const marks = () => store.state.calibration || {};
  const mark = async (id, v) => { await patchState({ calibration: { [id]: v } }); paintCards(); setNext(); };
  const setNext = () => ctx.actions.set({ next: { label: Object.keys(marks()).length ? 'Finish' : 'Skip for now', icon: 'arrowRight' }, note: 'You can always run these later from Fluidd.' });
  const refresh = async () => { status = await get('calibrate/status').catch(() => status); paintBanner(); };

  function paintBanner() {
    mount(banner,
      !status.ready && alertBox('warn', h('p', null, h('strong', null, 'Klipper is not ready.')), h('p', null, status.message || 'Waiting for Klipper to start.'),
        h('p', null, button({ label: 'Check again', size: 'sm', icon: 'refresh', onClick: async (e) => withBusy(e.currentTarget, async () => { status = await get('calibrate/status').catch((er) => ({ ready: false, message: er.message })); paintBanner(); openTool(active); }) }))),
      status.ready && status.save_pending && alertBox('info', h('p', null, h('strong', null, 'Unsaved calibration results.'), ' Saving writes them to printer.cfg and restarts Klipper.'),
        h('p', null, button({ label: 'Save and restart Klipper', size: 'sm', icon: 'shield', onClick: async (e) => withBusy(e.currentTarget, async () => {
          const { job } = await post('calibrate/save'); await followJob(job.id, () => {}); await refresh(); toast('Saved', 'ok'); }) }))));
  }

  function paintCards() {
    mount(cards, TOOLS.map((t) => {
      const m = marks()[t.id];
      return h('button', { class: `card tool ${active === t.id ? 'active' : ''} ${m || ''}`, type: 'button', 'aria-pressed': String(active === t.id), onClick: () => openTool(active === t.id ? null : t.id) },
        h('div', { class: 'row' }, h('span', { class: 'link-ico' }, icon(t.icon)), h('span', { class: 'grow' }),
          m === 'done' ? h('span', { class: 'chip ok' }, icon('check'), 'Done') : m === 'skipped' ? h('span', { class: 'chip' }, 'Skipped') : h('span', { class: 'chip' }, 'Not run')),
        h('h3', { class: 'card-title' }, t.title), h('p', { class: 'card-sub' }, t.blurb));
    }));
  }

  function openTool(id) {
    active = id; paintCards();
    if (!id) { detail.hidden = true; return; }
    detail.hidden = false;
    const tool = TOOLS.find((t) => t.id === id);
    const builder = { zoffset: zoffsetPanel, shaper: shaperPanel, mesh: meshPanel }[id];
    mount(detail,
      h('div', { class: 'row detail-head' }, h('h3', { class: 'display', style: { fontSize: '2rem' } }, tool.title), h('span', { class: 'grow' }),
        marks()[id] === 'skipped' || marks()[id] === 'done' ? null : h('button', { class: 'link-btn', type: 'button', onClick: async () => { await mark(id, 'skipped'); openTool(null); } }, 'Skip this one')),
      status.ready ? builder(ctx, status, mark, refresh) : h('p', { class: 'help' }, 'Klipper must be ready first.'));
    detail.classList.remove('rise'); void detail.offsetWidth; detail.classList.add('rise');
  }

  paintCards(); paintBanner();
  setNext();
  return { nodes: [banner, cards, detail], destroy() { window.__eitriBusy = false; } };
}
