import { useEffect, useRef, useState } from 'react'
import { api } from './api'
import GraphCanvas from './GraphCanvas'
import { colorFor } from './palette'

function ResultTable({ rows }) {
  if (!rows?.length) return null
  const columns = Object.keys(rows[0])
  return (
    <div className="result-table">
      <table>
        <thead>
          <tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.slice(0, 25).map((row, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c}>
                  {row[c] === null || row[c] === undefined
                    ? '—'
                    : typeof row[c] === 'object'
                      ? JSON.stringify(row[c])
                      : String(row[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Message({ msg }) {
  const [showCypher, setShowCypher] = useState(false)

  if (msg.role === 'user') {
    return <div className="msg user"><div className="bubble">{msg.text}</div></div>
  }

  return (
    <div className="msg">
      <div className="bubble">
        {msg.text}
        {msg.cypher && (
          <>
            <button className="cypher-toggle" onClick={() => setShowCypher((v) => !v)}>
              {showCypher ? 'Hide' : 'Show'} the query it ran
              {msg.totalRows != null && ` · ${msg.totalRows} row${msg.totalRows === 1 ? '' : 's'}`}
            </button>
            {showCypher && <div className="cypher-block">{msg.cypher}</div>}
          </>
        )}
        {msg.rows?.length > 0 && <ResultTable rows={msg.rows} />}
      </div>
    </div>
  )
}

export default function ExploreView({ dataset, onReset }) {
  const [graph, setGraph] = useState(null)
  const [stats, setStats] = useState(null)
  const [suggestions, setSuggestions] = useState([])
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState(null)
  const [highlight, setHighlight] = useState([])
  const [error, setError] = useState(null)
  const logRef = useRef(null)

  const labels = dataset.schema.nodes.map((n) => n.label)

  useEffect(() => {
    let cancelled = false
    Promise.all([
      api.graph(dataset.datasetId),
      api.stats(dataset.datasetId),
    ])
      .then(([graphData, statsData]) => {
        if (cancelled) return
        setGraph(graphData)
        setStats(statsData)
      })
      .catch((err) => !cancelled && setError(err.message))

    api.suggestions(dataset.datasetId)
      .then((r) => !cancelled && setSuggestions(r.suggestions))
      .catch(() => {})

    return () => { cancelled = true }
  }, [dataset.datasetId])

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, busy])

  async function send(text) {
    const question = (text ?? input).trim()
    if (!question || busy) return

    setInput('')
    setBusy(true)
    setError(null)

    const history = messages.reduce((acc, msg, i) => {
      if (msg.role === 'user' && messages[i + 1]?.role === 'assistant') {
        acc.push({ user: msg.text, assistant: messages[i + 1].text })
      }
      return acc
    }, []).slice(-4)

    setMessages((prev) => [...prev, { role: 'user', text: question }])

    try {
      const result = await api.chat(dataset.datasetId, question, history)
      setMessages((prev) => [...prev, {
        role: 'assistant',
        text: result.answer,
        cypher: result.cypher,
        rows: result.rows,
        totalRows: result.totalRows,
      }])

      // Highlight nodes whose identity appears in the answer. Deliberately
      // strings only: a numeric measure like `9` would otherwise collide with
      // an unrelated numeric property (a completion percentage, a count) and
      // light up nodes that have nothing to do with the question.
      const values = (result.rows || [])
        .flatMap((row) => Object.values(row))
        .filter((v) => typeof v === 'string' && v.trim().length >= 3)
        .map((v) => v.trim())
      setHighlight([...new Set(values)].slice(0, 60))
    } catch (err) {
      setMessages((prev) => [...prev, { role: 'assistant', text: `Something went wrong: ${err.message}` }])
    } finally {
      setBusy(false)
    }
  }

  async function expand(nodeId) {
    try {
      const extra = await api.expand(nodeId)
      setGraph((prev) => {
        const nodeIds = new Set(prev.nodes.map((n) => n.id))
        const edgeKeys = new Set(prev.edges.map((e) => `${e.source}-${e.type}-${e.target}`))
        return {
          nodes: [...prev.nodes, ...extra.nodes.filter((n) => !nodeIds.has(n.id))],
          edges: [...prev.edges, ...extra.edges.filter(
            (e) => !edgeKeys.has(`${e.source}-${e.type}-${e.target}`)
          )],
        }
      })
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div className="explore">
      <div className="graph-pane">
        {graph && graph.nodes.length > 0 ? (
          <GraphCanvas
            data={graph}
            labels={labels}
            highlight={highlight}
            onSelect={setSelected}
          />
        ) : (
          <div className="graph-empty">
            {error ? error : graph ? 'No nodes to display.' : <><span className="spinner" /> Loading graph…</>}
          </div>
        )}

        <div className="graph-overlay">
          {labels.map((label) => (
            <span className="legend-item" key={label}>
              <span className="legend-dot" style={{ background: colorFor(label, labels) }} />
              {label}
              {stats && (
                <b style={{ color: 'var(--text-dim)', fontWeight: 400 }}>
                  {stats.labels.find((l) => l.label === label)?.count ?? 0}
                </b>
              )}
            </span>
          ))}
        </div>

        {selected && (
          <div className="node-detail">
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, marginBottom: 8 }}>
              <b style={{ color: colorFor(selected.label, labels) }}>{selected.label}</b>
              <button className="ghost" onClick={() => setSelected(null)}>✕</button>
            </div>
            {Object.entries(selected.props || {})
              .filter(([key]) => key !== '_ds')
              .map(([key, value]) => (
                <div key={key}>
                  <div className="dt">{key}</div>
                  <p className="dd">{String(value)}</p>
                </div>
              ))}
            <button style={{ width: '100%', marginTop: 4 }} onClick={() => expand(selected.id)}>
              Expand neighbours
            </button>
          </div>
        )}
      </div>

      <div className="chat-pane">
        <div className="chat-header">
          <div className="row">
            <div style={{ flex: 1, minWidth: 0 }}>
              <b>{dataset.name}</b>
              <div style={{ color: 'var(--text-dim)', fontSize: 12 }}>
                {dataset.counts.nodes.toLocaleString()} nodes ·{' '}
                {dataset.counts.relationships.toLocaleString()} relationships
              </div>
            </div>
            <button className="ghost" onClick={onReset}>New sheet</button>
          </div>
        </div>

        {stats && (
          <div className="stat-strip">
            {stats.relationships.map((rel) => (
              <span key={rel.type}>{rel.type} <b>{rel.count}</b></span>
            ))}
          </div>
        )}

        <div className="chat-log" ref={logRef}>
          {messages.length === 0 && (
            <div style={{ color: 'var(--text-dim)', fontSize: 13 }}>
              Ask anything about this data in plain English. Every answer shows the query
              it ran, so you can check the working.
            </div>
          )}
          {messages.map((msg, i) => <Message key={i} msg={msg} />)}
          {busy && (
            <div className="thinking"><span className="spinner" /> Working it out…</div>
          )}
        </div>

        {messages.length === 0 && suggestions.length > 0 && (
          <div className="suggestions">
            {suggestions.map((s) => (
              <button key={s} className="suggestion" onClick={() => send(s)}>{s}</button>
            ))}
          </div>
        )}

        <div className="chat-input">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask a question…"
            disabled={busy}
            onKeyDown={(e) => { if (e.key === 'Enter') send() }}
          />
          <button className="primary" onClick={() => send()} disabled={busy || !input.trim()}>
            Ask
          </button>
        </div>
      </div>
    </div>
  )
}
