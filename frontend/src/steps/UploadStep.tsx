import { useEffect, useRef, useState } from 'react'
import { api, errMsg, MAX_UPLOAD_MB } from '../api'
import type { Session } from '../types'
import { BusyOverlay } from '../ui'
import { numOr, paperName } from '../util'

const ACCEPT = 'image/*,.heic,.heif,.HEIC,.HEIF'
type Sizes = Record<string, { w_mm: number; h_mm: number }>
/** Same list the server ships (paper.py), used when it cannot be reached so the Sheet menu is never empty. */
const FALLBACK_SIZES: Sizes = {
  letter: { w_mm: 215.9, h_mm: 279.4 },
  legal: { w_mm: 215.9, h_mm: 355.6 },
  tabloid: { w_mm: 279.4, h_mm: 431.8 },
  a3: { w_mm: 297, h_mm: 420 },
  a4: { w_mm: 210, h_mm: 297 },
  a5: { w_mm: 148, h_mm: 210 },
}

export default function UploadStep({ s, update }: { s: Session; update: (p: Partial<Session>) => void }) {
  const [sizes, setSizes] = useState<Sizes>(FALLBACK_SIZES)
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false) // read by drop handlers so a second drop during an upload is ignored
  const [err, setErr] = useState('')
  const [over, setOver] = useState(false)
  const pickRef = useRef<HTMLInputElement>(null) // photo library / file picker (no capture)
  const cameraRef = useRef<HTMLInputElement>(null) // rear camera on phones; desktops fall back to the picker
  const sRef = useRef(s)
  sRef.current = s

  useEffect(() => {
    api
      .paperSizes()
      .then(setSizes)
      .catch((e) => setErr(`Could not reach the server for the paper list: ${errMsg(e)}`))
  }, [])

  // A photo dropped anywhere on the page (the panel, the header, the busy overlay) would otherwise make
  // the browser open the file and leave the app. Catch it at the window and treat it like the drop zone.
  useEffect(() => {
    const over = (e: DragEvent) => e.preventDefault()
    const drop = (e: DragEvent) => {
      e.preventDefault()
      setOver(false)
      handle(e.dataTransfer?.files[0])
    }
    window.addEventListener('dragover', over)
    window.addEventListener('drop', drop)
    return () => {
      window.removeEventListener('dragover', over)
      window.removeEventListener('drop', drop)
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  async function handle(file: File | undefined, input?: HTMLInputElement | null) {
    if (input) input.value = '' // so picking the same file again (or after a failure) fires onChange
    if (!file || busyRef.current) return
    if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
      setErr(`That photo is ${(file.size / 1e6).toFixed(0)} MB. The limit is ${MAX_UPLOAD_MB} MB.`)
      return
    }
    busyRef.current = true
    setBusy(true)
    setErr('')
    try {
      const cur = sRef.current
      const up = await api.upload(file, cur.paper, cur.customW, cur.customH, cur.projectName)
      update({
        upload: up,
        corners: up.corners,
        rect: undefined,
        rectKey: undefined,
        tools: [],
        step: 1,
        projectId: up.project_id,
      })
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }

  return (
    <>
      <div className="canvas">
        <div
          className={'dropzone' + (over ? ' over' : '')}
          onDragOver={(e) => {
            e.preventDefault()
            setOver(true)
          }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => {
            e.preventDefault()
            e.stopPropagation() // the window handler would upload it a second time
            setOver(false)
            handle(e.dataTransfer.files[0])
          }}
        >
          <p style={{ fontSize: 16, color: 'var(--c-ink)' }}>Drop a photo here, or</p>
          <div className="btnrow" style={{ justifyContent: 'center' }}>
            <button
              className="primary"
              disabled={busy}
              title="Pick a photo from this device"
              onClick={() => pickRef.current?.click()}
            >
              Upload a photo
            </button>
            <button
              className="secondary"
              disabled={busy}
              title="Open the camera (on a phone or tablet)"
              onClick={() => cameraRef.current?.click()}
            >
              Take a photo
            </button>
          </div>
          <input
            ref={pickRef}
            type="file"
            accept={ACCEPT}
            hidden
            aria-label="Choose a photo file"
            onChange={(e) => handle(e.target.files?.[0], e.target)}
          />
          <input
            ref={cameraRef}
            type="file"
            accept={ACCEPT}
            capture="environment"
            hidden
            aria-label="Take a photo with the camera"
            onChange={(e) => handle(e.target.files?.[0], e.target)}
          />
          {err && <div className="status err">{err}</div>}
          <p className="hint">
            JPEG, PNG, HEIC (iPhone), WebP, TIFF, up to {MAX_UPLOAD_MB} MB. The photo stays on this machine.
            Nothing is sent to the internet.
          </p>
        </div>
        {busy && <BusyOverlay text="Reading the photo and looking for the sheet…" block />}
      </div>
      <aside className="panel">
        <h2>1. Photo</h2>
        <label className="row">
          Project name
          <input
            type="text"
            placeholder="e.g. Drawer 2 pliers"
            title="What this project is called in the Library"
            value={s.projectName}
            onChange={(e) => update({ projectName: e.target.value })}
          />
        </label>
        <ol style={{ paddingLeft: 18, margin: '4px 0' }}>
          <li>
            Put a plain sheet of paper on a <b>dark, non-white surface</b>. All four corners must be visible.
          </li>
          <li>Lay the tools flat on the paper, not touching each other. Hanging over the edge is fine.</li>
          <li>
            Shoot <b>straight down</b>, from as high as you can, with the main (1×) camera. Zoom in rather
            than moving close.
          </li>
          <li>Even light, no hard shadows. Skip the flash.</li>
        </ol>
        <figure className="examplefig">
          <img
            className="example"
            src="/example.jpg"
            alt="Example photo: tools lying flat on a sheet of paper on a dark bench, shot from straight above"
          />
          <figcaption className="hint">
            Like this: sheet fully in frame, tools flat and not touching, shot straight down.
          </figcaption>
        </figure>
        <h3>Paper size</h3>
        <label className="row">
          Sheet
          <select
            value={s.paper}
            title="The paper under the tools. This sets the scale, so get it right"
            onChange={(e) => update({ paper: e.target.value })}
          >
            {Object.entries(sizes).map(([k, v]) => (
              <option key={k} value={k}>
                {paperName(k)} · {Math.round(v.w_mm)} × {Math.round(v.h_mm)} mm
              </option>
            ))}
            <option value="custom">Custom…</option>
          </select>
        </label>
        {s.paper === 'custom' && (
          <>
            <label className="row">
              Width (mm)
              <input
                type="number"
                step="0.1"
                min="50"
                max="1000"
                title="Short side of the sheet in millimetres, 50 to 1000"
                value={s.customW}
                onChange={(e) => update({ customW: numOr(e.target.value, s.customW, 50, 1000) })}
              />
            </label>
            <label className="row">
              Height (mm)
              <input
                type="number"
                step="0.1"
                min="50"
                max="1000"
                title="Long side of the sheet in millimetres, 50 to 1000"
                value={s.customH}
                onChange={(e) => update({ customH: numOr(e.target.value, s.customH, 50, 1000) })}
              />
            </label>
          </>
        )}
        <label className="row">
          Sheet in the photo
          <select
            value={s.orientation}
            title="Which way the sheet lies in the photo. Auto works for almost every shot"
            onChange={(e) => update({ orientation: e.target.value as Session['orientation'] })}
          >
            <option value="auto">Auto</option>
            <option value="landscape">Wide (landscape)</option>
            <option value="portrait">Tall (portrait)</option>
          </select>
        </label>
        <p className="hint">The sheet size is what makes the pockets the right size. Double-check it.</p>
      </aside>
    </>
  )
}
