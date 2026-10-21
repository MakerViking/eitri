import { get, post } from '../api.js';
import { h, icon, mount } from '../dom.js';
import { patchState, store } from '../store.js';
import { alertBox, button, toast, withBusy } from '../ui.js';

export const meta = {
  id: 'network', title: 'Network', hint: 'Connection and name',
  headline: 'How will you *reach* it?',
  lead: 'Wired is the most dependable link for flashing. Wi-Fi works for everyday use. Pick a name you will recognise on your network.',
};

const HOST_RE = /^(?!-)[a-z0-9-]{1,63}(?<!-)$/;

function bars(signal) {
  const lit = signal > 75 ? 4 : signal > 50 ? 3 : signal > 25 ? 2 : 1;
  return h('span', { class: 'bars', title: `${signal}%`, 'aria-label': `Signal ${signal} percent` }, [1, 2, 3, 4].map((n) => h('i', { class: n <= lit ? 'on' : '', style: { height: `${n * 4 + 2}px` } })));
}

export async function enter(ctx) {
  let net = await get('network');
  const wifiHost = h('div', { class: 'wifi-list' });
  const linkHost = h('div', { class: 'grid two' });
  const hostInput = h('input', { class: 'input mono', id: 'hostname', value: store.state.hostname || net.hostname || 'eitri', maxlength: 63, autocomplete: 'off', autocapitalize: 'none', spellcheck: false, 'aria-describedby': 'host-help' });
  const hostMsg = h('div', { class: 'help', id: 'host-help' });
  let selected = null;

  const paintLinks = () => {
    const eth = net.interfaces.find((i) => i.kind === 'ethernet');
    const wifi = net.interfaces.find((i) => i.kind === 'wifi');
    const card = (ic, title, iface, extra) => h('div', { class: 'card link-card' },
      h('div', { class: 'row' }, h('span', { class: 'link-ico' }, icon(ic)), h('div', { class: 'grow' }, h('h3', { class: 'card-title' }, title),
        h('p', { class: 'card-sub' }, iface ? (iface.up && iface.addresses.length ? iface.addresses.join(', ') : iface.up ? 'Link up, waiting for an address' : 'Not connected') : 'Not available')),
        h('span', { class: `chip ${iface?.up && iface.addresses.length ? 'ok' : ''}` }, h('span', { class: `dot ${iface?.up && iface.addresses.length ? 'ok' : ''}` }), iface?.up && iface.addresses.length ? 'Connected' : 'Offline')),
      extra);
    mount(linkHost,
      card('ethernet', 'Ethernet', eth, eth?.speed ? h('p', { class: 'help link-meta' }, `${eth.speed} Mbit/s · ${eth.mac}`) : null),
      card('wifi', 'Wi-Fi', wifi, wifi?.ssid ? h('p', { class: 'help link-meta' }, `Joined “${wifi.ssid}”`) : null));
  };

  const connect = async (net_, pw, btn) => {
    await withBusy(btn, async () => {
      try {
        await post('network/wifi', { ssid: net_.ssid, password: pw || undefined });
        toast(`Joined ${net_.ssid}`, 'ok');
        selected = null;
        net = await get('network');
        paintLinks(); await paintWifi();
      } catch (e) { toast(e.message, 'bad'); }
    });
  };

  let lastScan = null;

  async function paintWifi(force) {
    if (!net.wifi_supported && !force) {
      mount(wifiHost, h('p', { class: 'help' }, 'No Wi-Fi adapter or manager was found on this image. Ethernet is the way in.'));
      return;
    }
    mount(wifiHost, h('div', { class: 'shimmer', style: { height: '3rem' } }));
    lastScan = await get('network/wifi').catch((e) => ({ supported: false, networks: [], reason: e.message }));
    renderWifi();
  }

  // Render from the last scan; selecting a network must not trigger another radio scan.
  function renderWifi() {
    const res = lastScan;
    if (!res.supported) { mount(wifiHost, h('p', { class: 'help' }, res.reason || 'Wi-Fi is unavailable.')); return; }
    mount(wifiHost, ...res.networks.map((n) => {
      const open = selected?.ssid === n.ssid;
      const pw = h('input', { class: 'input', type: 'password', placeholder: 'Wi-Fi password', autocomplete: 'off', 'aria-label': `Password for ${n.ssid}` });
      const go = button({ label: 'Join', kind: 'primary', size: 'sm', onClick: () => connect(n, pw.value, go) });
      pw.addEventListener('keydown', (e) => { if (e.key === 'Enter') go.click(); });
      if (open && n.security) setTimeout(() => pw.focus(), 0);
      return h('div', { class: `wifi-item ${open ? 'open' : ''}` },
        h('button', { class: 'wifi-row', type: 'button', onClick: () => { selected = open ? null : n; renderWifi(); } },
          bars(n.signal), h('span', { class: 'grow wifi-name' }, n.ssid), n.in_use && h('span', { class: 'chip ok' }, 'Joined'), n.security ? icon('lock') : h('span', { class: 'faint', style: { fontSize: '.75rem' } }, 'open')),
        open && h('div', { class: 'wifi-join row' }, n.security ? h('div', { class: 'grow' }, pw) : h('span', { class: 'grow help' }, 'This network has no password.'), go));
    }));
  }

  const validate = () => {
    const v = hostInput.value.trim().toLowerCase();
    const ok = HOST_RE.test(v);
    hostMsg.className = `help ${ok ? '' : 'field-error'}`;
    hostMsg.textContent = ok ? `Reachable as ${v}.local once you are on the same network.` : 'Use letters, digits and hyphens, up to 63 characters, not starting or ending with a hyphen.';
    ctx.actions.set({ next: { disabled: !ok, onClick: save } });
    return ok;
  };
  async function save() {
    const v = hostInput.value.trim().toLowerCase();
    if (v !== (store.state.hostname || net.hostname)) {
      try { await post('network/hostname', { hostname: v }); } catch (e) { toast(e.message, 'bad'); throw e; }
    }
    await patchState({ hostname: v });
    await ctx.advance();
  }
  hostInput.addEventListener('input', validate);

  paintLinks();
  const hasWired = net.interfaces.some((i) => i.kind === 'ethernet' && i.addresses.length);
  setTimeout(validate, 0);
  paintWifi();

  const nodes = [
    linkHost,
    !hasWired && alertBox('warn', h('p', null, h('strong', null, 'No Ethernet link detected.'), ' You can continue on Wi-Fi, but plug in a cable before flashing: a dropped connection mid-update is the one thing we would rather avoid.')),
    h('div', { class: 'card' },
      h('div', { class: 'row', style: { marginBottom: '1rem' } }, h('div', { class: 'grow' }, h('h3', { class: 'card-title' }, 'Wi-Fi networks'), h('p', { class: 'card-sub' }, 'Optional if Ethernet is connected.')),
        button({ label: 'Scan', kind: '', size: 'sm', icon: 'refresh', onClick: (e) => withBusy(e.currentTarget, () => paintWifi(true)) })),
      wifiHost),
    h('div', { class: 'card' }, h('div', { class: 'field' }, h('label', { for: 'hostname' }, 'Printer name'),
      h('div', { class: 'input-group' }, hostInput, h('span', { class: 'suffix' }, '.local')), hostMsg)),
  ].filter(Boolean);
  return { nodes };
}
