import { useEffect, useState } from 'react'
import { api, misconfigurationReason, needsApiBase, setApiBase, apiBase } from './api'
import UploadStep from './UploadStep'
import DatasetLibrary from './DatasetLibrary'
import SchemaStep from './SchemaStep'
import ExploreView from './ExploreView'

const STEPS = ['Upload', 'Design schema', 'Explore']

export default function App() {
  const [step, setStep] = useState(0)
  const [pending, setPending] = useState(null)   // { uploadId, profile, schema, warnings }
  const [dataset, setDataset] = useState(null)   // { datasetId, name, counts, schema }
  const [health, setHealth] = useState(null)
  const [opening, setOpening] = useState(null)
  const [needsBase, setNeedsBase] = useState(needsApiBase())
  const [baseInput, setBaseInput] = useState('')
  const [baseError, setBaseError] = useState(null)
  const [checking, setChecking] = useState(false)

  async function applyApiBase() {
    const value = baseInput.trim()
    if (!value) return
    setChecking(true)
    setBaseError(null)
    const previous = apiBase()
    setApiBase(value)
    try {
      // Prove the address before accepting it, so a typo is caught here
      // rather than surfacing as a confusing failure three screens later.
      const result = await api.health()
      setHealth(result)
      setNeedsBase(false)
    } catch (err) {
      setApiBase(previous)
      setBaseError(err.message)
    } finally {
      setChecking(false)
    }
  }

  async function openDataset(id) {
    setOpening(id)
    try {
      const saved = await api.dataset(id)
      setDataset(saved)
      setStep(2)
    } catch (err) {
      setOpening(null)
      // Surfaced in place of the library rather than as a silent no-op.
      window.alert(`Could not open that dataset: ${err.message}`)
    }
  }

  useEffect(() => {
    if (needsBase) return
    api.health().then(setHealth).catch(() => setHealth({ status: 'error' }))
  }, [needsBase])

  if (needsBase) {
    return (
      <div className="app">
        <header className="topbar">
          <div className="brand">Sheet<span>Graph</span></div>
          <div className="tagline">connect to your API</div>
        </header>
        <div className="stage">
          <div className="center-wrap" style={{ maxWidth: 620 }}>
            <h1>Where is your API?</h1>
            <p className="sub">
              This build has no API address compiled into it, so it does not know
              where to send requests.
            </p>

            <div className="warnings">
              {misconfigurationReason}
            </div>

            <div className="card">
              <h2>Enter it now</h2>
              <p className="sub" style={{ marginBottom: 12 }}>
                Paste the api service's public subdomain. It is checked before being
                accepted, and remembered for this browser tab — no rebuild needed.
              </p>
              <div className="row">
                <input
                  value={baseInput}
                  onChange={(e) => setBaseInput(e.target.value)}
                  placeholder="https://api-1a2b-8000.prg1.zerops.app"
                  disabled={checking}
                  onKeyDown={(e) => { if (e.key === 'Enter') applyApiBase() }}
                />
                <button className="primary" onClick={applyApiBase} disabled={checking || !baseInput.trim()}>
                  {checking ? <span className="spinner" /> : 'Connect'}
                </button>
              </div>
              {baseError && (
                <div className="error-banner" style={{ marginTop: 12, marginBottom: 0 }}>
                  {baseError}
                </div>
              )}
            </div>

            <div className="card">
              <h2>Or fix it permanently</h2>
              <p className="sub" style={{ margin: 0 }}>
                Set the project environment variable <code>API_URL</code> to that same
                subdomain, then <b>rebuild</b> the <code>web</code> service. The value is
                compiled into the bundle at build time, so setting it without rebuilding
                changes nothing.
              </p>
            </div>
          </div>
        </div>
      </div>
    )
  }

  const neo4jUp = health?.neo4j === 'connected'

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">Sheet<span>Graph</span></div>
        <div className="tagline">any spreadsheet → a graph you can ask questions of</div>
        <div className="spacer" />
        {health && (
          <>
            <span className={`pill ${neo4jUp ? 'ok' : 'bad'}`}>
              neo4j {neo4jUp ? 'up' : 'down'}
            </span>
            <span className={`pill ${health.llmConfigured ? 'ok' : 'bad'}`}>
              llm {health.llmConfigured ? 'ready' : 'unset'}
            </span>
          </>
        )}
      </header>

      {step === 2 && dataset ? (
        <div className="stage" style={{ overflow: 'hidden' }}>
          <ExploreView
            dataset={dataset}
            onReset={() => { setDataset(null); setPending(null); setStep(0) }}
          />
        </div>
      ) : (
        <div className="stage">
          <div className="center-wrap">
            <div className="steps">
              {STEPS.map((name, i) => (
                <div key={name} style={{ display: 'contents' }}>
                  {i > 0 && <div className="step-sep" />}
                  <div className={`step ${i === step ? 'active' : ''} ${i < step ? 'done' : ''}`}>
                    <span className="num">{i < step ? '✓' : i + 1}</span>
                    {name}
                  </div>
                </div>
              ))}
            </div>

            {health && !neo4jUp && (
              <div className="warnings">
                The graph database is not answering yet. On Zerops the Neo4j service runs in
                a VM and takes a minute or two to boot after a restart — this usually clears
                on its own. {health.neo4j}
              </div>
            )}

            {step === 0 && (
              <>
                <UploadStep
                  onProfiled={(result) => { setPending(result); setStep(1) }}
                />
                {opening ? (
                  <div className="thinking" style={{ marginTop: 40 }}>
                    <span className="spinner" /> Opening…
                  </div>
                ) : (
                  <DatasetLibrary onOpen={openDataset} />
                )}
              </>
            )}

            {step === 1 && pending && (
              <SchemaStep
                uploadId={pending.uploadId}
                profiles={pending.profiles}
                schema={pending.schema}
                warnings={pending.warnings}
                onSchema={(schema, warnings) => setPending({ ...pending, schema, warnings })}
                onSeeded={(result) => {
                  setDataset(result)
                  setStep(2)
                }}
                onBack={() => setStep(0)}
              />
            )}
          </div>
        </div>
      )}
    </div>
  )
}
