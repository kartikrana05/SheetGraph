import { useEffect, useState } from 'react'
import { api, misconfigured } from './api'
import UploadStep from './UploadStep'
import SchemaStep from './SchemaStep'
import ExploreView from './ExploreView'

const STEPS = ['Upload', 'Design schema', 'Explore']

export default function App() {
  const [step, setStep] = useState(0)
  const [pending, setPending] = useState(null)   // { uploadId, profile, schema, warnings }
  const [dataset, setDataset] = useState(null)   // { datasetId, name, counts, schema }
  const [health, setHealth] = useState(null)

  useEffect(() => {
    if (misconfigured) return
    api.health().then(setHealth).catch(() => setHealth({ status: 'error' }))
  }, [])

  if (misconfigured) {
    return (
      <div className="app">
        <header className="topbar">
          <div className="brand">Sheet<span>Graph</span></div>
        </header>
        <div className="stage">
          <div className="center-wrap">
            <div className="error-banner">
              <b>This build has no API address.</b>
              <p style={{ margin: '8px 0 0' }}>
                Set the project environment variable <code>API_URL</code> to the api
                service's public subdomain, then redeploy the <code>web</code> service.
                Until then the frontend has nothing to talk to.
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
              <UploadStep
                onProfiled={(result) => { setPending(result); setStep(1) }}
              />
            )}

            {step === 1 && pending && (
              <SchemaStep
                uploadId={pending.uploadId}
                profile={pending.profile}
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
