import { useEffect, useState } from 'react'
import { api } from './api'

function CopyButton({ text, label = 'Copy' }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      className="ghost"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text)
        } catch {
          // Clipboard is unavailable over plain http on some browsers; the
          // user can still select the text, so this must not throw.
        }
        setCopied(true)
        setTimeout(() => setCopied(false), 1400)
      }}
    >
      {copied ? '✓ Copied' : label}
    </button>
  )
}

function ParamTable({ params }) {
  if (!params?.length) {
    return <div className="sub" style={{ margin: 0 }}>No filters — this endpoint takes no parameters.</div>
  }
  return (
    <div className="param-table">
      {params.map((p) => (
        <div className="param-row" key={p.name}>
          <code className="param-name">{p.name}</code>
          <span className={`param-type type-${p.type === 'number' ? 'measure' : 'category'}`}>
            {p.type}
          </span>
          <span className="param-req">{p.required ? 'required' : 'optional'}</span>
          <span className="param-desc">
            {p.description}
            {p.default !== null && p.default !== undefined && (
              <em> · defaults to {String(p.default)}</em>
            )}
          </span>
        </div>
      ))}
    </div>
  )
}

function SavedEndpoint({ endpoint, onDelete }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="saved-endpoint">
      <div className="row" style={{ alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <b>{endpoint.name}</b>
          {endpoint.description && (
            <div className="sub" style={{ margin: '2px 0 0', fontSize: 12.5 }}>
              {endpoint.description}
            </div>
          )}
          <code className="endpoint-url">GET /api/data/{endpoint.slug}</code>
        </div>
        <span className="calls-badge" title="Times called">{endpoint.calls ?? 0}</span>
        <button className="ghost" onClick={() => setOpen((v) => !v)}>
          {open ? 'Hide' : 'Details'}
        </button>
      </div>

      {open && (
        <div style={{ marginTop: 10 }}>
          <ParamTable params={endpoint.params} />
          <div className="curl-block">
            <code>{endpoint.curl}</code>
            <CopyButton text={endpoint.curl} />
          </div>
          <details style={{ marginTop: 8 }}>
            <summary className="cypher-summary">Show the Cypher it runs</summary>
            <div className="cypher-block">{endpoint.cypher}</div>
          </details>
          <div className="row end" style={{ marginTop: 8 }}>
            <button className="ghost" onClick={() => onDelete(endpoint.slug)}>Delete</button>
          </div>
        </div>
      )}
    </div>
  )
}

const EXAMPLES = [
  'Top distributors by revenue, filterable by city and tier',
  'All overdue invoices, filter by distributor and minimum value',
  'Product sales totals, filterable by category and brand',
]

export default function ApiPanel({ dataset }) {
  const [prompt, setPrompt] = useState('')
  const [draft, setDraft] = useState(null)
  const [saved, setSaved] = useState([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [name, setName] = useState('')

  useEffect(() => {
    api.listEndpoints(dataset.datasetId)
      .then((r) => setSaved(r.endpoints))
      .catch(() => {})
  }, [dataset.datasetId])

  async function generate(text) {
    const value = (text ?? prompt).trim()
    if (!value) return
    setBusy(true)
    setError(null)
    setDraft(null)
    try {
      const result = await api.draftEndpoint(dataset.datasetId, value)
      setDraft(result)
      setName(result.name)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function persist() {
    setBusy(true)
    setError(null)
    try {
      const result = await api.saveEndpoint(dataset.datasetId, {
        name: name.trim(),
        description: draft.description,
        cypher: draft.cypher,
        params: draft.params,
      })
      setSaved((prev) => [result, ...prev])
      setDraft(null)
      setPrompt('')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function remove(slug) {
    try {
      await api.deleteEndpoint(slug)
      setSaved((prev) => prev.filter((e) => e.slug !== slug))
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div className="api-panel">
      <div className="api-intro">
        Describe an endpoint in plain English. You get a parameterised, read-only
        query saved under a name, callable over HTTP by anything — a dashboard, a
        cron job, another service.
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="row">
        <input
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="e.g. top distributors by revenue, filterable by city"
          disabled={busy}
          onKeyDown={(e) => { if (e.key === 'Enter') generate() }}
        />
        <button className="primary" onClick={() => generate()} disabled={busy || !prompt.trim()}>
          {busy && !draft ? <span className="spinner" /> : 'Generate'}
        </button>
      </div>

      {!draft && saved.length === 0 && (
        <div className="suggestions" style={{ padding: '10px 0 0' }}>
          {EXAMPLES.map((e) => (
            <button key={e} className="suggestion" disabled={busy} onClick={() => generate(e)}>
              {e}
            </button>
          ))}
        </div>
      )}

      {draft && (
        <div className="card" style={{ marginTop: 14 }}>
          <div className="row" style={{ marginBottom: 10 }}>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Endpoint name"
              style={{ fontWeight: 600 }}
            />
          </div>

          {draft.description && (
            <div className="sub" style={{ marginBottom: 12 }}>{draft.description}</div>
          )}

          <h2 style={{ fontSize: 14 }}>Filters</h2>
          <ParamTable params={draft.params} />

          <h2 style={{ fontSize: 14, marginTop: 14 }}>Preview</h2>
          {draft.previewError ? (
            <div className="warnings" style={{ marginBottom: 0 }}>
              The query saved fine but returned an error when run with default
              values: {draft.previewError}
            </div>
          ) : draft.preview?.length ? (
            <div className="result-table">
              <table>
                <thead>
                  <tr>{Object.keys(draft.preview[0]).map((c) => <th key={c}>{c}</th>)}</tr>
                </thead>
                <tbody>
                  {draft.preview.map((row, i) => (
                    <tr key={i}>
                      {Object.keys(draft.preview[0]).map((c) => (
                        <td key={c}>{row[c] === null ? '—' : String(row[c])}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="warnings" style={{ marginBottom: 0 }}>
              Ran successfully but returned no rows with the default filters. That
              may be correct, or the filters may be too narrow.
            </div>
          )}

          <details style={{ marginTop: 12 }}>
            <summary className="cypher-summary">Show the Cypher</summary>
            <div className="cypher-block">{draft.cypher}</div>
          </details>

          <div className="row end mt">
            <button className="ghost" onClick={() => setDraft(null)} disabled={busy}>Discard</button>
            <button className="primary" onClick={persist} disabled={busy || !name.trim()}>
              {busy ? <><span className="spinner" /> Saving…</> : 'Save endpoint'}
            </button>
          </div>
        </div>
      )}

      {saved.length > 0 && (
        <div style={{ marginTop: 18 }}>
          <h2 style={{ fontSize: 14, marginBottom: 10 }}>
            Saved endpoints ({saved.length})
          </h2>
          {saved.map((endpoint) => (
            <SavedEndpoint key={endpoint.slug} endpoint={endpoint} onDelete={remove} />
          ))}
        </div>
      )}
    </div>
  )
}
