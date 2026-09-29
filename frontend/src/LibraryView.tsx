import { useEffect, useRef, useState } from 'react'
import { api, errMsg, LibraryExport, LibraryProject } from './api'
import StlViewer from './StlViewer'
import type { Session } from './types'
import { openProject } from './project'
import { toast } from './ui'
import { expiryNote, paperName } from './util'

const fmtDate = (t: number) => new Date(t * 1000).toLocaleString()
const fmtSize = (b: number) => (b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.round(b / 1e3)} KB`)

export default function LibraryView({ update }: { update: (p: Partial<Session>) => void }) {
  const [projects, setProjects] = useState<LibraryProject[]>([])
  const [err, setErr] = useState('')
  const [viewing, setViewing] = useState<{ exp: LibraryExport; blob: Blob } | null>(null)
  const [stats, setStats] = useState('')
  const [busy, setBusy] = useState('')
  const nameBefore = useRef('')   // the name when the rename box got focus, so blur only saves a real change

  const refresh = () => api.library().then((r) => setProjects(r.projects)).catch((e) => setErr(errMsg(e)))
  useEffect(() => { refresh() }, [])

  async function view(exp: LibraryExport) {
    if (exp.format !== 'stl') { setErr('Only STL files can be previewed here. Download the 3MF or STEP instead.'); return }
    setBusy('Loading the model…'); setErr('')
    try { setViewing({ exp, blob: await api.exportBlob(exp.id) }) }
    catch (e) { setErr(errMsg(e)) } finally { setBusy('') }
  }

  async function reopen(p: LibraryProject) {
    setBusy(`Opening ${p.name}…`); setErr('')
    try { const e = await openProject(p.id, update); if (e) setErr(e) } catch (e) { setErr(errMsg(e)) } finally { setBusy('') }
  }

  async function rename(p: LibraryProject, name: string) {
    const n = name.trim()
    if (!n || n === nameBefore.current) { if (!n) setProjects((ps) => ps.map((x) => (x.id === p.id ? { ...x, name: nameBefore.current } : x))); return }
    try { await api.libraryRename(p.id, n); toast(`Renamed to ${n}`) }
    catch (e) { setErr(errMsg(e)); return }
    setProjects((ps) => ps.map((x) => (x.id === p.id ? { ...x, name: n } : x)))
  }
  async function remove(p: LibraryProject) {
    if (!confirm(`Delete "${p.name}" and its ${p.exports.length} download(s)? This cannot be undone.`)) return
    try { await api.libraryDelete(p.id) } catch (e) { setErr(errMsg(e)); return }
    if (viewing && p.exports.some((e) => e.id === viewing.exp.id)) setViewing(null)
    toast(`Deleted ${p.name}`)
    refresh()
  }

  return (
    <>
      <div className="library">
        {viewing && (
          <div className="viewerbox">
            <div className="viewerhead">
              <b>{viewing.exp.filename}</b> <span className="hint">{viewing.exp.summary} · {stats}</span>
              <span style={{ marginLeft: 'auto', display: 'inline-flex', gap: 4 }}>
                <a className="ghost" title="Download this file" href={`/api/library/exports/${viewing.exp.id}`}>Download</a>
                <button className="ghost" title="Close the 3D preview" onClick={() => setViewing(null)}>Close</button>
              </span>
            </div>
            <StlViewer data={viewing.blob} onStats={setStats} minHeight={380} />
          </div>
        )}
        {busy && <div className="status" role="status"><span className="spinner" />{busy}</div>}
        {err && <div className="status err">{err}</div>}
        {projects.length === 0 && !err && <p className="hint" style={{ padding: 24 }}>Nothing here yet. Every photo you upload and every model you download is kept here.</p>}
        <div className="cards">
          {projects.map((p) => {
            const exp = expiryNote(p)
            const openable = p.has_state || !p.has_photo
            return (
              <div key={p.id} className="card">
                <div className="cardimgs">
                  {p.has_snapshot && <img src={`/api/library/${p.id}/snapshot?t=${p.updated_at}`} alt="3D snapshot" />}
                  {p.has_photo && <img src={`/api/library/${p.id}/thumb`} alt="Photo" />}
                  {!p.has_snapshot && !p.has_photo && <div className="noimg" aria-hidden="true">▦</div>}
                </div>
                <div className="cardbody">
                  <input className="cardname" aria-label="Project name" title="Click to rename" value={p.name}
                    onFocus={() => { nameBefore.current = p.name }}
                    onChange={(e) => setProjects((ps) => ps.map((x) => (x.id === p.id ? { ...x, name: e.target.value } : x)))}
                    onBlur={(e) => rename(p, e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }} />
                  <div className="hint">{fmtDate(p.created_at)}{p.has_photo ? ` · ${paperName(p.paper)} sheet · ${p.width}×${p.height}px` : ' · plain bin'}</div>
                  {exp && <div className="hint expiry">{exp}. Open it to keep it.</div>}
                  {p.has_photo && <div className="hint">Original photo: <a href={`/api/library/${p.id}/original`} title="Download the photo exactly as uploaded">{p.original_filename}</a> ({fmtSize(p.original_size)})</div>}
                  {p.exports.length > 0 && (
                    <ul className="exports">
                      {p.exports.map((e) => (
                        <li key={e.id}>
                          <span className="fmt">{e.format.toUpperCase()}</span> <a href={`/api/library/exports/${e.id}`} title="Download this file again">{e.filename}</a>
                          <span className="hint"> {fmtSize(e.size)} · {fmtDate(e.created_at)} · {e.summary}</span>
                          {e.format === 'stl' && <button className="ghost" title="Show this model in 3D" onClick={() => view(e)}>View in 3D</button>}
                        </li>
                      ))}
                    </ul>
                  )}
                  {p.exports.length === 0 && <div className="hint">No downloads yet.</div>}
                  {!openable && <div className="hint">Can't open: no layout was saved for this photo. Upload the photo again to start over.</div>}
                  <div className="btnrow" style={{ marginBottom: 0 }}>
                    <button className="secondary" disabled={!openable || !!busy} title={openable ? 'Reopen this project to change it or download again' : 'No saved layout yet'} onClick={() => reopen(p)}>Open</button>
                    <button className="ghost" title="Delete this project, its photo and its downloads" onClick={() => remove(p)}>Delete</button>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      </div>
      <aside className="panel">
        <h2>Library</h2>
        <p>Everything you have made. The original photo is kept exactly as uploaded (HEIC included). Each download is kept with its bin settings, so you can reopen a project, tweak it, and download again.</p>
        <p className="hint">Projects that sit untouched for a while are cleaned up automatically. Opening one keeps it around.</p>
        <div className="cta"><button className="secondary" title="Reload the list from the server" onClick={() => { setErr(''); refresh().then(() => toast('List refreshed', 'info', 2000)) }}>Refresh list</button></div>
      </aside>
    </>
  )
}
