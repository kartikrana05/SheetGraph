// In production Zerops injects VITE_API_URL at build time (the api service's
// public subdomain). In dev, vite proxies /api to localhost:8000.
// The deployed API on Zerops. Hardcoded as a fallback because VITE_API_URL is
// compiled in at build time, and a missing value there leaves the literal
// "${API_URL}" in the bundle rather than an address. The env var still wins
// when it holds a real URL, so this does not pin the app to one deployment.
const FALLBACK_API_URL = 'https://api-2b30-8000.prg1.zerops.app'

const RAW = (import.meta.env.VITE_API_URL || '').trim()

// If the platform had no value for the variable at build time, the literal
// "${API_URL}" ends up compiled into the bundle instead of a URL. Left alone
// that produces requests to https://web.../${API_URL}/api/upload, which nginx
// answers with a baffling 405. Treat an unexpanded placeholder as unset.
const UNEXPANDED = /^\$\{[^}]*\}$/.test(RAW)
const FROM_ENV = UNEXPANDED ? '' : RAW.replace(/\/$/, '')

// On localhost the base stays empty so Vite's dev proxy handles /api. Anywhere
// else, fall back to the deployed API rather than posting to the static host.
const ON_LOCALHOST =
  typeof window !== 'undefined' &&
  ['localhost', '127.0.0.1'].includes(window.location.hostname)

const BASE = FROM_ENV || (ON_LOCALHOST ? '' : FALLBACK_API_URL.replace(/\/$/, ''))

// A static build with no API base and no dev proxy would fail every request
// against its own origin. Fail loudly and usefully instead.
export const misconfigured = !BASE && !ON_LOCALHOST

export const misconfigurationReason = UNEXPANDED
  ? 'VITE_API_URL was left as the literal placeholder "' + RAW + '", which means ' +
    'API_URL had no value when this frontend was built.'
  : 'VITE_API_URL was empty when this frontend was built.'

// Runtime override. VITE_API_URL is compiled in at build time, so a wrong or
// missing value normally means a rebuild — a slow loop when the deployment is
// the thing being debugged. This lets the API address be supplied in the
// browser instead, which keeps the app usable while the build config is fixed.
const OVERRIDE_KEY = 'sheetgraph.apiBase'

function readOverride() {
  try {
    return sessionStorage.getItem(OVERRIDE_KEY) || ''
  } catch {
    return ''
  }
}

let override = typeof window !== 'undefined' ? readOverride() : ''

export function apiBase() {
  return override || BASE
}

export function setApiBase(url) {
  override = (url || '').trim().replace(/\/$/, '')
  try {
    if (override) sessionStorage.setItem(OVERRIDE_KEY, override)
    else sessionStorage.removeItem(OVERRIDE_KEY)
  } catch {
    // Private browsing can block sessionStorage; the in-memory value still works.
  }
  return override
}

export function needsApiBase() {
  return !apiBase() && !ON_LOCALHOST
}

async function request(path, options = {}) {
  const response = await fetch(`${apiBase()}${path}`, options)

  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    try {
      const body = await response.json()
      if (body.detail) detail = body.detail
    } catch {
      // Non-JSON error body — keep the status-based message.
    }
    throw new Error(detail)
  }

  const body = await response.text()
  try {
    return JSON.parse(body)
  } catch {
    // The SPA fallback serves index.html for any unmatched path, so a request
    // aimed at the wrong origin comes back as a 200 full of HTML. Without this
    // it surfaces as an opaque JSON parse error.
    if (body.trimStart().startsWith('<')) {
      throw new Error(
        'Got HTML instead of JSON — this request reached the web server, not the API. ' +
        'The API address is wrong or unset.'
      )
    }
    throw new Error('Response was not valid JSON')
  }
}

function postJson(path, body) {
  return request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export const api = {
  health: () => request('/api/health'),

  upload(files) {
    const form = new FormData()
    // Same field name repeated — FastAPI collects these into list[UploadFile]
    for (const file of files) form.append('files', file)
    return request('/api/upload', { method: 'POST', body: form })
  },

  propose: (uploadId, hint) => postJson('/api/schema/propose', { uploadId, hint }),
  refine: (uploadId, schema, instruction) =>
    postJson('/api/schema/refine', { uploadId, schema, instruction }),
  apply: (uploadId, schema) => postJson('/api/schema/apply', { uploadId, schema }),

  datasets: () => request('/api/datasets'),
  dataset: (id) => request(`/api/datasets/${id}`),
  deleteDataset: (id) => request(`/api/datasets/${id}`, { method: 'DELETE' }),
  graph: (id) => request(`/api/datasets/${id}/graph`),
  stats: (id) => request(`/api/datasets/${id}/stats`),
  suggestions: (id) => request(`/api/datasets/${id}/suggestions`),

  expand: (nodeId) => postJson('/api/expand', { nodeId }),

  draftEndpoint: (datasetId, prompt) =>
    postJson('/api/endpoints/draft', { datasetId, prompt }),
  saveEndpoint: (datasetId, endpoint) =>
    postJson('/api/endpoints', { datasetId, ...endpoint }),
  listEndpoints: (datasetId) => request(`/api/datasets/${datasetId}/endpoints`),
  deleteEndpoint: (slug) => request(`/api/endpoints/${slug}`, { method: 'DELETE' }),
  runEndpoint: (slug, query) =>
    request(`/api/data/${slug}${query ? `?${query}` : ''}`),
  chat: (datasetId, message, history) =>
    postJson('/api/chat', { datasetId, message, history }),
}
