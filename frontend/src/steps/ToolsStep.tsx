import { useEffect, useRef, useState } from 'react'
import { api, errMsg, Poly, ToolOutline } from '../api'
import { toPath } from '../geom'
import type { Session, Tool } from '../types'
import { depthFor } from '../types'
import { BusyOverlay, Tip, toast } from '../ui'
import { numOr, sourceLabel, uid } from '../util'

type SamPt = { x: number; y: number; label: number }
type PointMode = 'add' | 'exclude'
type VertexRef = { tool: string; idx: number }

/** rect_ids we already auto-detected for, so coming back to this step does not re-run it. */
const autoRanFor = new Set<string>()

export default function ToolsStep({ s, update }: { s: Session; update: (p: Partial<Session>) => void }) {
  const rect = s.rect!
  const ppm = rect.px_per_mm
  const svgRef = useRef<SVGSVGElement>(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [samPts, setSamPts] = useState<SamPt[]>([])
  const [samPreview, setSamPreview] = useState<ToolOutline | null>(null)
  const [sel, setSel] = useState<string | null>(null)
  const [selV, setSelV] = useState<VertexRef | null>(null)
  const [dragV, setDragV] = useState<VertexRef | null>(null)
  const [showOffset, setShowOffset] = useState(true)
  const [pointMode, setPointMode] = useState<PointMode>('add')

  // Always read tools through the ref inside async / late handlers: the closure's `s.tools` goes stale
  // the moment an await resolves. setTools updates the ref immediately so back-to-back calls compose.
  const toolsRef = useRef(s.tools)
  toolsRef.current = s.tools
  const tools = s.tools
  const setTools = (t: Tool[]) => {
    toolsRef.current = t
    update({ tools: t })
  }
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  function toPx(e: React.PointerEvent | React.MouseEvent): [number, number] {
    const svg = svgRef.current!
    const pt = svg.createSVGPoint()
    pt.x = e.clientX
    pt.y = e.clientY
    const p = pt.matrixTransform(svg.getScreenCTM()!.inverse())
    return [p.x, p.y]
  }
  const mm2px = (p: Poly) => p.map(([x, y]) => [x * ppm, y * ppm] as [number, number])

  async function makeTool(o: ToolOutline, name: string): Promise<Tool> {
    const base = {
      clearance: 0.5,
      printerOffset: 0.2,
      gaussian: 1.0,
      tolerance: 0.3,
      smooth: 0,
      bridge: 1.5,
      snap: false,
      symmetric: false,
      convex: false,
    }
    const off = await api.offset({
      polygon: o.polygon,
      clearance_mm: base.clearance,
      printer_offset_mm: base.printerOffset,
      gaussian_mm: base.gaussian,
      tolerance_mm: base.tolerance,
    })
    const t: Tool = {
      id: uid(),
      name,
      source: o.source,
      raw: o.polygon,
      offset: off.polygon,
      ...base,
      thickness: 12,
      sit: 'threeq',
      depth: 9,
      x: 0,
      y: 0,
      rot: 0,
      straightRot: off.straighten_deg,
      fingerHoles: [],
    }
    t.depth = depthFor(t)
    return t
  }

  async function detect(method: 'auto' | 'classical') {
    setBusy('Looking for tools…')
    setErr('')
    try {
      const r = await api.detectAuto(rect.rect_id, method)
      const good = r.tools.filter((o) => o.polygon.length >= 3)
      if (!good.length)
        setErr(
          method === 'auto'
            ? 'No tools found. Try "Simple contrast", or click a tool in the photo to trace it by hand.'
            : 'No tools found. Try "Find tools", or click a tool in the photo to trace it by hand.',
        )
      const made: Tool[] = []
      for (const o of good) made.push(await makeTool(o, `Tool ${toolsRef.current.length + made.length + 1}`))
      if (made.length) setTools([...toolsRef.current, ...made])
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      if (mounted.current) setBusy('')
    }
  }

  // Auto-run detection the first time we land here with nothing traced yet.
  useEffect(() => {
    if (toolsRef.current.length === 0 && !autoRanFor.has(rect.rect_id)) {
      autoRanFor.add(rect.rect_id)
      detect('auto')
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const samSeq = useRef(0)
  async function samClick(pts: SamPt[]) {
    setSamPts(pts)
    const seq = ++samSeq.current
    if (!pts.length) {
      setSamPreview(null)
      return
    }
    setBusy('Tracing…')
    setErr('')
    try {
      const r = await api.detectSam(rect.rect_id, pts)
      if (seq !== samSeq.current) return
      setSamPreview(r.tools[0] ?? null)
      if (!r.tools.length) setErr('Nothing traced here. Click another spot on the tool.')
    } catch (e) {
      if (seq === samSeq.current) setErr(errMsg(e))
    } finally {
      if (seq === samSeq.current && mounted.current) setBusy('')
    }
  }
  async function acceptSam() {
    const preview = samPreview
    if (!preview) return
    setBusy('Cleaning up the outline…')
    setErr('')
    try {
      const t = await makeTool(preview, `Tool ${toolsRef.current.length + 1}`)
      setTools([...toolsRef.current, t])
      setSamPts([])
      setSamPreview(null)
      setSel(t.id)
      setSelV(null)
      toast(`Added ${t.name}`)
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      if (mounted.current) setBusy('')
    }
  }

  function onCanvasClick(e: React.MouseEvent) {
    if (dragV || busy) return
    const [x, y] = toPx(e)
    const exclude = e.shiftKey || e.button === 2 || pointMode === 'exclude'
    samClick([...samPts, { x, y, label: exclude ? 0 : 1 }])
  }

  // ---- outline recomputation, one in-flight counter per tool so a slow older answer never overwrites a newer one
  const offsetSeq = useRef<Record<string, number>>({})
  async function reoffset(t: Tool, patch: Partial<Tool>) {
    const nt = { ...t, ...patch }
    const seq = (offsetSeq.current[t.id] = (offsetSeq.current[t.id] ?? 0) + 1)
    // optimistic: apply the option now, swap in the recomputed outline when it arrives
    setTools(toolsRef.current.map((x) => (x.id === t.id ? nt : x)))
    try {
      const off = await api.offset({
        polygon: nt.raw,
        clearance_mm: nt.clearance,
        printer_offset_mm: nt.printerOffset,
        gaussian_mm: nt.gaussian,
        tolerance_mm: nt.tolerance,
        smooth_mm: nt.smooth,
        bridge_mm: nt.bridge,
        snap: nt.snap,
        symmetric: nt.symmetric,
        convex: nt.convex,
      })
      if (offsetSeq.current[t.id] !== seq) return
      if (!toolsRef.current.some((x) => x.id === t.id)) return // removed meanwhile
      setTools(
        toolsRef.current.map((x) =>
          x.id === t.id ? { ...x, offset: off.polygon, straightRot: off.straighten_deg } : x,
        ),
      )
    } catch (e) {
      if (offsetSeq.current[t.id] === seq) setErr(errMsg(e))
    }
  }
  const patchTool = (id: string, patch: Partial<Tool>) =>
    setTools(toolsRef.current.map((x) => (x.id === id ? { ...x, ...patch } : x)))
  const findTool = (id: string) => toolsRef.current.find((x) => x.id === id)
  function removeTool(id: string) {
    const cur = toolsRef.current
    const idx = cur.findIndex((x) => x.id === id)
    if (idx < 0) return
    const t = cur[idx]
    setTools(cur.filter((x) => x.id !== id))
    if (sel === id) setSel(null)
    if (selV?.tool === id) setSelV(null)
    toast(`Removed ${t.name}`, 'info', 5000, {
      label: 'Undo',
      title: 'Put the tool back',
      onClick: () => {
        const now = toolsRef.current
        if (now.some((x) => x.id === id)) return
        const at = Math.min(idx, now.length)
        setTools([...now.slice(0, at), t, ...now.slice(at)])
        toast(`Restored ${t.name}`)
      },
    })
  }

  // ---- vertex editing (raw polygon, mm)
  function deleteVertex(v: VertexRef) {
    const t = findTool(v.tool)
    if (!t || t.raw.length <= 3) return
    setSelV(null)
    reoffset(t, { raw: t.raw.filter((_, i) => i !== v.idx) })
  }
  /** Insert a vertex at the midpoint of the longest edge (of the two touching the selected vertex when there is one). */
  function addVertex(toolId: string, near?: VertexRef | null) {
    const t = findTool(toolId)
    if (!t || t.raw.length < 3) return
    const n = t.raw.length
    const edgeLen = (i: number) => {
      const [x0, y0] = t.raw[i],
        [x1, y1] = t.raw[(i + 1) % n]
      return Math.hypot(x1 - x0, y1 - y0)
    }
    let candidates = Array.from({ length: n }, (_, i) => i)
    if (near && near.tool === toolId) candidates = [(near.idx - 1 + n) % n, near.idx]
    const i = candidates.reduce((a, b) => (edgeLen(b) > edgeLen(a) ? b : a))
    const [x0, y0] = t.raw[i],
      [x1, y1] = t.raw[(i + 1) % n]
    const raw = [...t.raw]
    raw.splice(i + 1, 0, [(x0 + x1) / 2, (y0 + y1) / 2])
    setSelV({ tool: toolId, idx: i + 1 })
    reoffset(t, { raw })
  }
  function vertexDown(e: React.PointerEvent, toolId: string, idx: number) {
    e.stopPropagation()
    try {
      ;(e.target as Element).setPointerCapture?.(e.pointerId)
    } catch {}
    if (e.altKey) {
      deleteVertex({ tool: toolId, idx })
      return
    }
    setSel(toolId)
    setSelV({ tool: toolId, idx })
    setDragV({ tool: toolId, idx })
  }
  function vertexMove(e: React.PointerEvent) {
    if (!dragV) return
    const t = findTool(dragV.tool)
    if (!t) return
    const [x, y] = toPx(e)
    patchTool(dragV.tool, { raw: t.raw.map((p, i) => (i === dragV.idx ? [x / ppm, y / ppm] : p)) })
  }
  function vertexUp() {
    if (!dragV) return
    const t = findTool(dragV.tool)
    setDragV(null)
    if (t) reoffset(t, {})
  }
  function edgeDblClick(e: React.MouseEvent, toolId: string, idx: number) {
    e.stopPropagation()
    const t = findTool(toolId)
    if (!t) return
    const [x, y] = toPx(e)
    const raw = [...t.raw]
    raw.splice(idx + 1, 0, [x / ppm, y / ppm])
    setSelV({ tool: toolId, idx: idx + 1 })
    reoffset(t, { raw })
  }
  async function refine(t: Tool) {
    setBusy('Tightening the outline…')
    setErr('')
    try {
      const r = await api.refine(rect.rect_id, t.raw)
      const cur = findTool(t.id)
      if (r.tools[0] && r.tools[0].polygon.length >= 3 && cur) {
        setSelV(null)
        await reoffset(cur, { raw: r.tools[0].polygon, source: r.tools[0].source })
        toast(`Re-traced ${cur.name}`)
      } else setErr('Could not tighten this outline. Try moving a few points by hand instead.')
    } catch (x) {
      setErr(errMsg(x))
    } finally {
      if (mounted.current) setBusy('')
    }
  }

  useEffect(() => {
    document.addEventListener('contextmenu', prevent)
    return () => document.removeEventListener('contextmenu', prevent)
  }, [])
  const prevent = (e: Event) => {
    if ((e.target as Element).closest?.('.canvas')) e.preventDefault()
  }

  const selTool = tools.find((t) => t.id === sel)
  const selVertex = selV && selV.tool === sel && selTool && selV.idx < selTool.raw.length ? selV : null
  const [px0, py0, px1, py1] = rect.paper_px
  const R = Math.max(rect.width, rect.height) / 160
  const num = (e: React.ChangeEvent<HTMLInputElement>, prev: number) => numOr(e.target.value, prev)

  return (
    <>
      <div className="canvas">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${rect.width} ${rect.height}`}
          style={{
            touchAction: 'none',
            width: '100%',
            height: '100%',
            cursor: busy ? 'progress' : 'crosshair',
            pointerEvents: busy ? 'none' : undefined,
          }}
          onClick={onCanvasClick}
          onPointerMove={vertexMove}
          onPointerUp={vertexUp}
          onContextMenu={(e) => {
            e.preventDefault()
            onCanvasClick(e)
          }}
        >
          <image href={`/api/image/rect/${rect.rect_id}`} width={rect.width} height={rect.height} />
          <rect
            x={px0}
            y={py0}
            width={px1 - px0}
            height={py1 - py0}
            fill="none"
            stroke="var(--c-accent)"
            strokeOpacity={0.5}
            strokeDasharray={`${R * 2} ${R * 2}`}
            strokeWidth={R / 2}
          />
          {tools.map((t) => (
            <g
              key={t.id}
              onClick={(e) => {
                e.stopPropagation()
                setSel(t.id)
                if (selV?.tool !== t.id) setSelV(null)
              }}
              style={{ cursor: 'pointer' }}
            >
              {showOffset && (
                <path
                  d={toPath(mm2px(t.offset))}
                  fill="none"
                  stroke="var(--c-pocket)"
                  strokeWidth={R / 2}
                  strokeDasharray={`${R} ${R}`}
                />
              )}
              <path
                d={toPath(mm2px(t.raw))}
                fill="var(--c-accent-soft)"
                fillOpacity={t.id === sel ? 1 : 0.55}
                stroke="var(--c-trace)"
                strokeWidth={t.id === sel ? R * 0.8 : R / 2}
              />
              {t.id === sel &&
                mm2px(t.raw).map((p, i, arr) => {
                  const q = arr[(i + 1) % arr.length]
                  const isSel = selVertex?.idx === i
                  return (
                    <g key={i}>
                      <line
                        x1={p[0]}
                        y1={p[1]}
                        x2={q[0]}
                        y2={q[1]}
                        stroke="transparent"
                        strokeWidth={R * 3}
                        onDoubleClick={(e) => edgeDblClick(e, t.id, i)}
                      />
                      <circle
                        cx={p[0]}
                        cy={p[1]}
                        r={isSel ? R * 1.6 : R * 1.2}
                        fill={isSel ? 'var(--c-accent)' : 'var(--c-panel)'}
                        stroke="var(--c-trace)"
                        strokeWidth={R / 3}
                        style={{ cursor: 'grab' }}
                        onPointerDown={(e) => vertexDown(e, t.id, i)}
                        onClick={(e) => e.stopPropagation()}
                      />
                    </g>
                  )
                })}
            </g>
          ))}
          {samPreview && (
            <path
              d={toPath(mm2px(samPreview.polygon))}
              fill="var(--c-ok)"
              fillOpacity={0.25}
              stroke="var(--c-ok)"
              strokeWidth={R / 2}
            />
          )}
          {samPts.map((p, i) => (
            <circle
              key={i}
              cx={p.x}
              cy={p.y}
              r={R * 1.5}
              fill={p.label ? 'var(--c-ok)' : 'var(--c-danger)'}
              stroke="var(--c-panel)"
              strokeWidth={R / 3}
            />
          ))}
        </svg>
        {busy && <BusyOverlay text={busy} block />}
        {tools.length === 0 && !busy && samPts.length === 0 && (
          <Tip id="trace" className="float top">
            Click a tool to trace it, or use Find tools.
          </Tip>
        )}
      </div>
      <aside className="panel">
        <h2>3. Trace tools</h2>
        <div className="btnrow">
          <button
            className="primary"
            disabled={!!busy}
            title="Looks at the whole photo and traces every tool it can find"
            onClick={() => detect('auto')}
          >
            Find tools
          </button>
          <button
            className="secondary"
            disabled={!!busy}
            title="Faster fallback with no AI: picks out dark tools against the white sheet by contrast. Try it when Find tools misses something"
            onClick={() => detect('classical')}
          >
            Simple contrast (no AI)
          </button>
        </div>
        <p>
          Or <b>click a tool</b> in the photo to trace just that one. Click more spots to grow the outline.
          Use Exclude point (or <kbd>shift</kbd>+click / right-click) to carve a spot out.
        </p>
        <div className="seg" role="radiogroup" aria-label="What a click on the photo does">
          <button
            type="button"
            role="radio"
            aria-checked={pointMode === 'add'}
            className={pointMode === 'add' ? 'on' : ''}
            title="A click marks a spot that is part of the tool"
            onClick={() => setPointMode('add')}
          >
            Add point
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={pointMode === 'exclude'}
            className={pointMode === 'exclude' ? 'on' : ''}
            title="A click marks a spot that is not part of the tool"
            onClick={() => setPointMode('exclude')}
          >
            Exclude point
          </button>
        </div>
        {(samPts.length > 0 || samPreview) && (
          <div className="btnrow">
            <button
              className="primary"
              disabled={!samPreview || !!busy}
              title="Keep the green outline as a new tool"
              onClick={acceptSam}
            >
              Add this tool
            </button>
            <button
              className="secondary"
              title="Forget the points and the green outline"
              onClick={() => samClick([])}
            >
              Clear points
            </button>
          </div>
        )}
        {busy && (
          <div className="status" role="status">
            <span className="spinner" />
            {busy}
          </div>
        )}
        {err && <div className="status err">{err}</div>}
        <div className="legend">
          <span>
            <i style={{ background: 'var(--c-trace)' }} />
            Traced outline
          </span>
          <span>
            <i style={{ background: 'var(--c-pocket)' }} />
            Pocket (with clearance)
          </span>
        </div>
        <label className="row chk" style={{ marginTop: 8 }}>
          Show pocket outline
          <input
            type="checkbox"
            checked={showOffset}
            title="Draw the orange pocket outline (the traced shape plus clearance) on the photo"
            onChange={(e) => setShowOffset(e.target.checked)}
          />
        </label>

        <h3>Tools ({tools.length})</h3>
        {tools.length === 0 && !busy && (
          <p className="hint">No tools yet. Use Find tools, or click a tool in the photo.</p>
        )}
        {tools.map((t) => (
          <div key={t.id} className={'toolcard' + (t.id === sel ? ' sel' : '')} onClick={() => setSel(t.id)}>
            <div className="name">
              <input
                type="text"
                aria-label="Tool name"
                title="Name this tool (shown on the pocket)"
                value={t.name}
                onChange={(e) => patchTool(t.id, { name: e.target.value })}
              />
              <button
                className="ghost"
                title="Remove this tool (you get 5 seconds to undo)"
                onClick={(e) => {
                  e.stopPropagation()
                  removeTool(t.id)
                }}
              >
                Remove
              </button>
            </div>
            <div className="hint meta">
              <span title={`${t.source} · ${Math.round(area(t.raw))} mm² · ${t.raw.length} points`}>
                {sourceLabel(t.source)} · {Math.round(area(t.raw) / 100)} cm²
              </span>
              <button
                className="ghost"
                disabled={!!busy}
                title="Runs the AI again on a zoomed-in crop to tighten this outline"
                onClick={(e) => {
                  e.stopPropagation()
                  refine(t)
                }}
              >
                Re-trace
              </button>
            </div>
            {t.id === sel && (
              <>
                <label className="row">
                  Fit clearance (mm)
                  <input
                    type="number"
                    step="0.1"
                    min="-2"
                    max="10"
                    value={t.clearance}
                    title="Gap between the tool and the pocket wall. 0.3 to 0.5 mm is snug, 1 to 1.5 mm drops in easily"
                    onChange={(e) => reoffset(t, { clearance: num(e, t.clearance) })}
                  />
                </label>
                <label className="row">
                  Printer compensation (mm)
                  <input
                    type="number"
                    step="0.05"
                    min="0"
                    max="1"
                    value={t.printerOffset}
                    title="Extra room for the printer's over-extrusion. 0.2 mm is typical"
                    onChange={(e) => reoffset(t, { printerOffset: num(e, t.printerOffset) })}
                  />
                </label>
                <label className="row">
                  Smoothing (mm)
                  <input
                    type="number"
                    step="0.25"
                    min="0"
                    max="5"
                    value={t.gaussian}
                    title="Irons out pixel wobble along the outline. 1 mm is typical, 0 keeps every bump"
                    onChange={(e) => reoffset(t, { gaussian: num(e, t.gaussian) })}
                  />
                </label>
                <label className="row">
                  Bridge gaps under (mm)
                  <input
                    type="number"
                    step="0.5"
                    min="0"
                    max="10"
                    value={t.bridge * 2}
                    title="Fills slots narrower than this (between open jaws, say) that would print as fragile slivers. 3 mm is typical"
                    onChange={(e) => reoffset(t, { bridge: num(e, t.bridge * 2) / 2 })}
                  />
                </label>
                <label className="row chk">
                  Straighten edges
                  <input
                    type="checkbox"
                    checked={t.snap}
                    title="Squares up nearly straight edges, like handles"
                    onChange={(e) => reoffset(t, { snap: e.target.checked })}
                  />
                </label>
                <label className="row chk">
                  Mirror-symmetric
                  <input
                    type="checkbox"
                    checked={t.symmetric}
                    title="Mirrors the outline about its long axis. Good for screwdrivers and wrenches"
                    onChange={(e) => reoffset(t, { symmetric: e.target.checked })}
                  />
                </label>
                <label className="row chk">
                  Convex hull
                  <input
                    type="checkbox"
                    checked={t.convex}
                    title="Wraps the outline like a rubber band, so there are no inward dents"
                    onChange={(e) => reoffset(t, { convex: e.target.checked })}
                  />
                </label>
                <label className="row">
                  Tool thickness (mm)
                  <input
                    type="number"
                    step="0.5"
                    min="1"
                    max="100"
                    value={t.thickness}
                    title="How tall the tool is lying flat. Sets how deep the pocket goes"
                    onChange={(e) => {
                      const nt = { ...t, thickness: num(e, t.thickness) }
                      patchTool(t.id, { thickness: nt.thickness, depth: depthFor(nt) })
                    }}
                  />
                </label>
                <details>
                  <summary className="hint" title="Two more knobs you rarely need">
                    Advanced
                  </summary>
                  <label className="row">
                    Simplify (mm)
                    <input
                      type="number"
                      step="0.1"
                      min="0"
                      max="3"
                      value={t.tolerance}
                      title="Drops outline points that move the shape by less than this. 0.3 mm is typical"
                      onChange={(e) => reoffset(t, { tolerance: num(e, t.tolerance) })}
                    />
                  </label>
                  <label className="row">
                    De-spike radius (mm)
                    <input
                      type="number"
                      step="0.5"
                      min="0"
                      max="10"
                      value={t.smooth}
                      title="Rounds off thin spikes smaller than this radius. 0 leaves them"
                      onChange={(e) => reoffset(t, { smooth: num(e, t.smooth) })}
                    />
                  </label>
                </details>
                <h3>Outline points</h3>
                <div className="btnrow">
                  <button
                    className="secondary"
                    disabled={!!busy}
                    title="Adds a point in the middle of the longest edge (next to the selected point, if there is one)"
                    onClick={(e) => {
                      e.stopPropagation()
                      addVertex(t.id, selVertex)
                    }}
                  >
                    Add point
                  </button>
                  <button
                    className="secondary"
                    disabled={!!busy || !selVertex || t.raw.length <= 3}
                    title={selVertex ? 'Deletes the selected point' : 'Select a point on the outline first'}
                    onClick={(e) => {
                      e.stopPropagation()
                      if (selVertex) deleteVertex(selVertex)
                    }}
                  >
                    Delete point
                  </button>
                </div>
                <p className="hint">
                  {selVertex
                    ? `Point ${selVertex.idx + 1} of ${t.raw.length} selected. `
                    : 'Click a point on the outline to select it. '}
                  Drag a point to move it. Shortcuts: double-click an edge to add a point, <kbd>alt</kbd>
                  +click a point to delete it.
                </p>
              </>
            )}
          </div>
        ))}
        <div className="cta">
          <button className="ghost" title="Back to the paper corners" onClick={() => update({ step: 1 })}>
            ← Back
          </button>
          <button
            className="primary"
            disabled={tools.length === 0 || !!busy}
            title={
              tools.length === 0
                ? 'Trace at least one tool first'
                : busy
                  ? 'Wait for tracing to finish'
                  : 'Arrange the pockets in a bin and download the model'
            }
            onClick={() => update({ step: 3 })}
          >
            Design the holder →
          </button>
        </div>
      </aside>
    </>
  )
}

function area(p: Poly) {
  let a = 0
  for (let i = 0; i < p.length; i++) {
    const [x0, y0] = p[i],
      [x1, y1] = p[(i + 1) % p.length]
    a += x0 * y1 - x1 * y0
  }
  return Math.abs(a) / 2
}
