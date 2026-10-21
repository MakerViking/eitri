import { post } from '../api.js';
import { h, icon } from '../dom.js';
import { store } from '../store.js';
import { ROLE_LABEL } from './detect.js';

export const meta = {
  id: 'done', title: 'Done', hint: 'Ready to print',
  headline: 'It is *forged*.',
  lead: 'Your X-Plus 4 now runs open, mainline-clean firmware, and the touchscreen is driven by Klarity. Nothing proprietary is left in the loop.',
};

export function enter(ctx) {
  post('complete').then((s) => { store.state = s; }).catch(() => {});
  const st = store.state;
  const flavor = store.boot.flavors.find((f) => f.id === st.flavor);
  const host = `${st.hostname || 'eitri'}.local`;
  const origin = `${location.protocol}//${location.hostname}`;
  const k = store.klarity || {};
  const opts = st.options;
  const ui = opts.ui === 'mainsail' ? 'Mainsail' : 'Fluidd';
  const cal = st.calibration || {};

  const fact = (label, value, sub) => h('div', null, h('dt', null, label), h('dd', null, value, sub && h('small', null, sub)));
  const receipt = h('div', { class: 'plate' }, h('dl', { class: 'specs' },
    fact('Firmware', flavor.name, `${flavor.version} · ${flavor.commit}`),
    ...['mainboard', 'toolhead'].map((r) => fact(ROLE_LABEL[r], st.flash?.[r]?.ok ? 'Verified' : 'Not flashed here', st.flash?.[r]?.version || '')),
    fact('Reach it at', host, `${origin.replace(/^https?:\/\//, '')}`),
    fact('Camera', opts.camera ? '1920×1080 MJPEG' : 'Off', opts.camera ? 'ustreamer, served at /webcam/' : ''),
    fact('Calibration', ['zoffset', 'shaper', 'mesh'].filter((c) => cal[c] === 'done').length + ' of 3 done', 'Run the rest from ' + ui)));

  const klarity = h('div', { class: 'card klarity-card' },
    h('span', { class: 'link-ico big' }, icon('screen')),
    h('div', { class: 'grow' }, h('h3', { class: 'card-title' }, 'Klarity, your open screen daemon'),
      h('p', { class: 'card-sub' }, k.connected ? `Driving the ${k.display || 'touchscreen'} over its serial link. The display firmware was never touched.` : k.service === 'active' ? 'Running. Waiting for the display to answer.' : 'Not running. Enable it in Options if you want the touchscreen.')),
    h('span', { class: `chip ${k.connected ? 'ok' : k.service === 'active' ? 'warn' : 'bad'}` }, h('span', { class: `dot ${k.connected ? 'ok' : k.service === 'active' ? 'warn' : 'bad'}` }), k.connected ? 'Screen online' : k.service === 'active' ? 'Running' : 'Stopped'));

  const link = (label, href, ic) => h('a', { class: 'btn', href, target: '_blank', rel: 'noopener' }, icon(ic), h('span', null, label), icon('external'));
  const links = h('div', { class: 'row wrap' },
    opts.camera && link('Camera stream', `${origin}/webcam/stream`, 'camera'),
    link('Moonraker API', `${origin}:7125/server/info`, 'layers'));

  ctx.actions.set({ next: { label: `Open ${ui}`, icon: 'external', onClick: () => { window.location.href = `${origin}/`; } }, note: 'Setup complete. You can reopen this wizard any time at port 8099.' });
  burst();
  return { nodes: [receipt, klarity, h('div', null, h('div', { class: 'section-label', style: { marginTop: 0 } }, 'Quick links'), links),
    h('div', { class: 'card forged-note' }, h('p', null, h('strong', null, 'What stays yours. '), 'Your Klipper tree is a plain git clone (', h('code', null, '/data/firmware/klipper'), '), your config lives in ', h('code', null, '/data/printer_data/config'), ', and your own tweaks go in ', h('code', null, 'user.cfg'), ', which Eitri never overwrites.'))] };
}

// A one-off shower of sparks. Pure CSS animation, removed afterwards.
function burst() {
  const host = h('div', { class: 'burst', 'aria-hidden': 'true' });
  for (let i = 0; i < 34; i++) {
    const a = Math.random() * Math.PI * 2, r = 120 + Math.random() * 380;
    host.append(h('i', { style: { '--x': `${Math.cos(a) * r}px`, '--y': `${Math.sin(a) * r - 120}px`, '--s': `${2 + Math.random() * 4}px`, '--d': `${1.1 + Math.random() * 1.4}s`, '--delay': `${Math.random() * 0.35}s` } }));
  }
  document.body.append(host);
  setTimeout(() => host.remove(), 3200);
}
