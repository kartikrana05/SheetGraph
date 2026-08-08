import { useState } from 'react'
import { api } from './api'
import { colorFor } from './palette'

const EXAMPLE_EDITS = [
  'Split Priority into its own node',
  'Drop the budget and spend properties',
  'Make Sprint a node connected to Project',
  'Merge Status back into Project as a property',
]

export default function SchemaStep({ uploadId, profile, schema, warnings, onSchema, onSeeded, onBack }) {
  const [instruction, setInstruction] = useState('')
  const [busy, setBusy] = useState(false)
  const [busyLabel, setBusyLabel] = useState('')
  const [error, setError] = useState(null)

  const labels = schema.nodes.map((n) => n.label)

  async function refine(text) {
    const value = (text ?? instruction).trim()
    if (!value) return
    setBusy(true)
    setBusyLabel('Rethinking the schema…')
    setError(null)
    try {
      const result = await api.refine(uploadId, schema, value)
      onSchema(result.schema, result.warnings)
      setInstruction('')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function seed() {
    setBusy(true)
    setBusyLabel('Seeding the graph…')
    setError(null)
    try {
      const result = await api.apply(uploadId, schema)
      onSeeded(result)
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <div>
      <h1>{schema.datasetName}</h1>
      {schema.summary && <p className="sub">{schema.summary}</p>}

      {error && <div className="error-banner">{error}</div>}

      {warnings?.length > 0 && (
        <div className="warnings">
          <b>Adjusted {warnings.length} thing{warnings.length > 1 ? 's' : ''} in the proposal</b>
          <ul>{warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
        </div>
      )}

      <div className="card">
        <h2>Nodes</h2>
        <p className="sub" style={{ marginBottom: 14 }}>
          {schema.nodes.length} entity types will be created from {profile.columnCount} columns.
        </p>
        {schema.nodes.map((node) => (
          <div
            className="schema-node"
            key={node.label}
            style={{ borderLeftColor: colorFor(node.label, labels) }}
          >
            <div className="label-row">
              <span className="label">{node.label}</span>
              <span className="key">key: {node.key} ← “{node.keyColumn}”</span>
            </div>
            {node.properties.length > 0 && (
              <div className="props">
                {node.properties.map((p) => (
                  <span className="prop-tag" key={p.name} title={`from column “${p.column}”`}>
                    {p.name}
                  </span>
                ))}
              </div>
            )}
            {node.reason && <div className="reason">{node.reason}</div>}
          </div>
        ))}
      </div>

      <div className="card">
        <h2>Relationships</h2>
        {schema.relationships.length === 0 ? (
          <p className="sub" style={{ margin: 0 }}>
            None proposed. Ask below to connect two entities.
          </p>
        ) : (
          schema.relationships.map((rel) => (
            <div className="rel-row" key={`${rel.from}-${rel.type}-${rel.to}`}>
              <span style={{ color: colorFor(rel.from, labels) }}>({rel.from})</span>
              <span className="rel-arrow">─[</span>
              <span className="rel-type">:{rel.type}</span>
              <span className="rel-arrow">]→</span>
              <span style={{ color: colorFor(rel.to, labels) }}>({rel.to})</span>
            </div>
          ))
        )}
      </div>

      <div className="card">
        <h2>Change anything</h2>
        <p className="sub" style={{ marginBottom: 12 }}>
          Describe the change in plain English. The whole schema is re-derived and
          re-validated against your real columns.
        </p>
        <div className="row">
          <input
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            placeholder="e.g. split Priority into its own node"
            disabled={busy}
            onKeyDown={(e) => { if (e.key === 'Enter') refine() }}
          />
          <button onClick={() => refine()} disabled={busy || !instruction.trim()}>
            Apply
          </button>
        </div>
        <div className="suggestions" style={{ padding: '12px 0 0' }}>
          {EXAMPLE_EDITS.map((example) => (
            <button
              key={example}
              className="suggestion"
              disabled={busy}
              onClick={() => refine(example)}
            >
              {example}
            </button>
          ))}
        </div>
      </div>

      <div className="row end mt">
        <button className="ghost" onClick={onBack} disabled={busy}>← Back</button>
        <button className="primary" onClick={seed} disabled={busy}>
          {busy
            ? <><span className="spinner" /> {busyLabel}</>
            : `Seed ${profile.rowCount.toLocaleString()} rows into the graph →`}
        </button>
      </div>
    </div>
  )
}
