import { useEffect, useState } from 'react'
import { api } from './api'

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      className="ghost"
      onClick={async (e) => {
        e.stopPropagation()
        try {
          await navigator.clipboard.writeText(text)
        } catch {
          // Clipboard is blocked over plain http in some browsers; the text is
          // still selectable, so this must never throw.
        }
        setCopied(true)
        setTimeout(() => setCopied(false), 1400)
      }}
    >
      {copied ? '✓ Copied' : 'Copy'}
    </button>
  )
}

function TryIt({ endpoint }) {
  // Seeded from each parameter's example, so the first run returns something.
  const [values, setValues] = useState(() =>
    Object.fromEntries(
      endpoint.params.map((p) => [
        p.name,
        p.example ?? (p.default !== null && p.default !== undefined ? String(p.default) : ''),
      ])
    )
  )
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const query = Object.entries(values)
    .filter(([, v]) => v !== '' && v !== null && v !== undefined)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join('&')

  async function run() {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      setResult(await api.runEndpoint(endpoint.slug, query))
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="try-it">
      {endpoint.params.length > 0 && (
        <div className="try-params">
          {endpoint.params.map((p) => (
            <label className="try-param" key={p.name}>
              <span>
                {p.name}
                {p.required && <em className="req">*</em>}
              </span>
              <input
                value={values[p.name] ?? ''}
                placeholder={p.required ? 'required' : 'any'}
                onChange={(e) =>
                  setValues((prev) => ({ ...prev, [p.name]: e.target.value }))
                }
              />
            </label>
          ))}
        </div>
      )}

      <div className="row" style={{ marginTop: 8 }}>
        <code className="try-url">
          GET /api/data/{endpoint.slug}{query ? `?${query}` : ''}
        </code>
        <button onClick={run} disabled={busy}>
          {busy ? <span className="spinner" /> : 'Send'}
        </button>
      </div>

      {error && <div className="error-banner" style={{ marginTop: 8 }}>{error}</div>}

      {result && (
        <>
          <div className="try-meta">
            {result.count} row{result.count === 1 ? '' : 's'} in {result.tookMs} ms
          </div>
          <pre className="json-block">{JSON.stringify(result.data.slice(0, 5), null, 2)}</pre>
          {result.count > 5 && (
            <div className="try-meta">…{result.count - 5} more not shown</div>
          )}
        </>
      )}
    </div>
  )
}

function EndpointRow({ endpoint }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="library-endpoint">
      <div className="row" style={{ alignItems: 'baseline' }}>
        <code className="method">GET</code>
        <code className="slug">/api/data/{endpoint.slug}</code>
        <span style={{ flex: 1 }} />
        <span className="calls-badge" title="Times called">{endpoint.calls ?? 0}</span>
        <CopyButton text={endpoint.curl} />
        <button className="ghost" onClick={() => setOpen((v) => !v)}>
          {open ? 'Close' : 'Try it'}
        </button>
      </div>
      {endpoint.description && (
        <div className="endpoint-desc">{endpoint.description}</div>
      )}
      {open && <TryIt endpoint={endpoint} />}
    </div>
  )
}

function DatasetCard({ dataset, onOpen, onDelete }) {
  const [expanded, setExpanded] = useState(false)
  const [endpoints, setEndpoints] = useState(null)
  const [loading, setLoading] = useState(false)
  // Two-step rather than a browser confirm(): deleting a dataset drops its
  // nodes, relationships and saved endpoints, and there is no undo.
  const [confirming, setConfirming] = useState(false)
  const [deleting, setDeleting] = useState(false)

  async function toggle() {
    const next = !expanded
    setExpanded(next)
    if (next && endpoints === null) {
      setLoading(true)
      try {
        const result = await api.listEndpoints(dataset.id)
        setEndpoints(result.endpoints)
      } catch {
        setEndpoints([])
      } finally {
        setLoading(false)
      }
    }
  }

  return (
    <div className="library-card">
      <div className="row" style={{ alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <b>{dataset.name}</b>
          <div className="library-meta">
            {(dataset.nodeCount ?? 0).toLocaleString()} nodes ·{' '}
            {(dataset.relCount ?? 0).toLocaleString()} relationships
            {dataset.sheetCount > 1 && ` · ${dataset.sheetCount} sheets`}
            {dataset.createdAt && ` · ${dataset.createdAt.slice(0, 10)}`}
          </div>
        </div>
        {dataset.endpointCount > 0 && (
          <button className="ghost" onClick={toggle}>
            {dataset.endpointCount} API{dataset.endpointCount > 1 ? 's' : ''}
            {expanded ? ' ▴' : ' ▾'}
          </button>
        )}

        {confirming ? (
          <>
            <button
              className="danger"
              disabled={deleting}
              onClick={async () => {
                setDeleting(true)
                try {
                  await onDelete(dataset.id)
                } finally {
                  setDeleting(false)
                  setConfirming(false)
                }
              }}
            >
              {deleting ? <span className="spinner" /> : 'Delete for good'}
            </button>
            <button className="ghost" disabled={deleting} onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </>
        ) : (
          <>
            <button className="ghost" title="Delete this dataset" onClick={() => setConfirming(true)}>
              Delete
            </button>
            <button onClick={() => onOpen(dataset.id)}>Open</button>
          </>
        )}
      </div>

      {confirming && (
        <div className="confirm-note">
          Deletes {(dataset.nodeCount ?? 0).toLocaleString()} nodes,{' '}
          {(dataset.relCount ?? 0).toLocaleString()} relationships
          {dataset.endpointCount > 0 &&
            ` and ${dataset.endpointCount} saved endpoint${dataset.endpointCount > 1 ? 's' : ''}`}
          . This cannot be undone.
        </div>
      )}

      {expanded && (
        <div style={{ marginTop: 10 }}>
          {loading && <div className="thinking"><span className="spinner" /> Loading…</div>}
          {endpoints?.map((endpoint) => (
            <EndpointRow key={endpoint.slug} endpoint={endpoint} />
          ))}
          {endpoints?.length === 0 && (
            <div className="sub" style={{ margin: 0 }}>No endpoints saved yet.</div>
          )}
        </div>
      )}
    </div>
  )
}

export default function DatasetLibrary({ onOpen }) {
  const [datasets, setDatasets] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.datasets()
      .then((r) => setDatasets(r.datasets))
      .catch((err) => setError(err.message))
  }, [])

  async function remove(id) {
    setError(null)
    try {
      await api.deleteDataset(id)
      setDatasets((prev) => prev.filter((d) => d.id !== id))
    } catch (err) {
      setError(err.message)
    }
  }

  // Nothing seeded yet is the normal first-run state, not a problem worth
  // showing — the dropzone above already tells the user what to do.
  if (!datasets?.length) return null

  const totalEndpoints = datasets.reduce((n, d) => n + (d.endpointCount || 0), 0)

  return (
    <div style={{ marginTop: 40 }}>
      <h2>Already loaded</h2>
      <p className="sub" style={{ marginBottom: 14 }}>
        {datasets.length} dataset{datasets.length > 1 ? 's' : ''} in the graph
        {totalEndpoints > 0 && (
          <> · {totalEndpoints} live API endpoint{totalEndpoints > 1 ? 's' : ''} you can
          call right now</>
        )}
        . Open one to keep exploring, or try an endpoint without leaving this page.
      </p>
      {error && <div className="error-banner">{error}</div>}

      {datasets.map((dataset) => (
        <DatasetCard
          key={dataset.id}
          dataset={dataset}
          onOpen={onOpen}
          onDelete={remove}
        />
      ))}
    </div>
  )
}
