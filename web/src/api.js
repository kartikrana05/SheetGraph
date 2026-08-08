// In production Zerops injects VITE_API_URL at build time (the api service's
// public subdomain). In dev, vite proxies /api to localhost:8000.
const BASE = (import.meta.env.VITE_API_URL || '').replace(/\/$/, '')

// A static build with no API base and no dev proxy would fail every request
// with a confusing 404 from nginx. Fail loudly and usefully instead.
export const misconfigured =
  !BASE &&
  typeof window !== 'undefined' &&
  !['localhost', '127.0.0.1'].includes(window.location.hostname)

async function request(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, options)

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

  return response.json()
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

  upload(file, sheet) {
    const form = new FormData()
    form.append('file', file)
    if (sheet) form.append('sheet', sheet)
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
  chat: (datasetId, message, history) =>
    postJson('/api/chat', { datasetId, message, history }),
}
