// Shared client state: what the server told us at boot plus the persisted wizard state.
import { get, put, session } from './api.js';

export const store = { boot: null, state: null, klarity: null };
const listeners = new Set();

export const subscribe = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
const emit = () => listeners.forEach((fn) => fn(store));

export async function load() {
  store.boot = await get('bootstrap');
  store.state = store.boot.state;
  session.csrf = store.boot.auth?.csrf || null;
  store.klarity = store.boot.klarity;
  emit();
  return store;
}

/** Persist a patch of wizard state and adopt the server's canonical copy. */
export async function patchState(patch) {
  store.state = await put('state', patch);
  emit();
  return store.state;
}

export async function refreshKlarity() {
  try { store.klarity = await get('klarity'); emit(); } catch { /* keep last */ }
}

