const KEY = 'eitri-theme';
const COLORS = { dark: '#080b0f', light: '#e9eef3' };

export function currentTheme() { return document.documentElement.dataset.theme; }

export function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem(KEY, t); } catch { /* private mode */ }
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', COLORS[t]);
}

export const toggleTheme = () => setTheme(currentTheme() === 'dark' ? 'light' : 'dark');
