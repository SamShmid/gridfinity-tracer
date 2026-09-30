import { useRef, useState } from 'react'
import { api, errMsg, Poly } from '../api'
import type { Session } from '../types'
import { rectKeyOf } from '../types'
import { BusyOverlay, toast } from '../ui'
import { paperName } from '../util'

const CORNER_NAMES = ['Top-left corner', 'Top-right corner', 'Bottom-right corner', 'Bottom-left corner']
const LOUPE_PX = 150 // on-screen size of the loupe
const LOUPE_ZOOM = 3 // times the main canvas zoom

export default function PaperStep({ s, update }: { s: Session; update: (p: Partial<Session>) => void }) {
  const up = s.upload!
  const corners = s.corners ?? up.corners
  const svgRef = useRef<SVGSVGElement>(null)
  const [drag, setDrag] = useState<number | null>(null)
  const [focused, setFocused] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const imgSrc = `/api/image/upload/${up.image_id}`

  function toImage(e: React.PointerEvent): [number, number] {
    const svg = svgRef.current!
    const pt = svg.createSVGPoint()
    pt.x = e.clientX
    pt.y = e.clientY
    const p = pt.matrixTransform(svg.getScreenCTM()!.inverse())
    return [Math.max(0, Math.min(up.width, p.x)), Math.max(0, Math.min(up.height, p.y))]
  }
  const setCorner = (i: number, c: [number, number]) =>
    update({ corners: corners.map((x, j) => (j === i ? c : x)) as Poly })
  function move(e: React.PointerEvent) {
    if (drag === null) return
    setCorner(drag, toImage(e))
  }
  /** Arrow keys move the focused handle by 1 image px, shift makes it 10. */
  function nudge(e: React.KeyboardEvent, i: number) {
    const step = e.shiftKey ? 10 : 1
    const d: Record<string, [number, number]> = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, -step],
      ArrowDown: [0, step],
    }
    const v = d[e.key]
    if (!v) return
    e.preventDefault()
    const [x, y] = corners[i]
    setCorner(i, [Math.max(0, Math.min(up.width, x + v[0])), Math.max(0, Math.min(up.height, y + v[1]))])
  }
  // Nothing changed since the photo was last straightened: the traced tools still fit, so just move on.
  const rectKey = rectKeyOf({ ...s, corners })
  const unchanged = !!s.rect && s.rectKey === rectKey
  async function rectify() {
    if (unchanged) {
      update({ step: 2 })
      return
    }
    const n = s.tools.length
    if (
      n > 0 &&
      !confirm(
        `Straightening the photo again clears the ${n} traced tool${n === 1 ? '' : 's'}, since their outlines were drawn on the old version. Continue?`,
      )
    )
      return
    setBusy(true)
    setErr('')
    try {
      const rect = await api.rectify({
        image_id: up.image_id,
        corners,
        paper: s.paper,
        orientation: s.orientation,
        custom_w_mm: s.paper === 'custom' ? s.customW : undefined,
        custom_h_mm: s.paper === 'custom' ? s.customH : undefined,
      })
      update({ rect, rectKey, tools: [], step: 2 })
      if (n > 0) toast(`Cleared ${n} traced tool${n === 1 ? '' : 's'}`, 'info')
      api.samWarm(rect.rect_id).catch(() => {})
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setBusy(false)
    }
  }
  const labels = ['TL', 'TR', 'BR', 'BL']
  const r = Math.max(up.width, up.height) / 70

  // ---- loupe: the corner being dragged (or keyboard-focused), at 3x the canvas zoom
  const active = drag ?? focused
  const canvasScale = svgRef.current ? svgRef.current.getBoundingClientRect().width / up.width : 1
  const loupeSpan = LOUPE_PX / Math.max(1e-6, canvasScale * LOUPE_ZOOM) // image px shown across the loupe
  const sheet = s.paper === 'custom' ? `Custom · ${s.customW} × ${s.customH} mm` : paperName(s.paper)
  const orient = s.orientation === 'auto' ? 'auto' : s.orientation === 'landscape' ? 'wide' : 'tall'

  return (
    <>
      <div className="canvas">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${up.width} ${up.height}`}
          style={{ touchAction: 'none', width: '100%', height: '100%' }}
          onPointerMove={move}
          onPointerUp={() => setDrag(null)}
          onPointerCancel={() => setDrag(null)}
          onPointerLeave={() => setDrag(null)}
        >
          <image href={imgSrc} width={up.width} height={up.height} />
          <polygon
            points={corners.map((c) => c.join(',')).join(' ')}
            fill="var(--c-accent-soft)"
            stroke="var(--c-accent)"
            strokeWidth={r / 4}
          />
          {corners.map((c, i) => (
            <g
              key={i}
              role="button"
              tabIndex={0}
              aria-label={`${CORNER_NAMES[i]}. Drag it onto the paper's corner, or use the arrow keys`}
              style={{ cursor: 'grab', outline: 'none' }}
              onPointerDown={(e) => {
                try {
                  ;(e.target as Element).setPointerCapture?.(e.pointerId)
                } catch {}
                setDrag(i)
              }}
              onFocus={() => setFocused(i)}
              onBlur={() => setFocused(null)}
              onKeyDown={(e) => nudge(e, i)}
            >
              <title>{CORNER_NAMES[i]}: drag onto the paper's corner</title>
              <circle
                cx={c[0]}
                cy={c[1]}
                r={r}
                fill="var(--c-panel)"
                stroke={active === i ? 'var(--c-pocket)' : 'var(--c-accent)'}
                strokeWidth={active === i ? r / 2.5 : r / 4}
              />
              <text
                x={c[0]}
                y={c[1] + r * 0.35}
                fontSize={r}
                textAnchor="middle"
                fill="var(--c-accent)"
                fontWeight={700}
                style={{ pointerEvents: 'none' }}
              >
                {labels[i]}
              </text>
            </g>
          ))}
        </svg>
        {busy && <BusyOverlay text="Straightening the photo…" block />}
      </div>
      <aside className="panel">
        <h2>2. Paper corners</h2>
        <p>
          {up.detected
            ? 'We found the sheet. Check that each handle sits right on a paper corner.'
            : "We couldn't find the sheet. Drag each handle onto a corner of the paper."}{' '}
          The corners set the scale, so a few pixels matter.
        </p>
        <p className="hint">
          Tip: click a handle, then nudge it with the arrow keys (hold <kbd>shift</kbd> for 10 px).
        </p>
        {err && <div className="status err">{err}</div>}
        <h3>Sheet</h3>
        <div className="louperow">
          <div>
            <p style={{ margin: 0 }}>
              {sheet} · {orient}{' '}
              <button
                className="ghost"
                title="Go back and change the paper size or orientation"
                onClick={() => update({ step: 0 })}
              >
                Change
              </button>
            </p>
            {active !== null ? (
              <p className="hint">
                3× zoom on the {CORNER_NAMES[active].toLowerCase()}. Line the paper's corner up with the
                crosshair.
              </p>
            ) : (
              <p className="hint">A 3× zoom shows up here while you drag a handle.</p>
            )}
          </div>
          {active !== null && (
            <div className="loupe" aria-hidden="true">
              <svg
                viewBox={`${corners[active][0] - loupeSpan / 2} ${corners[active][1] - loupeSpan / 2} ${loupeSpan} ${loupeSpan}`}
              >
                <image href={imgSrc} width={up.width} height={up.height} />
                <polygon
                  points={corners.map((c) => c.join(',')).join(' ')}
                  fill="none"
                  stroke="var(--c-accent)"
                  strokeWidth={loupeSpan / 150}
                />
                <line
                  x1={corners[active][0] - loupeSpan / 2}
                  y1={corners[active][1]}
                  x2={corners[active][0] + loupeSpan / 2}
                  y2={corners[active][1]}
                  stroke="var(--c-pocket)"
                  strokeWidth={loupeSpan / 150}
                />
                <line
                  x1={corners[active][0]}
                  y1={corners[active][1] - loupeSpan / 2}
                  x2={corners[active][0]}
                  y2={corners[active][1] + loupeSpan / 2}
                  stroke="var(--c-pocket)"
                  strokeWidth={loupeSpan / 150}
                />
              </svg>
            </div>
          )}
        </div>
        <div className="cta">
          <button className="ghost" title="Back to the photo step" onClick={() => update({ step: 0 })}>
            ← Back
          </button>
          <button
            className="secondary"
            title="Put the handles back where we first found the sheet"
            onClick={() => update({ corners: up.corners })}
          >
            Reset
          </button>
          <button
            className="primary"
            disabled={busy}
            title={
              unchanged
                ? 'Nothing changed, so this just goes on to tracing'
                : s.tools.length
                  ? 'Straightens the photo again with these corners. This clears the traced tools, so you get asked first'
                  : 'Straightens the photo using these four corners, then moves on to tracing'
            }
            onClick={rectify}
          >
            {busy ? (
              <>
                <span className="spinner" />
                Straightening…
              </>
            ) : (
              'Looks right →'
            )}
          </button>
        </div>
      </aside>
    </>
  )
}
