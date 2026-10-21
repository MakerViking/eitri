// "Camera sign-in": the RTSP password on a page of its own, outside the wizard.
// Same door as everything else: the browser must be paired with the printer first.
import { ApiError, get } from './api.js';
import { h, icon, logo, mount } from './dom.js';
import { showGate } from './gate.js';
import { toggleTheme } from './theme.js';
import { alertBox, button, toast } from './ui.js';

const app = document.getElementById('app');

function frame(...kids) {
  return h('div', { class: 'gate' },
    h('div', { class: 'gate-top' },
      h('a', { class: 'brand', href: './', 'aria-label': 'Eitri setup' }, logo(), h('span', null, h('span', { class: 'brand-name' }, 'Eitri'))),
      h('button', { class: 'icon-btn theme-toggle', type: 'button', 'aria-label': 'Toggle light and dark theme', title: 'Toggle theme', onClick: toggleTheme }, icon('sun', 'sun'), icon('moon', 'moon'))),
    h('main', { class: 'gate-card wide rise' }, ...kids));
}

/** Copy text. The printer is served over plain http, where navigator.clipboard does not exist: fall back to a selection. */
async function copy(text, source) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* fall through */ }
  const ta = h('textarea', { 'aria-hidden': 'true', tabindex: '-1', readonly: true, style: { position: 'fixed', top: '0', left: '-9999px', opacity: '0' } });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  if (!ok && source) { const r = document.createRange(); r.selectNodeContents(source); getSelection().removeAllRanges(); getSelection().addRange(r); }
  return ok;
}

function credentials(d) {
  const host = location.hostname;
  const base = `${host}:${d.port}/${d.stream}`;
  const plain = `rtsp://${base}`;
  const full = `rtsp://${encodeURIComponent(d.user)}:${encodeURIComponent(d.password)}@${base}`;
  let shown = false;
  const mask = '•'.repeat(12);

  const row = (label, hint, textFor, copyText) => {
    const value = h('span', { class: 'cred-value mono' });
    const paint = () => { value.textContent = textFor(shown); };
    paint();
    const btn = button({ label: 'Copy', kind: 'ghost', size: 'sm', icon: 'copy', onClick: async () => {
      const ok = await copy(copyText(), value);
      toast(ok ? `${label} copied.` : 'Could not copy automatically: it is selected, press Ctrl+C.', ok ? 'ok' : 'bad');
    } });
    return { paint, node: h('div', { class: 'cred-row', role: 'group', 'aria-label': label },
      h('div', { class: 'cred-label' }, h('span', null, label), h('small', null, hint)), value, btn) };
  };
  const rows = [
    row('Address', 'No sign-in in it', () => plain, () => plain),
    row('User name', 'Always the same', () => d.user, () => d.user),
    row('Password', 'Generated on this printer', (s) => (s ? d.password : mask), () => d.password),
    row('Address with sign-in', 'For OBS and VLC', (s) => (s ? full : `rtsp://${d.user}:${mask}@${base}`), () => full),
  ];
  const reveal = button({ label: 'Show password', kind: '', icon: 'eye', onClick: () => {
    shown = !shown; rows.forEach((r) => r.paint());
    mount(reveal, icon('eye'), h('span', null, shown ? 'Hide password' : 'Show password'));
  } });
  return h('div', { class: 'stack' },
    h('div', { class: 'plate cred' }, rows.map((r) => r.node), h('div', { class: 'cred-foot' }, reveal)),
    h('div', { class: 'card forged-note' }, h('p', null, h('strong', null, 'Keep it to yourself. '),
      'Anyone who has it can watch the camera. The password is made once and stays the same when you run setup again. To read it over SSH: ', h('code', null, `cat ${d.file}`), '.')));
}

function unavailable(d) {
  const why = !d.camera_enabled ? ['The camera is off', 'Turn it on under Options in setup. Then the stream and its password exist.']
    : !d.gateway_enabled ? ['The RTSP stream is off', 'The camera sign-in belongs to the go2rtc re-streamer. Turn it on under Options in setup, and the printer makes the password the first time it starts.']
      : ['The password does not exist yet', 'go2rtc makes it the first time it starts. Start it from setup, wait a moment, then reload this page.'];
  return alertBox('info', h('p', null, h('strong', null, why[0] + '. ')), h('p', null, why[1]));
}

async function boot() {
  let auth;
  try { auth = await get('auth'); } catch (e) {
    mount(app, frame(h('h1', { class: 'display' }, 'The printer is ', h('em', null, 'unreachable')), h('p', { class: 'lead' }, e.message), button({ label: 'Try again', kind: 'primary', onClick: () => location.reload() })));
    return;
  }
  if (!auth.paired) { showGate(auth); return; }
  let data = null; let problem = null;
  try { data = await get('camera/rtsp'); } catch (e) { if (!(e instanceof ApiError)) throw e; problem = e.message; }
  mount(app, frame(
    h('div', null, h('span', { class: 'eyebrow' }, 'Camera'),
      h('h1', { class: 'display' }, 'Camera ', h('em', null, 'sign-in'), '.'),
      h('p', { class: 'lead' }, 'The password for the camera’s RTSP stream, for OBS, VLC or a recorder. It lives on this page, apart from setup.')),
    problem ? alertBox('bad', h('p', null, problem)) : data.configured ? credentials(data) : unavailable(data),
    h('div', { class: 'gate-actions' }, h('a', { class: 'btn btn-ghost', href: './' }, icon('arrowLeft'), h('span', null, 'Back to setup')))));
  document.title = 'Camera sign-in · Eitri';
}

boot();
