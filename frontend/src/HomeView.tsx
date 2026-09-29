import { useEffect, useState } from 'react'
import { api, errMsg, LibraryProject } from './api'
import { openProject } from './project'
import type { Session } from './types'
import { defaultBin, TRACE_STEPS, TRACE_STEP_HINTS } from './types'
import { expiryNote, stamp } from './util'

const CameraIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M4 8h3l2-3h6l2 3h3v11H4z" /><circle cx="12" cy="13" r="3.5" />
  </svg>
)
const GridIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="4" y="4" width="16" height="16" rx="2" /><path d="M12 4v16M4 12h16" />
  </svg>
)

export default function HomeView({ update }: { update: (p: Partial<Session>) => void }) {
  const [projects, setProjects] = useState<LibraryProject[]>([])
  const [err, setErr] = useState('')
  const [name, setName] = useState('')
  const [opening, setOpening] = useState('')
  useEffect(() => { api.library().then((r) => setProjects(r.projects)).catch(() => {}) }, [])

  // A plain bin gets its Library entry on the first save or download (DesignStep creates it), not on this click.
  const newBin = () => update({ view: 'bin', projectId: undefined, projectName: name.trim() || `Bin ${stamp()}`, tools: [], bin: { ...defaultBin, solid: false }, upload: undefined, rect: undefined, corners: undefined })
  const startTrace = () => update({ view: 'trace', step: 0, projectName: name.trim() || `Trace ${stamp()}`, tools: [], upload: undefined, rect: undefined, corners: undefined, projectId: undefined, bin: defaultBin })

  async function open(p: LibraryProject) {
    setErr(''); setOpening(p.id)
    try { const e = await openProject(p.id, update); if (e) setErr(e) } catch (x) { setErr(errMsg(x)) } finally { setOpening('') }
  }

  return (
    <>
      <div className="home">
        <div className="hero">
          <h1>What are we making?</h1>
          <p className="lead">Gridfinity bins and tool holders, generated on this machine. Your photos never leave it.</p>
          <label className="row" style={{ maxWidth: 460, gridTemplateColumns: 'auto 1fr' }}>Project name
            <input type="text" placeholder="e.g. Drawer 2 pliers" title="Optional. Leave it blank and we name it by the time you started" value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') startTrace() }} />
          </label>
          <div className="choices">
            <button className="choice" title="Photograph tools on a sheet of paper and get a holder with a pocket for each one" onClick={startTrace}>
              <div className="choiceicon"><CameraIcon /></div>
              <b>Trace tools from a photo</b>
              <span className="hint">Lay tools on a sheet of paper, shoot from above, and we trace a pocket that fits each tool. Then arrange them in a bin.</span>
            </button>
            <button className="choice" title="A plain bin: pick the size and options, then download" onClick={newBin}>
              <div className="choiceicon"><GridIcon /></div>
              <b>Regular Gridfinity bin</b>
              <span className="hint">Pick the size in 42 mm units, stacking lip, magnet or screw holes, dividers, scoop and label tab. Download STL, 3MF or STEP.</span>
            </button>
          </div>
          {err && <div className="status err">{err}</div>}
          <div className="howto">
            {TRACE_STEPS.map((n, i) => <div key={n}><b><span className="n">{i + 1}</span>{n}</b>{TRACE_STEP_HINTS[i]}.</div>)}
          </div>
        </div>
      </div>
      <aside className="panel">
        <h2>Recent projects</h2>
        {projects.length === 0 && <p className="hint">Your projects will show up here with a picture of the result.</p>}
        {opening && <div className="status" role="status"><span className="spinner" />Opening…</div>}
        <div className="miniCards">
          {projects.slice(0, 6).map((p) => {
            const exp = expiryNote(p)
            const openable = p.has_state || !p.has_photo
            return (
              <button key={p.id} className="mini" title={openable ? `Open ${p.name}` : `${p.name}: no saved layout yet, only the photo. Open it to start over from the photo`} disabled={!!opening} onClick={() => open(p)}>
                {p.has_snapshot ? <img src={`/api/library/${p.id}/snapshot?t=${p.updated_at}`} alt="" /> : p.has_photo ? <img src={`/api/library/${p.id}/thumb`} alt="" /> : <div className="noimg" aria-hidden="true">▦</div>}
                <div className="minibody">
                  <div className="mininame">{p.name}</div>
                  <div className="hint">{new Date(p.updated_at * 1000).toLocaleDateString()} · {p.exports.length} download{p.exports.length === 1 ? '' : 's'}</div>
                  {exp && <div className="hint expiry">{exp}</div>}
                </div>
              </button>
            )
          })}
        </div>
        {projects.length > 6 && <p className="hint">Showing the 6 most recent of {projects.length}.</p>}
        <div className="cta"><button className="secondary" title="See every project, with photos, downloads and delete" onClick={() => update({ view: 'library' })}>Open full library</button></div>
      </aside>
    </>
  )
}
