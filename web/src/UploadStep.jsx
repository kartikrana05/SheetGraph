import { useRef, useState } from 'react'
import { api } from './api'

// Bundled with the frontend (web/public/samples), so a first-time visitor can
// see the whole flow without having a spreadsheet to hand — which is most of
// them. Fetched from this origin, wrapped in a File, then sent through exactly
// the same upload path as a real drop.
const SAMPLES = [
  {
    file: 'fmcg_operations.xlsx',
    name: 'FMCG field operations',
    detail: '4 tabs · 165 rows',
    blurb: 'Sales plus distributor, product and team masters. Three entities appear in more than one tab — including two whose column names differ — and get joined into single nodes.',
    featured: true,
  },
  {
    file: 'pm_tracker.xlsx',
    name: 'Project tracker',
    detail: '1 sheet · 180 rows',
    blurb: 'Projects with owners, departments, status and budgets.',
  },
  {
    file: 'support_tickets.xlsx',
    name: 'Support tickets',
    detail: '1 sheet · 260 rows',
    blurb: 'Tickets by customer, assignee, component and severity.',
  },
  {
    file: 'hiring_pipeline.csv',
    name: 'Hiring pipeline',
    detail: 'CSV · 190 rows',
    blurb: 'Candidates by role, stage, source and recruiter.',
  },
]

const TYPE_HELP = {
  identifier: 'unique per row — usually the entity key',
  measure: 'numeric — stays a property',
  category: 'few repeated values — a good node candidate',
  date: 'stays a property',
  text: 'free text — stays a property',
  boolean: 'true/false flag',
  empty: 'no values found',
}

function SheetCard({ profile, expanded, onToggle }) {
  const shown = expanded ? profile.columns : profile.columns.slice(0, 6)
  const hidden = profile.columns.length - shown.length

  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <div className="row" style={{ marginBottom: 12 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 style={{ margin: 0 }}>{profile.sheetName}</h2>
          <div style={{ color: 'var(--text-dim)', fontSize: 12.5 }}>
            {profile.rowCount.toLocaleString()} rows · {profile.columnCount} columns
            {profile.sheetName !== profile.filename.replace(/\.[^.]+$/, '') &&
              ` · from ${profile.filename}`}
          </div>
        </div>
        {profile.columns.length > 6 && (
          <button className="ghost" onClick={onToggle}>
            {expanded ? 'Show less' : `Show all ${profile.columns.length}`}
          </button>
        )}
      </div>

      <div className="profile-grid">
        {shown.map((col) => (
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
      {hidden > 0 && (
        <div style={{ color: 'var(--text-dim)', fontSize: 12, marginTop: 8 }}>
          + {hidden} more column{hidden > 1 ? 's' : ''}
        </div>
      )}
    </div>
  )
}

export default function UploadStep({ onProfiled }) {
  const [dragging, setDragging] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [profiles, setProfiles] = useState(null)
  const [rejected, setRejected] = useState([])
  const [uploadId, setUploadId] = useState(null)
  const [hint, setHint] = useState('')
  const [expanded, setExpanded] = useState({})
  const [loadingSample, setLoadingSample] = useState(null)
  const inputRef = useRef(null)

  async function loadSample(sample) {
    setBusy(true)
    setError(null)
    setLoadingSample(sample.file)
    try {
      const response = await fetch(`/samples/${sample.file}`)
      if (!response.ok) {
        throw new Error(`Could not load the sample (${response.status})`)
      }
      const blob = await response.blob()
      // Wrapped as a File so it goes through the identical upload path — the
      // sample is not a special case anywhere downstream.
      const file = new File([blob], sample.file, { type: blob.type })
      const result = await api.upload([file])
      setUploadId(result.uploadId)
      setProfiles(result.profiles)
      setRejected(result.rejected || [])
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
      setLoadingSample(null)
    }
  }

  async function handleFiles(fileList) {
    const files = Array.from(fileList || [])
    if (!files.length) return

    setBusy(true)
    setError(null)
    try {
      const result = await api.upload(files)
      setUploadId(result.uploadId)
      setProfiles(result.profiles)
      setRejected(result.rejected || [])
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
      onProfiled({ uploadId, profiles, schema: result.schema, warnings: result.warnings })
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  if (!profiles) {
    return (
      <div>
        <h1>Turn your spreadsheets into one knowledge graph — and a live API</h1>
        <p className="sub" style={{ marginBottom: 18 }}>
          Upload as many sheets as you like. An AI model reads the shape of every column,
          works out which entities appear in more than one sheet, and proposes a single
          graph that joins them. You refine it in plain English, ask questions that cross
          all of them at once, then freeze any question into a REST endpoint your other
          systems can call.
        </p>

        <div className="hero-features">
          <div className="hero-feature">
            <span className="hero-num">1</span>
            <div>
              <b>Sheets become one graph</b>
              <p>
                Every tab of every workbook is read separately, and an entity found in
                more than one of them becomes a single node — so a distributor code in
                your sales export and a party name in your master are the same thing.
              </p>
            </div>
          </div>

          <div className="hero-feature">
            <span className="hero-num">2</span>
            <div>
              <b>Ask in plain English</b>
              <p>
                Questions are translated to Cypher against your inferred schema and run
                read-only. Every answer shows the query it ran, so you can check the
                working rather than trust it.
              </p>
            </div>
          </div>

          <div className="hero-feature">
            <span className="hero-num">3</span>
            <div>
              <b>Publish it as a GET API</b>
              <p>
                Describe an endpoint — “top distributors by revenue, filterable by city
                and tier” — and get a named, parameterised, read-only URL plus a ready
                curl. Your dashboard, cron job or another service can call it directly.
              </p>
              <code className="hero-curl">
                curl '.../api/data/top-distributors?city=Mumbai&amp;tier=Gold'
              </code>
            </div>
          </div>
        </div>

        {error && <div className="error-banner">{error}</div>}

        <div
          className={`dropzone ${dragging ? 'over' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDragging(false)
            handleFiles(e.dataTransfer.files)
          }}
          onClick={() => inputRef.current?.click()}
        >
          {busy ? (
            <div className="thinking" style={{ justifyContent: 'center' }}>
              <span className="spinner" /> Reading your sheets…
            </div>
          ) : (
            <>
              <div className="icon">⬚</div>
              <div style={{ fontWeight: 600, marginTop: 8 }}>
                Drop your spreadsheets here, or click to browse
              </div>
              <div className="hint">
                .xlsx, .xlsm, .csv or .tsv — up to 10 files, 25 MB total.
                Every tab of a workbook is read as its own table.
              </div>
            </>
          )}
          <input
            ref={inputRef}
            type="file"
            multiple
            accept=".csv,.tsv,.txt,.xlsx,.xlsm"
            style={{ display: 'none' }}
            onChange={(e) => handleFiles(e.target.files)}
          />
        </div>

        <div className="sample-divider"><span>or try one of these</span></div>

        <div className="sample-grid">
          {SAMPLES.map((sample) => (
            <button
              key={sample.file}
              className={`sample-card ${sample.featured ? 'featured' : ''}`}
              disabled={busy}
              onClick={() => loadSample(sample)}
            >
              <div className="sample-head">
                <b>{sample.name}</b>
                {sample.featured && <span className="sample-badge">best demo</span>}
              </div>
              <div className="sample-detail">{sample.detail}</div>
              <p className="sample-blurb">{sample.blurb}</p>
              <span className="sample-cta">
                {loadingSample === sample.file
                  ? <><span className="spinner" /> Loading…</>
                  : 'Load this →'}
              </span>
            </button>
          ))}
        </div>
      </div>
    )
  }

  const totalRows = profiles.reduce((sum, p) => sum + p.rowCount, 0)

  return (
    <div>
      <h1>
        {profiles.length} table{profiles.length > 1 ? 's' : ''} read
      </h1>
      <p className="sub">
        {totalRows.toLocaleString()} rows in total. This is how each column was
        interpreted — the profile is what the model reasons about, not your raw data.
      </p>

      {error && <div className="error-banner">{error}</div>}

      {rejected.length > 0 && (
        <div className="warnings">
          <b>Skipped {rejected.length} file{rejected.length > 1 ? 's' : ''}</b>
          <ul>
            {rejected.map((r, i) => <li key={i}>{r.file} — {r.reason}</li>)}
          </ul>
        </div>
      )}

      {profiles.map((profile) => (
        <SheetCard
          key={profile.sheetName}
          profile={profile}
          expanded={!!expanded[profile.sheetName]}
          onToggle={() =>
            setExpanded((prev) => ({
              ...prev,
              [profile.sheetName]: !prev[profile.sheetName],
            }))
          }
        />
      ))}

      <div className="card">
        <h2>Anything you want to steer? (optional)</h2>
        <p className="sub" style={{ marginBottom: 12 }}>
          {profiles.length > 1
            ? 'A sentence about what you want to analyse, or which columns you think link the sheets together.'
            : 'A sentence about what you actually want to analyse. Leave it blank and the model decides on its own.'}
        </p>
        <input
          value={hint}
          onChange={(e) => setHint(e.target.value)}
          placeholder={profiles.length > 1
            ? 'e.g. SKU in the sales sheet is the same as Product Code in the master'
            : 'e.g. I care about which owners are carrying at-risk projects'}
          onKeyDown={(e) => { if (e.key === 'Enter' && !busy) proposeSchema() }}
        />
      </div>

      <div className="row end mt">
        <button className="ghost" onClick={() => { setProfiles(null); setUploadId(null) }}>
          Start over
        </button>
        <button className="primary" onClick={proposeSchema} disabled={busy}>
          {busy
            ? <><span className="spinner" /> Designing schema…</>
            : profiles.length > 1
              ? 'Design one graph across all tables →'
              : 'Design the graph schema →'}
        </button>
      </div>
    </div>
  )
}
