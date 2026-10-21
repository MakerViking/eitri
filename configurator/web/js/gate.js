// The door: nothing can be changed until this browser is paired with the printer.
// Pairing proves the person is at the printer (the code is on its screen), and once setup is
// complete the configurator locks so nothing on the network can change the printer.
import { ApiError, get, post } from './api.js';
import { h, icon, logo, mount } from './dom.js';
import { toggleTheme } from './theme.js';
import { button } from './ui.js';

const CMD = (text) => h('code', { class: 'cmd' }, text);

function frame(...kids) {
  return h('div', { class: 'gate' },
    h('div', { class: 'gate-top' },
      h('span', { class: 'brand' }, logo(), h('span', null, h('span', { class: 'brand-name' }, 'Eitri'))),
      h('button', { class: 'icon-btn theme-toggle', type: 'button', 'aria-label': 'Toggle light and dark theme', title: 'Toggle theme', onClick: toggleTheme }, icon('sun', 'sun'), icon('moon', 'moon'))),
    h('main', { class: 'gate-card rise' }, ...kids));
}

function pairForm(auth) {
  const input = h('input', {
    class: 'input code-input', type: 'text', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: 9, spellcheck: false,
    placeholder: '0000-0000', 'aria-label': 'Pairing code', 'aria-describedby': 'gate-msg', autofocus: true,
    onInput: () => {
      const d = input.value.replace(/\D/g, '').slice(0, 8);
      input.value = d.length > 4 ? `${d.slice(0, 4)}-${d.slice(4)}` : d;
      submit.disabled = d.length !== 8 || !!timer;
    },
  });
  const msg = h('p', { class: 'field-error', id: 'gate-msg', role: 'alert' });
  let timer = null;
  const submit = button({ label: 'Pair this browser', kind: 'primary', iconAfter: 'arrowRight', type: 'submit', disabled: true });
  const wait = (secs) => {
    clearInterval(timer);
    let left = secs;
    const tick = () => {
      if (left <= 0) { clearInterval(timer); timer = null; msg.textContent = ''; submit.disabled = input.value.replace(/\D/g, '').length !== 8; return; }
      msg.textContent = `Too many wrong codes. Try again in ${left} s.`;
      submit.disabled = true; left -= 1;
    };
    timer = setInterval(tick, 1000); tick();
  };
  const form = h('form', { class: 'gate-form', novalidate: true, onSubmit: async (e) => {
    e.preventDefault();
    if (submit.disabled) return;
    submit.classList.add('busy'); msg.textContent = '';
    try {
      await post('pair', { code: input.value }, { gate: false });
      location.reload();
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      if (err.status === 423) { showGate({ ...auth, locked: true, pairing_open: false }); return; }
      if (err.status === 429) wait(err.data?.retry_after || 30); else msg.textContent = err.message;
      input.select();
    } finally { submit.classList.remove('busy'); }
  } },
    h('label', { class: 'label', for: 'pair-code' }, 'Pairing code'),
    Object.assign(input, { id: 'pair-code' }),
    msg,
    h('div', { class: 'gate-actions' }, submit));
  if (auth.retry_after) wait(auth.retry_after);
  return form;
}

export function showGate(auth) {
  const locked = auth.locked || !auth.pairing_open;
  const body = locked
    ? [
      h('span', { class: 'eyebrow' }, 'Locked'),
      h('h1', { class: 'display' }, 'Setup is ', h('em', null, 'complete'), '.'),
      h('p', { class: 'lead' }, 'The configurator is locked so nothing on your network can change this printer. To change settings, unlock it from the printer itself.'),
      h('ol', { class: 'howto gate-steps' },
        h('li', null, 'On the printer’s touchscreen choose ', h('b', null, 'Unlock setup'), ', or connect over SSH and run ', CMD('eitri-configurator unlock')),
        h('li', null, 'Come back here and check again. The printer screen will show a pairing code.')),
      h('div', { class: 'gate-actions' }, button({ label: 'Check again', kind: 'primary', icon: 'refresh', onClick: () => location.reload() })),
    ]
    : [
      h('span', { class: 'eyebrow' }, 'Pairing'),
      h('h1', { class: 'display' }, 'Pair with your ', h('em', null, 'printer'), '.'),
      h('p', { class: 'lead' }, 'Setup can reflash the control boards and rewrite the printer’s configuration, so it only listens to someone standing at the printer. Enter the 8-digit code shown on its touchscreen.'),
      pairForm(auth),
      h('details', { class: 'disclosure gate-help' },
        h('summary', null, 'The screen shows no code?'),
        h('p', { class: 'help' }, 'Over SSH, run ', CMD('eitri-configurator pairing'), ' or read it from the log with ', CMD('journalctl -u eitri-configurator | grep PAIRING'), '. Each code works once; the next browser needs the new one.')),
    ];
  mount(document.getElementById('app'), frame(...body));
  document.title = 'Pair with your printer · Eitri';
}

let shown = false;
/** Called when any request is refused for lack of a pairing: swap the wizard for the door. */
export async function gateOnDemand() {
  if (shown) return;
  shown = true;
  let auth = { locked: false, pairing_open: true };
  try { auth = await get('auth'); } catch { /* keep defaults */ }
  showGate(auth);
}
