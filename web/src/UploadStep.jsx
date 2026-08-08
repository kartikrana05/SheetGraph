import { useRef, useState } from 'react'
import { api } from './api'

const TYPE_HELP = {
  identifier: 'unique per row — usually the entity key',
  measure: 'numeric — stays a property',
  category: 'few repeated values — a good node candidate',
  date: 'stays a property',
  text: 'free text — stays a property',
  boolean: 'true/false flag',
  empty: 'no values found',
}

export default function UploadStep({ onProfiled }) {
  const [dragging, setDragging] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [profile, setProfile] = useState(null)
  const [uploadId, setUploadId] = useState(null)
  const [hint, setHint] = useState('')
  const inputRef = useRef(null)

  async function handleFile(file) {
    if (!file) return
    setBusy(true)
    setError(null)
    try {
      const result = await api.upload(file)
      setUploadId(result.uploadId)
      setProfile(result.profile)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function proposeSchema() {
    setBusy(true)
    setError(null)
    try {
      const result = await api.propose(uploadId, hint.trim() || null)
      onProfiled({ uploadId, profile, schema: result.schema, warnings: result.warnings })
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  if (!profile) {
    return (
      <div>
        <h1>Turn a reporting sheet into a knowledge graph</h1>
        <p className="sub">
          Upload any spreadsheet. An AI model reads the shape of your columns and proposes
          a graph schema — which columns become entities, which become properties, and how
          they connect. You refine it in plain English, then explore and query the result.
        </p>

        {error && <div className="error-banner">{error}</div>}

        <div
          className={`dropzone ${dragging ? 'over' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDragging(false)
            handleFile(e.dataTransfer.files?.[0])
          }}
          onClick={() => inputRef.current?.click()}
        >
          {busy ? (
            <div className="thinking" style={{ justifyContent: 'center' }}>
              <span className="spinner" /> Reading your sheet…
            </div>
          ) : (
            <>
              <div className="icon">⬚</div>
              <div style={{ fontWeight: 600, marginTop: 8 }}>
                Drop a spreadsheet here, or click to browse
              </div>
              <div className="hint">.xlsx, .xlsm, .csv or .tsv — up to 15 MB</div>
            </>
          )}
          <input
            ref={inputRef}
            type="file"
            accept=".csv,.tsv,.txt,.xlsx,.xlsm"
            style={{ display: 'none' }}
            onChange={(e) => handleFile(e.target.files?.[0])}
          />
        </div>
      </div>
    )
  }

  return (
    <div>
      <h1>{profile.filename}</h1>
      <p className="sub">
        {profile.rowCount.toLocaleString()} rows · {profile.columnCount} columns.
        Here is how each column was read — this profile is what the model reasons about,
        not your raw data.
      </p>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        <div className="profile-grid">
          {profile.columns.map((col) => (
            <div className="col-chip" key={col.name} title={TYPE_HELP[col.semanticType]}>
              <div className="name">{col.name}</div>
              <div className="meta">
                {col.distinctCount.toLocaleString()} distinct
                {col.fillRate < 1 && ` · ${Math.round(col.fillRate * 100)}% filled`}
              </div>
              <span className={`type-tag type-${col.semanticType}`}>{col.semanticType}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <h2>Anything you want to steer? (optional)</h2>
        <p className="sub" style={{ marginBottom: 12 }}>
          A sentence about what you actually want to analyse. Leave it blank and the model
          decides on its own.
        </p>
        <input
          value={hint}
          onChange={(e) => setHint(e.target.value)}
          placeholder="e.g. I care about which owners are carrying at-risk projects"
          onKeyDown={(e) => { if (e.key === 'Enter' && !busy) proposeSchema() }}
        />
      </div>

      <div className="row end mt">
        <button className="ghost" onClick={() => { setProfile(null); setUploadId(null) }}>
          Choose another file
        </button>
        <button className="primary" onClick={proposeSchema} disabled={busy}>
          {busy ? <><span className="spinner" /> Designing schema…</> : 'Design the graph schema →'}
        </button>
      </div>
    </div>
  )
}
