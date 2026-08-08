import { useState } from 'react'
import { api } from './api'
import { colorFor } from './palette'

const SINGLE_EDITS = [
  'Split Priority into its own node',
  'Drop the budget and spend properties',
  'Merge Status back into Project as a property',
]

const MULTI_EDITS = [
  'Join the two sheets on customer name',
  'Split that entity back into separate nodes per sheet',
  'Connect the two tables through a shared product',
]

export default function SchemaStep({ uploadId, profiles, schema, warnings, onSchema, onSeeded, onBack }) {
  const [instruction, setInstruction] = useState('')
  const [busy, setBusy] = useState(false)
  const [busyLabel, setBusyLabel] = useState('')
  const [error, setError] = useState(null)

  const labels = schema.nodes.map((n) => n.label)
  const multi = (schema.sheets || []).length > 1
  const joined = schema.nodes.filter((n) => n.sources.length > 1)
  const totalRows = profiles.reduce((sum, p) => sum + p.rowCount, 0)

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

      {multi && (
        <div className="card" style={{ borderColor: joined.length ? '#1f5c2b' : '#5c4a1a' }}>
          <h2>Cross-sheet joins</h2>
          {joined.length === 0 ? (
            <p className="sub" style={{ margin: 0 }}>
              No entity was found in more than one sheet, so these tables will sit side by
              side rather than connect. If you know two columns refer to the same thing,
              say so below — that is the difference between one graph and several.
            </p>
          ) : (
            <>
              <p className="sub" style={{ marginBottom: 12 }}>
                {joined.length} entit{joined.length > 1 ? 'ies' : 'y'} appear in more than
                one sheet and will become a single node. This is what lets one question
                span all your tables.
              </p>
              {joined.map((node) => (
                <div className="join-row" key={node.label}>
                  <span className="join-label" style={{ color: colorFor(node.label, labels) }}>
                    {node.label}
                  </span>
                  <div className="join-sources">
                    {node.sources.map((s, i) => (
                      <span key={s.sheet}>
                        {i > 0 && <span className="join-eq">=</span>}
                        <span className="prop-tag">
                          {s.sheet} · {s.keyColumn}
                        </span>
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </>
          )}
        </div>
      )}

      <div className="card">
        <h2>Nodes</h2>
        <p className="sub" style={{ marginBottom: 14 }}>
          {schema.nodes.length} entity types from {profiles.length} table
          {profiles.length > 1 ? 's' : ''}.
        </p>
        {schema.nodes.map((node) => (
          <div
            className="schema-node"
            key={node.label}
            style={{ borderLeftColor: colorFor(node.label, labels) }}
          >
            <div className="label-row">
              <span className="label">{node.label}</span>
              <span className="key">key: {node.key}</span>
              {node.sources.length > 1 && (
                <span className="join-badge">joined across {node.sources.length} sheets</span>
              )}
            </div>

            {node.sources.map((source) => (
              <div key={source.sheet} style={{ marginTop: 7 }}>
                {multi && (
                  <div className="source-line">
                    <b>{source.sheet}</b> · key from “{source.keyColumn}”
                  </div>
                )}
                {!multi && (
                  <div className="source-line">key from “{source.keyColumn}”</div>
                )}
                {source.properties.length > 0 && (
                  <div className="props">
                    {source.properties.map((p) => (
                      <span className="prop-tag" key={p.name} title={`from column “${p.column}”`}>
                        {p.name}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}

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
            <div className="rel-row" key={`${rel.from}-${rel.type}-${rel.to}-${rel.sheet}`}>
              <span style={{ color: colorFor(rel.from, labels) }}>({rel.from})</span>
              <span className="rel-arrow">─[</span>
              <span className="rel-type">:{rel.type}</span>
              <span className="rel-arrow">]→</span>
              <span style={{ color: colorFor(rel.to, labels) }}>({rel.to})</span>
              {multi && <span className="rel-sheet">from {rel.sheet}</span>}
            </div>
          ))
        )}
      </div>

      <div className="card">
        <h2>Change anything</h2>
        <p className="sub" style={{ marginBottom: 12 }}>
          Describe the change in plain English. The whole schema is re-derived and
          re-validated against your real sheets and columns.
        </p>
        <div className="row">
          <input
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            placeholder={multi
              ? 'e.g. Distributor in sales is the same as Party Name in the master'
              : 'e.g. split Priority into its own node'}
            disabled={busy}
            onKeyDown={(e) => { if (e.key === 'Enter') refine() }}
          />
          <button onClick={() => refine()} disabled={busy || !instruction.trim()}>
            Apply
          </button>
        </div>
        <div className="suggestions" style={{ padding: '12px 0 0' }}>
          {(multi ? MULTI_EDITS : SINGLE_EDITS).map((example) => (
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
            : `Seed ${totalRows.toLocaleString()} rows into the graph →`}
        </button>
      </div>
    </div>
  )
}
