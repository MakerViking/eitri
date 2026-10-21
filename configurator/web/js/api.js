// Backend client. All URLs are relative so the app also works behind a reverse
// proxy prefix (e.g. nginx serving it under /configure/).

export class ApiError extends Error {
  constructor(message, status, data) { super(message); this.status = status; this.data = data; }
}

export async function api(path, { method = 'GET', body } = {}) {
  let res;
  try {
    res = await fetch(`api/${path}`, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: 'no-store',
    });
  } catch {
    throw new ApiError('Cannot reach the configurator. Check that the printer is powered and on your network.', 0);
  }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) throw new ApiError(data?.error || `Request failed (${res.status})`, res.status, data);
  return data;
}

export const get = (p) => api(p);
export const post = (p, body = {}) => api(p, { method: 'POST', body });
export const put = (p, body) => api(p, { method: 'PUT', body });

/**
 * Follow a job's event stream (Server-Sent Events), replaying history first.
 * EventSource reconnects by itself and resumes via Last-Event-ID, so a flaky
 * phone connection never loses flashing progress.  Resolves with the final
 * `job.end` event.
 */
export function followJob(jobId, onEvent, since = 0) {
  return new Promise((resolve) => {
    const es = new EventSource(`api/jobs/${jobId}/events?since=${since}`);
    es.onmessage = (m) => {
      let ev;
      try { ev = JSON.parse(m.data); } catch { return; }
      try { onEvent(ev); } finally {
        if (ev.type === 'job.end') { es.close(); resolve(ev); }
      }
    };
  });
}

/** Start a job and follow it; returns { job, done } where done resolves with job.end. */
export async function runJob(path, body, onEvent) {
  const { job } = await post(path, body);
  return { job, done: followJob(job.id, onEvent) };
}

/** Latest job of a kind, if any (used to re-attach after a reload). */
export async function latestJob(kind) {
  try { return await get(`jobs/latest/${kind}`); } catch { return null; }
}
