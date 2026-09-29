// Thin fetch wrapper over the FastAPI backend. All paths are relative so the same build
// works behind the Vite dev proxy (5173 -> 8000) and when FastAPI serves the bundle.

async function req(method, path, body) {
  const opts = { method, headers: {} };
  if (body instanceof Blob) {
    opts.body = body;  // a file upload goes as raw bytes, not JSON
  } else if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`/api${path}`, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const detail = (data && data.detail) || res.statusText || 'request failed';
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

export const api = {
  dashboard: (sinceDays) =>
    req('GET', `/dashboard${sinceDays == null ? '' : `?since_days=${sinceDays}`}`),
  status: () => req('GET', '/status'),
  job: (key) => req('GET', `/jobs/${key}`),
  saveAnswers: (key, answers) => req('PUT', `/jobs/${key}/answers`, { answers }),
  editFilled: (key, label, value) => req('PUT', `/jobs/${key}/filled`, { label, value }),
  setResume: (key, tag, display) => req('PUT', `/jobs/${key}/resume`, { tag, display }),
  setState: (key, target, reason, meta) =>
    req('POST', `/jobs/${key}/state`, { target, reason, meta }),
  submitJob: (key) => req('POST', `/jobs/${key}/submit`),

  runCollect: () => req('POST', '/run/collect'),
  runFlush: () => req('POST', '/run/flush'),
  runScraper: () => req('POST', '/run/scraper'),
  buildExtension: () => req('POST', '/run/build-extension'),

  queue: () => req('GET', '/queue'),

  bank: () => req('GET', '/bank'),
  saveBank: (row) => req('PUT', '/bank', row),
  deleteBank: (id) => req('DELETE', `/bank/${id}`),

  profile: () => req('GET', '/profile'),
  saveProfile: (profile) => req('PUT', '/profile', { profile }),
  setupStatus: () => req('GET', '/setup/status'),
  uploadResume: (tag, file) => req('PUT', `/resumes/${encodeURIComponent(tag)}`, file),

  search: () => req('GET', '/search'),
  saveSearch: (topics, levels) => req('PUT', '/search', { topics, levels }),

  companies: () => req('GET', '/companies'),
  setTier: (name, tier) => req('PUT', '/companies/tier', { name, tier }),

  draftsPref: () => req('GET', '/prefs/drafts'),
  setDraftsPref: (enabled) => req('PUT', '/prefs/drafts', { enabled }),
  aiPref: () => req('GET', '/prefs/ai'),
  setAiPref: (enabled) => req('PUT', '/prefs/ai', { enabled }),
};
