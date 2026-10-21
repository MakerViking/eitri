const KEY = 'eitri-theme';
const COLORS = { dark: '#0c0a09', light: '#efe9de' };

export function currentTheme() { return document.documentElement.dataset.theme; }

export function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem(KEY, t); } catch { /* private mode */ }
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', COLORS[t]);
}

export const toggleTheme = () => setTheme(currentTheme() === 'dark' ? 'light' : 'dark');

export function spawnEmbers(host, count = 16) {
  for (let i = 0; i < count; i++) {
    const e = document.createElement('i');
    e.style.setProperty('left', `${Math.random() * 100}%`);
    e.style.setProperty('--s', `${2 + Math.random() * 3.2}px`);
    e.style.setProperty('--d', `${11 + Math.random() * 12}s`);
    e.style.setProperty('--delay', `${-Math.random() * 22}s`);
    e.style.setProperty('--dx', `${(Math.random() - 0.4) * 120}px`);
    host.append(e);
  }
}
