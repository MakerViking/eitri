import { get, post } from '../api.js';
import { h, icon, mount } from '../dom.js';
import { store } from '../store.js';
import { alertBox, button, switchControl, toast, withBusy } from '../ui.js';

export const meta = {
  id: 'remote', title: 'Remote access', hint: 'Optional, off',
  headline: 'A door, only if you *want* one.',
  lead: 'Remote access starts out off. The printer works fully without it, and nothing here is required. Add an SSH key or a password only if you want to log in from another computer later.',
};

const KEY_START = /^(ssh-|ecdsa-|sk-)/;

/** The bolt: a little rail whose knob slides over when anything has been added. Pure CSS, driven by one class. */
function bolt(open, label) {
  return h('div', { class: `bolt ${open ? 'open' : ''}`, role: 'img', 'aria-label': label },
    h('span', { class: 'bolt-rail' }, h('span', { class: 'bolt-knob' })),
    h('span', { class: 'bolt-words' }, h('b', null, 'Shut'), h('b', null, 'Open')));
}

function fingerprintChips(fps) {
  return h('div', { class: 'fps' }, fps.map((fp) => h('code', { class: 'fp', title: 'Fingerprint, as ssh-keygen -l prints it' }, fp)));
}

function passwordField(id, label, autocomplete) {
  const input = h('input', { class: 'input mono', type: 'password', id, autocomplete, spellcheck: false, autocapitalize: 'off', 'aria-label': label });
  const eye = h('button', { class: 'suffix eye', type: 'button', 'aria-label': `Show ${label.toLowerCase()}`, title: 'Show or hide', onClick: () => {
    input.type = input.type === 'password' ? 'text' : 'password';
  } }, icon('eye'));
  const err = h('div', { class: 'field-error', role: 'alert' });
  return { input, err, node: h('div', { class: 'field' }, h('label', { for: id }, label), h('div', { class: 'input-group' }, input, eye), err) };
}

export async function enter(ctx) {
  let st = await get('access');
  const host = `${store.state.hostname || 'eitri'}.local`;
  const stateHost = h('div', { class: 'stack' });
  const keyErr = h('div', { class: 'field-error', role: 'alert', id: 'key-err' });
  const keyDone = h('div', { class: 'key-done', hidden: true, role: 'status' });

  // ---- SSH key
  const area = h('textarea', {
    class: 'keybox mono', rows: 4, spellcheck: false, autocomplete: 'off', autocapitalize: 'off', placeholder: 'ssh-ed25519 AAAAC3Nza… you@your-computer',
    'aria-label': 'SSH public key', 'aria-describedby': 'key-err',
    onInput: () => { addKey.disabled = !KEY_START.test(area.value.trim()); keyErr.textContent = ''; },
  });
  const addKey = button({ label: 'Add this key', kind: 'primary', icon: 'plus', disabled: true, onClick: () => withBusy(addKey, async () => {
    keyErr.textContent = '';
    try {
      const r = await post('access/key', { key: area.value });
      area.value = ''; addKey.disabled = true;
      st = r.status; paint();
      mount(keyDone, icon('check'), h('span', null, 'Key added: ', h('code', { class: 'fp' }, r.added.fingerprint), r.added.comment && h('span', { class: 'faint' }, ` (${r.added.comment})`)));
      keyDone.hidden = false;
    } catch (e) { if (e.data?.fields?.key) keyErr.textContent = e.data.fields.key; else toast(e.message, 'bad'); }
  }) });
  const keyCard = h('section', { class: 'card remote-card' },
    h('div', { class: 'remote-head' }, h('span', { class: 'link-ico' }, icon('key')),
      h('div', { class: 'grow' }, h('h2', { class: 'card-title' }, 'An SSH key'), h('p', { class: 'card-sub' }, 'The safe way in. Only the computer that holds the matching private key can log in.')),
      h('span', { class: 'chip accent' }, 'Recommended')),
    h('p', { class: 'help' }, 'On your computer run ', h('code', { class: 'cmd' }, 'ssh-keygen -t ed25519'), ' once, then paste the contents of the file ending in ', h('code', { class: 'cmd' }, '.pub'), '. Never paste the private one.'),
    area, keyErr, keyDone,
    h('div', { class: 'remote-actions' }, addKey));

  // ---- password
  let sshLogin = false;
  const pw = passwordField('rm-pw', 'New password', 'new-password');
  const pw2 = passwordField('rm-pw2', 'Repeat the password', 'new-password');
  const sshRow = h('div', { class: 'opt inline ssh-login' },
    h('div', { class: 'opt-text' }, h('div', { class: 'opt-label' }, 'Also let SSH accept this password'),
      h('p', { class: 'help' }, 'Off keeps SSH to keys only, and the password then works at the console and for sudo. On lets anyone on your network who guesses it log in.')),
    h('div', { class: 'opt-ctl' }, switchControl({ checked: false, label: 'Let SSH accept the password over the network', onChange: (v) => { sshLogin = v; } })));
  const setPw = button({ label: 'Set password', kind: '', icon: 'lock', onClick: () => withBusy(setPw, async () => {
    pw.err.textContent = pw2.err.textContent = '';
    try {
      const r = await post('access/password', { password: pw.input.value, confirm: pw2.input.value, ssh_login: sshLogin });
      pw.input.value = pw2.input.value = '';
      pw.input.type = pw2.input.type = 'password';
      st = r.status; paint();
      toast(st.password_auth ? `Password set. SSH accepts it for the user ${st.user}.` : 'Password set for sudo and the console. SSH still needs a key.', 'ok');
    } catch (e) {
      const f = e.data?.fields || {};
      if (f.password || f.confirm) { pw.err.textContent = f.password || ''; pw2.err.textContent = f.confirm || ''; } else toast(e.message, 'bad');
    }
  }) });
  const pwCard = h('details', { class: 'card remote-card disclosure pw-details' },
    h('summary', null, 'Set a password (not recommended)'),
    h('div', { class: 'pw-body' },
      alertBox('warn', h('p', null, h('strong', null, 'A password is more than a login. '), 'It gives the printer’s user administrator rights through sudo, and a console login, and a password can be guessed. A key alone is safer; set one only if you need sudo or the console, and never one you use elsewhere.')),
      h('div', { class: 'grid two' }, pw.node, pw2.node),
      h('p', { class: 'help' }, 'At least 10 characters; a few random words make a good one.'),
      sshRow,
      h('div', { class: 'remote-actions' }, setPw)));

  // ---- turn it off again
  const off = button({ label: 'Remove all remote access', kind: 'danger', icon: 'undo', onClick: () => withBusy(off, async () => {
    try { const r = await post('access/disable', { confirm: true }); st = r.status; keyDone.hidden = true; paint(); toast('Remote access removed.', 'ok'); }
    catch (e) { toast(e.message, 'bad'); }
  }) });
  const offCard = h('section', { class: 'card remote-card', hidden: true },
    h('div', { class: 'remote-head' }, h('span', { class: 'link-ico' }, icon('shield')),
      h('div', { class: 'grow' }, h('h2', { class: 'card-title' }, 'Close the door again'),
        h('p', { class: 'card-sub' }, 'Removes the keys and the password added here. Only what was installed with the printer stays.')), off));

  function paint() {
    const on = st.enabled;
    const plate = h('div', { class: 'plate door' },
      bolt(on, on ? 'Remote access is on' : 'Remote access is off'),
      h('dl', { class: 'specs' },
        h('div', null, h('dt', null, 'Remote access'), h('dd', null, on ? 'On' : 'Off',
          h('small', null, !on ? 'The default' : st.keys || st.password_auth ? `ssh ${st.user}@${host}` : 'Console and sudo only'))),
        h('div', null, h('dt', null, 'SSH keys added'), h('dd', null, String(st.keys), h('small', null, st.keys ? 'Added in this setup' : 'None'))),
        h('div', null, h('dt', null, 'Password'), h('dd', null, st.password ? 'Set' : 'Not set',
          h('small', null, !st.password ? 'Key login only' : st.password_auth ? 'SSH accepts it' : 'Sudo and console, not SSH')))));
    mount(stateHost, plate, st.pending ? h('div', { class: 'chip warn applying' }, h('span', { class: 'dot live' }), 'Applying the change on the printer…') : null,
      st.fingerprints?.length ? fingerprintChips(st.fingerprints) : null);
    if (st.pending) settle();
    offCard.hidden = !on;
    area.disabled = pw.input.disabled = pw2.input.disabled = setPw.disabled = !st.available;
    ctx.actions.set({ next: { label: on ? 'Continue' : 'Skip, keep it off' },
      note: !st.available ? 'Not available on this image.' : on ? 'Remote access is on. You can remove it here at any time before finishing.' : 'Optional. Nothing is added unless you do it here.' });
  }

  // The helper saves at once and the system applies it a moment later (`pending`): follow until it is done.
  let polling = false;
  async function settle() {
    if (polling) return;
    polling = true;
    for (let i = 0; i < 20 && st.pending && alive; i += 1) {
      await new Promise((r) => setTimeout(r, 1500));
      try { st = await get('access'); } catch { break; }
    }
    polling = false;
    if (alive) paint();
  }
  let alive = true;

  paint();
  if (!st.available) {
    return { nodes: [stateHost, alertBox('info', h('p', null, h('strong', null, 'Remote access cannot be changed from here. '), st.reason || 'This image has no remote-access helper.'), h('p', null, 'The step is optional: continue to finish setup.'))] };
  }
  return { nodes: [stateHost, h('div', { class: 'stack' }, keyCard, pwCard), offCard], destroy: () => { alive = false; } };
}
