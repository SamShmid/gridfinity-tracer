import { useEffect, useMemo, useRef, useState } from 'react'
import type { BinIn, PocketIn, Poly } from '../api'
import { api, errMsg, isAbort } from '../api'
import {
  bbox,
  centroid,
  circlePoly,
  nearestS,
  pointAt,
  pointInPoly,
  recentre,
  toPath,
  transform,
} from '../geom'
import { snapshot } from '../project'
import StlViewer, { DragApi, StlViewerHandle } from '../StlViewer'
import type { FingerHole, Session, Sit, Tool } from '../types'
import { depthFor, maxPocketDepth } from '../types'
import { Tip, toast } from '../ui'
import { numOr, safeFileName } from '../util'

const PITCH = 42
const MARGIN = 3
/** mm of wall to keep around any cut. The stacking lip's inner face sits 2.6 mm in, so keep 3 mm when there is a lip. */
const wallKeepFor = (bin: BinIn) => (bin.lip === 'regular' ? 3.0 : 2.0)

export function placedPolygon(t: Tool): Poly {
  return transform(recentre(t.offset), t.rot, t.x, t.y)
}
/** World centre of a notch: the point at fraction s along the tool's pocket outline. */
function placedCut(t: Tool, h: FingerHole) {
  const [lx, ly] = pointAt(recentre(t.offset), h.s)
  const [x, y] = transform([[lx, ly]], t.rot, t.x, t.y)[0]
  return { x, y, r: h.diameter / 2 }
}
function cutOutline(c: { x: number; y: number; r: number }): Poly {
  return circlePoly(c.x, c.y, c.r)
}
function pockets(tools: Tool[]): PocketIn[] {
  return tools.map((t) => ({
    polygon: placedPolygon(t),
    depth: t.depth,
    finger_holes: t.fingerHoles.map((h) => {
      const c = placedCut(t, h)
      return {
        x: c.x,
        y: c.y,
        diameter: h.diameter,
        length: h.diameter,
        angle_deg: 0,
        extra_depth: h.extraDepth,
      }
    }),
  }))
}
/** Perimeter fractions of the two outline points where the line through the centroid,
 *  perpendicular to the tool's long axis, crosses the outline (one per side). */
function pinchPositions(t: Tool, along = 0.5): [number, number] {
  const local = recentre(t.offset)
  const b = bbox(transform(local, t.straightRot, 0, 0))
  // pick the point along the (horizontal) long axis, then the perpendicular line there
  const ax = b.minx + b.w * along
  const a = (-t.straightRot * Math.PI) / 180
  const axis: [number, number] = [Math.cos(a), Math.sin(a)],
    nrm: [number, number] = [-Math.sin(a), Math.cos(a)]
  const [cx, cy] = centroid(local)
  const shift = ax - (b.minx + b.w / 2)
  const px = cx + axis[0] * shift,
    py = cy + axis[1] * shift
  // Sample the outline; keep points near the perpendicular line, then take the OUTERMOST one on
  // each side (so on pliers the pair lands on the outer edges of both handles, not inside the V).
  const N = 800
  const near: { s: number; side: number; d: number }[] = []
  for (let i = 0; i < N; i++) {
    const s = i / N
    const [x, y] = pointAt(local, s)
    const rel: [number, number] = [x - px, y - py]
    near.push({
      s,
      side: rel[0] * nrm[0] + rel[1] * nrm[1],
      d: Math.abs(rel[0] * axis[0] + rel[1] * axis[1]),
    })
  }
  const pick = (sign: 1 | -1) => {
    const cands = near.filter((c) => Math.sign(c.side) === sign && c.d < 2.5)
    if (cands.length) return cands.reduce((a, b) => (Math.abs(b.side) > Math.abs(a.side) ? b : a)).s
    const fallback = near.filter((c) => Math.sign(c.side) === sign)
    return fallback.length ? fallback.reduce((a, b) => (b.d < a.d ? b : a)).s : 0
  }
  return [pick(1), pick(-1)]
}

function packRows(tools: Tool[], bin: BinIn) {
  const items = tools.map((t) => {
    const pts: Poly = [...transform(recentre(t.offset), t.rot, 0, 0)]
    for (const h of t.fingerHoles) pts.push(...cutOutline(placedCut({ ...t, x: 0, y: 0 }, h)))
    return { t, b: bbox(pts) }
  })
  const maxRowW = Math.max(PITCH * 4, ...items.map((i) => i.b.w + 2 * MARGIN))
  let x = MARGIN,
    y = MARGIN,
    rowH = 0,
    usedW = 0
  const placed: Tool[] = []
  for (const { t, b } of items) {
    if (x + b.w + MARGIN > maxRowW && x > MARGIN) {
      x = MARGIN
      y += rowH + MARGIN
      rowH = 0
    }
    placed.push({ ...t, x: x - b.minx, y: y - b.miny })
    x += b.w + MARGIN
    rowH = Math.max(rowH, b.h)
    usedW = Math.max(usedW, x)
  }
  const totalH = y + rowH + MARGIN
  const gx = Math.max(1, Math.ceil((usedW + 0.5) / PITCH)),
    gy = Math.max(1, Math.ceil((totalH + 0.5) / PITCH))
  const W = PITCH * gx - 0.5,
    L = PITCH * gy - 0.5
  const dx = (W - usedW) / 2,
    dy = (L - totalH) / 2
  const maxDepth = Math.max(0, ...tools.map((t) => t.depth))
  const hu = Math.max(bin.height_units, Math.ceil((maxDepth + 7) / 7))
  return {
    tools: placed.map((t) => ({ ...t, x: t.x + dx, y: t.y + dy })),
    bin: { ...bin, grid_x: gx, grid_y: gy, height_units: hu },
    cells: gx * gy,
  }
}
export function autoFit(
  tools: Tool[],
  bin: BinIn,
  orient: 'auto' | 'horizontal' | 'vertical' | 'keep' = 'auto',
) {
  if (!tools.length) return { tools, bin }
  const variants: Tool[][] = []
  if (orient === 'keep') variants.push(tools)
  if (orient === 'auto' || orient === 'horizontal')
    variants.push(tools.map((t) => ({ ...t, rot: t.straightRot })))
  if (orient === 'auto' || orient === 'vertical')
    variants.push(tools.map((t) => ({ ...t, rot: t.straightRot + 90 })))
  const packed = variants.map((v) => packRows(v, bin))
  packed.sort(
    (a, b) =>
      a.cells - b.cells || Math.abs(a.bin.grid_x - a.bin.grid_y) - Math.abs(b.bin.grid_x - b.bin.grid_y),
  )
  return { tools: packed[0].tools, bin: packed[0].bin }
}

export default function DesignStep({ s, update }: { s: Session; update: (p: Partial<Session>) => void }) {
  const bin = s.bin
  const svgRef = useRef<SVGSVGElement>(null)
  const viewer = useRef<StlViewerHandle>(null)
  const [sel, setSel] = useState<string | null>(s.tools[0]?.id ?? null)
  const [drag, setDrag] = useState<{ id: string; dx: number; dy: number; hole?: number } | null>(null)
  const [drag3d, setDrag3d] = useState(false)
  const [blob, setBlob] = useState<Blob | null>(null)
  const [stats, setStats] = useState('')
  const [busy, setBusy] = useState('')
  const [previewBusy, setPreviewBusy] = useState(false) // a preview is scheduled or in flight
  const [err, setErr] = useState('')
  const [name, setName] = useState(safeFileName(s.projectName) || 'gridfinity-holder')
  const reqId = useRef(0)
  const toolsRef = useRef(s.tools)
  toolsRef.current = s.tools
  const sRef = useRef(s)
  sRef.current = s
  const pidRef = useRef(s.projectId)
  pidRef.current = s.projectId
  const setBin = (patch: Partial<BinIn>) => update({ bin: { ...sRef.current.bin, ...patch } })
  const setTools = (t: Tool[]) => {
    toolsRef.current = t
    update({ tools: t })
  }
  const patch = (id: string, p: Partial<Tool>) =>
    setTools(toolsRef.current.map((t) => (t.id === id ? { ...t, ...p } : t)))

  useEffect(() => {
    const up = () => setDrag(null)
    window.addEventListener('pointerup', up)
    return () => window.removeEventListener('pointerup', up)
  }, [])
  useEffect(() => {
    if (s.tools.length && s.tools.every((t) => t.x === 0 && t.y === 0)) {
      const r = autoFit(s.tools, bin, 'auto')
      update({ tools: r.tools, bin: r.bin })
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const W = PITCH * bin.grid_x - 0.5,
    L = PITCH * bin.grid_y - 0.5
  const pad = 8
  const wallKeep = wallKeepFor(bin)
  const placed = useMemo(
    () =>
      s.tools.map((t) => ({
        t,
        poly: placedPolygon(t),
        cuts: t.fingerHoles.map((h) => cutOutline(placedCut(t, h))),
      })),
    [s.tools],
  )
  const inside = ([x, y]: [number, number]) =>
    x >= wallKeep && y >= wallKeep && x <= W - wallKeep && y <= L - wallKeep
  const outside = placed
    .filter(({ poly, cuts }) => poly.some((p) => !inside(p)) || cuts.some((c) => c.some((p) => !inside(p))))
    .map((p) => p.t.name)
  const maxDepth = maxPocketDepth(bin)
  const tooDeep = s.tools.filter((t) => t.depth > maxDepth + 1e-6)
  const shallow = s.tools.filter((t) => t.depth < 0.4 * t.thickness)
  const overDeep = s.tools.filter((t) => t.depth > t.thickness + 3 && t.fingerHoles.length === 0)
  const neededUnits = Math.ceil((Math.max(0, ...s.tools.map((t) => t.depth)) + 7) / 7)
  const blocked = tooDeep.length > 0 || outside.length > 0

  // ---- live preview: debounced, aborts the superseded request, paused while dragging, skipped while blocked
  const previewKey = JSON.stringify({ bin, p: pockets(s.tools) })
  const idle = drag === null && !drag3d
  useEffect(() => {
    if (!idle || blocked) return
    const id = ++reqId.current
    const ac = new AbortController()
    setPreviewBusy(true)
    const h = setTimeout(async () => {
      try {
        const b = await api.generate(
          { bin: sRef.current.bin, pockets: pockets(toolsRef.current), format: 'stl', tolerance: 0.12 },
          ac.signal,
        )
        if (id === reqId.current) {
          setBlob(b)
          setErr('')
        }
      } catch (e) {
        if (!isAbort(e) && id === reqId.current) setErr(errMsg(e))
      } finally {
        if (id === reqId.current) setPreviewBusy(false)
      }
    }, 450)
    // Any change (or unmount) cancels the pending timer and aborts the in-flight request; the next run re-arms busy.
    return () => {
      clearTimeout(h)
      ac.abort()
      setPreviewBusy(false)
    }
  }, [previewKey, idle, blocked]) // eslint-disable-line react-hooks/exhaustive-deps

  // ---- 2D layout interaction
  function toMM(e: React.PointerEvent): [number, number] {
    const svg = svgRef.current!
    const pt = svg.createSVGPoint()
    pt.x = e.clientX
    pt.y = e.clientY
    const p = pt.matrixTransform(svg.getScreenCTM()!.inverse())
    return [p.x, p.y]
  }
  function down(e: React.PointerEvent, t: Tool, hole?: number) {
    e.stopPropagation()
    try {
      ;(e.currentTarget as Element).setPointerCapture?.(e.pointerId)
    } catch {}
    const [mx, my] = toMM(e)
    setSel(t.id)
    if (hole !== undefined) {
      setDrag({ id: t.id, dx: 0, dy: 0, hole })
    } else setDrag({ id: t.id, dx: t.x - mx, dy: t.y - my })
  }
  function move(e: React.PointerEvent) {
    if (!drag) return
    const [mx, my] = toMM(e)
    setTools(
      toolsRef.current.map((t) => {
        if (t.id !== drag.id) return t
        if (drag.hole !== undefined) {
          const [lx, ly] = transform([[mx - t.x, my - t.y]], -t.rot, 0, 0)[0]
          const sNew = nearestS(recentre(t.offset), [lx, ly])
          return { ...t, fingerHoles: t.fingerHoles.map((h, i) => (i === drag.hole ? { ...h, s: sNew } : h)) }
        }
        return { ...t, x: mx + drag.dx, y: my + drag.dy }
      }),
    )
  }
  // ---- 3D drag API
  const dragStart = useRef<{ x: number; y: number } | null>(null)
  const dragApi: DragApi | undefined = s.tools.length
    ? {
        bin: { W, L },
        hitTest: (x, y) => {
          for (const { t, poly, cuts } of placed)
            if (pointInPoly(poly, x, y) || cuts.some((c) => pointInPoly(c, x, y))) return t.id
          return null
        },
        onDragStart: (id) => {
          const t = toolsRef.current.find((x) => x.id === id)
          dragStart.current = t ? { x: t.x, y: t.y } : null
          setDrag3d(true)
        },
        onDrag: (id, dx, dy) => {
          const st = dragStart.current
          if (!st) return
          setTools(toolsRef.current.map((t) => (t.id === id ? { ...t, x: st.x + dx, y: st.y + dy } : t)))
        },
        onDragEnd: () => setDrag3d(false),
        onSelect: (id) => {
          if (id) setSel(id)
        },
      }
    : undefined

  const selTool = s.tools.find((t) => t.id === sel)
  const selPlaced = placed.find((p) => p.t.id === sel)
  const setSit = (t: Tool, sit: Sit) =>
    patch(t.id, { sit, depth: sit === 'custom' ? t.depth : depthFor({ ...t, sit }) })
  const setThickness = (t: Tool, thickness: number) =>
    patch(t.id, { thickness, depth: depthFor({ ...t, thickness }) })
  function addCuts(t: Tool, kind: 'pair' | 'single') {
    const [sA, sB] = pinchPositions(t)
    const mk = (s: number): FingerHole => ({ kind: 'notch', s, diameter: 20, extraDepth: 3 })
    const added = kind === 'pair' ? [mk(sA), mk(sB)] : [mk(sA)]
    const nt = { ...t, fingerHoles: [...t.fingerHoles, ...added] }
    const tools = toolsRef.current.map((x) => (x.id === t.id ? nt : x))
    // if a new notch pokes out of the bin, re-pack (keeping rotations) so the bin grows around it
    if (added.some((h) => cutOutline(placedCut(nt, h)).some((p) => !inside(p)))) {
      const r = autoFit(tools, bin, 'keep')
      toolsRef.current = r.tools
      update({ tools: r.tools, bin: r.bin })
    } else setTools(tools)
  }
  const patchCut = (t: Tool, i: number, p: Partial<FingerHole>) =>
    patch(t.id, { fingerHoles: t.fingerHoles.map((h, j) => (j === i ? { ...h, ...p } : h)) })

  // ---- export / save. A project is created on first use so every download lands in the Library.
  async function ensureProject(): Promise<string> {
    if (pidRef.current) return pidRef.current
    const cur = sRef.current
    const pname = cur.projectName.trim() || name || 'Gridfinity holder'
    const r = await api.libraryNew(pname)
    pidRef.current = r.project_id
    update({ projectId: r.project_id, projectName: cur.projectName.trim() || pname })
    return r.project_id
  }
  const fileBase = name || 'gridfinity-holder'
  async function download(format: 'stl' | '3mf' | 'step') {
    setBusy(`Building the ${format.toUpperCase()} file…`)
    setErr('')
    try {
      const pid = await ensureProject()
      const cur = sRef.current
      const file = `${fileBase}.${format}`
      const b = await api.generate({
        bin: cur.bin,
        pockets: pockets(toolsRef.current),
        format,
        filename: fileBase,
        tolerance: 0.02,
        project_id: pid,
        save: true,
        state: snapshot({ ...cur, projectId: pid }),
      })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(b)
      a.download = file
      a.click()
      setTimeout(() => URL.revokeObjectURL(a.href), 5000)
      const png = viewer.current?.snapshot()
      if (png) api.saveSnapshot(pid, png).catch(() => {})
      toast(`Saved ${file} to the Library`)
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setBusy('')
    }
  }
  async function save() {
    setBusy('Saving to the Library…')
    setErr('')
    try {
      const pid = await ensureProject()
      await api.saveState(pid, snapshot({ ...sRef.current, projectId: pid }))
      const png = viewer.current?.snapshot()
      if (png) await api.saveSnapshot(pid, png).catch(() => {})
      toast(`Saved ${sRef.current.projectName.trim() || fileBase} to the Library`)
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setBusy('')
    }
  }
  const num = (e: React.ChangeEvent<HTMLInputElement>, prev: number) => numOr(e.target.value, prev)
  const canExport = !busy && !blocked && !previewBusy
  const exportTitle = busy
    ? 'Wait for the current job to finish'
    : blocked
      ? 'Fix the red warnings first'
      : previewBusy
        ? 'Wait for the preview to finish'
        : ''
  const nothingToSave = s.view === 'trace' && s.tools.length === 0
  const canSave = !busy && !nothingToSave
  const isTrace = s.view === 'trace'
  const dividersX = bin.solid ? 0 : bin.dividers_x,
    dividersY = bin.solid ? 0 : bin.dividers_y
  const layoutHint =
    s.tools.length > 0
      ? 'drag a pocket to move it · drag a notch along its edge'
      : dividersX + dividersY > 0
        ? 'plain bin · dividers shown dashed'
        : 'plain bin · the 42 mm grid is dotted'
  const sitHint: Record<Sit, string> = {
    full: 'Pocket is the tool thickness plus 1 mm, so the tool sits flush with the top.',
    threeq: 'Pocket is 3/4 of the thickness. The top quarter sticks up so you can grab it.',
    half: 'Half the tool sticks up. Easiest to grab, least secure.',
    custom: 'Type the pocket depth you want.',
  }

  return (
    <>
      <div className="design">
        <div className="layoutpane">
          <div className="panehead">
            Layout (top view){' '}
            <span className="hint">
              {W} × {L} mm · {layoutHint}
            </span>
          </div>
          {s.tools.length > 0 && (
            <Tip id="layout" className="float tl">
              Drag a pocket to move it. Drag an orange notch along the edge.
            </Tip>
          )}
          <svg
            ref={svgRef}
            viewBox={`${-pad} ${-pad} ${W + 2 * pad} ${L + 2 * pad}`}
            style={{ touchAction: 'none', width: '100%', height: 'calc(100% - 30px)' }}
            onPointerMove={move}
            onPointerUp={() => setDrag(null)}
            onPointerDown={() => setSel(null)}
          >
            <rect
              x={0}
              y={0}
              width={W}
              height={L}
              rx={3.75}
              fill="var(--c-panel)"
              stroke="var(--c-ink)"
              strokeWidth={0.6}
            />
            {Array.from({ length: bin.grid_x - 1 }, (_, i) => (
              <line
                key={'v' + i}
                x1={(i + 1) * PITCH - 0.25}
                y1={0}
                x2={(i + 1) * PITCH - 0.25}
                y2={L}
                stroke="var(--c-grid)"
                strokeWidth={0.3}
                strokeDasharray="2 2"
              />
            ))}
            {Array.from({ length: bin.grid_y - 1 }, (_, i) => (
              <line
                key={'h' + i}
                x1={0}
                y1={(i + 1) * PITCH - 0.25}
                x2={W}
                y2={(i + 1) * PITCH - 0.25}
                stroke="var(--c-grid)"
                strokeWidth={0.3}
                strokeDasharray="2 2"
              />
            ))}
            {bin.lip === 'regular' && (
              <rect
                x={2.6}
                y={2.6}
                width={W - 5.2}
                height={L - 5.2}
                rx={1.2}
                fill="none"
                stroke="var(--c-grid)"
                strokeWidth={0.3}
              />
            )}
            {Array.from({ length: dividersX }, (_, i) => (
              <line
                key={'dx' + i}
                x1={(W * (i + 1)) / (dividersX + 1)}
                y1={2}
                x2={(W * (i + 1)) / (dividersX + 1)}
                y2={L - 2}
                stroke="var(--c-ink)"
                strokeOpacity={0.6}
                strokeWidth={0.8}
                strokeDasharray="4 3"
              />
            ))}
            {Array.from({ length: dividersY }, (_, i) => (
              <line
                key={'dy' + i}
                x1={2}
                y1={(L * (i + 1)) / (dividersY + 1)}
                x2={W - 2}
                y2={(L * (i + 1)) / (dividersY + 1)}
                stroke="var(--c-ink)"
                strokeOpacity={0.6}
                strokeWidth={0.8}
                strokeDasharray="4 3"
              />
            ))}
            {placed.map(({ t, poly, cuts }) => {
              const [cx, cy] = centroid(poly)
              const bad = tooDeep.includes(t) || outside.includes(t.name)
              const isSel = t.id === sel
              return (
                <g key={t.id} onPointerDown={(e) => down(e, t)} style={{ cursor: 'move' }}>
                  <path
                    d={toPath(poly)}
                    fill={
                      bad
                        ? 'var(--c-danger-soft)'
                        : isSel
                          ? 'var(--c-model-selected)'
                          : 'var(--c-accent-soft)'
                    }
                    fillOpacity={bad ? 1 : isSel ? 0.35 : 0.6}
                    stroke={bad ? 'var(--c-danger)' : isSel ? 'var(--c-model-selected)' : 'var(--c-trace)'}
                    strokeWidth={isSel ? 1.0 : 0.4}
                  />
                  {cuts.map((c, i) => (
                    <path
                      key={i}
                      d={toPath(c)}
                      fill="var(--c-pocket)"
                      fillOpacity={0.3}
                      stroke="var(--c-pocket)"
                      strokeWidth={0.5}
                      style={{ cursor: 'grab' }}
                      onPointerDown={(e) => down(e, t, i)}
                    />
                  ))}
                  <text
                    x={cx}
                    y={cy}
                    fontSize={4}
                    textAnchor="middle"
                    fill="var(--c-ink)"
                    fontWeight={isSel ? 600 : 400}
                    style={{ pointerEvents: 'none' }}
                  >
                    {t.name} · {t.depth} mm
                  </text>
                </g>
              )
            })}
          </svg>
        </div>
        <div className="previewpane">
          <div className="panehead">
            3D preview <span className="hint">{stats}</span>
          </div>
          <StlViewer
            ref={viewer}
            data={blob}
            onStats={setStats}
            minHeight={300}
            busy={previewBusy}
            busyText="Building the 3D model…"
            drag={dragApi}
            highlight={selPlaced?.poly ?? null}
          />
          <Tip id="view3d" className="float bl">
            {s.tools.length > 0 ? 'Drag a pocket here too. ' : ''}Drag empty space to orbit, right-drag to
            pan, scroll to zoom.
          </Tip>
        </div>
      </div>
      <aside className="panel">
        <h2>{s.view === 'bin' ? 'Gridfinity bin' : '4. Design and export'}</h2>
        {s.tools.length > 0 && (
          <div className="btnrow">
            <button
              className="primary"
              title="Straightens every tool, packs them tightly and sizes the bin to fit"
              onClick={() => {
                const r = autoFit(s.tools, bin, 'auto')
                update({ tools: r.tools, bin: r.bin })
                toast('Arranged the pockets', 'info', 2000)
              }}
            >
              Auto-arrange
            </button>
            <button
              className="secondary"
              title="Pack with every tool lying left to right"
              onClick={() => {
                const r = autoFit(s.tools, bin, 'horizontal')
                update({ tools: r.tools, bin: r.bin })
                toast('Arranged the pockets along the bin', 'info', 2000)
              }}
            >
              Along
            </button>
            <button
              className="secondary"
              title="Pack with every tool standing top to bottom"
              onClick={() => {
                const r = autoFit(s.tools, bin, 'vertical')
                update({ tools: r.tools, bin: r.bin })
                toast('Arranged the pockets across the bin', 'info', 2000)
              }}
            >
              Across
            </button>
          </div>
        )}
        {outside.length > 0 && (
          <div className="status err">
            Too close to the wall or outside the bin: {outside.join(', ')}. Drag it in, shrink the cut, or add
            a grid unit.
          </div>
        )}
        {tooDeep.length > 0 && (
          <div className="status err">
            Too deep for a {bin.height_units}-unit bin (max pocket {maxDepth} mm):{' '}
            {tooDeep.map((t) => t.name).join(', ')}.
            <button
              className="ghost"
              title="Make the bin tall enough for the deepest pocket"
              onClick={() => {
                setBin({ height_units: neededUnits })
                toast(`Bin is now ${neededUnits} units tall`)
              }}
            >
              Raise bin to {neededUnits} units
            </button>
          </div>
        )}
        {shallow.length > 0 && (
          <div className="status warn">
            Shallow pocket (under 40% of the tool thickness), may not hold:{' '}
            {shallow.map((t) => t.name).join(', ')}.
          </div>
        )}
        {overDeep.length > 0 && (
          <div className="status warn">
            Pocket much deeper than the tool with no notch, hard to grab:{' '}
            {overDeep.map((t) => t.name).join(', ')}.
          </div>
        )}
        {err && <div className="status err">{err}</div>}

        <h3>Bin size</h3>
        <label className="row">
          Width (42 mm units)
          <input
            type="number"
            min={1}
            max={10}
            value={bin.grid_x}
            title="Grid cells left to right. 1 unit = 42 mm. 2 to 4 is typical"
            onChange={(e) => setBin({ grid_x: num(e, bin.grid_x) })}
          />
        </label>
        <label className="row">
          Length (42 mm units)
          <input
            type="number"
            min={1}
            max={10}
            value={bin.grid_y}
            title="Grid cells front to back. 1 unit = 42 mm"
            onChange={(e) => setBin({ grid_y: num(e, bin.grid_y) })}
          />
        </label>
        <label className="row">
          Height (7 mm units)
          <input
            type="number"
            min={1}
            max={20}
            value={bin.height_units}
            title="Bin height in 7 mm units, not counting the lip. 3 units = 21 mm"
            onChange={(e) => setBin({ height_units: num(e, bin.height_units) })}
          />
        </label>
        <p className="hint">
          {W} × {L} × {bin.height_units * 7 + (bin.lip === 'regular' ? 4.4 : 0)} mm · deepest pocket allowed{' '}
          {maxDepth} mm
        </p>

        <h3>Bin options</h3>
        <label className="row">
          Stacking lip
          <select
            value={bin.lip}
            title="The 4.4 mm rim on top that lets another bin stack on this one"
            onChange={(e) => setBin({ lip: e.target.value as BinIn['lip'] })}
          >
            <option value="regular">Regular</option>
            <option value="none">None</option>
          </select>
        </label>
        <label className="row">
          Base holes
          <select
            value={bin.holes}
            title="Holes under each foot for 6×2 mm magnets or M3 screws"
            onChange={(e) => setBin({ holes: e.target.value as BinIn['holes'] })}
          >
            <option value="none">None</option>
            <option value="magnet">Magnet 6×2</option>
            <option value="screw">Screw M3</option>
            <option value="magnet_screw">Magnet + screw</option>
          </select>
        </label>
        <label className="row">
          Body
          <select
            value={bin.solid ? 'solid' : 'hollow'}
            title="Solid: a block with a pocket cut for each tool. Hollow: an open bin with optional dividers"
            onChange={(e) => setBin({ solid: e.target.value === 'solid' })}
          >
            <option value="solid">Solid with pockets</option>
            <option value="hollow">Hollow bin</option>
          </select>
        </label>
        {!bin.solid && (
          <>
            <label className="row">
              Compartments across
              <input
                type="number"
                min={1}
                max={11}
                value={bin.dividers_x + 1}
                title="How many sections left to right. 1 means no divider"
                onChange={(e) => setBin({ dividers_x: num(e, bin.dividers_x + 1) - 1 })}
              />
            </label>
            <label className="row">
              Compartments along
              <input
                type="number"
                min={1}
                max={11}
                value={bin.dividers_y + 1}
                title="How many sections front to back. 1 means no divider"
                onChange={(e) => setBin({ dividers_y: num(e, bin.dividers_y + 1) - 1 })}
              />
            </label>
            <label className="row chk">
              Scoop
              <input
                type="checkbox"
                checked={bin.scoop}
                title="Rounds the inside front edge so small parts slide out"
                onChange={(e) => setBin({ scoop: e.target.checked })}
              />
            </label>
            {bin.scoop && (
              <label className="row">
                Scoop radius (mm)
                <input
                  type="number"
                  min={2}
                  max={30}
                  value={bin.scoop_radius}
                  title="Size of the rounded scoop. 10 mm is typical"
                  onChange={(e) => setBin({ scoop_radius: num(e, bin.scoop_radius) })}
                />
              </label>
            )}
            <label className="row chk">
              Label tab
              <input
                type="checkbox"
                checked={bin.label_tab}
                title="A flat ledge along the back edge for a label"
                onChange={(e) => setBin({ label_tab: e.target.checked })}
              />
            </label>
          </>
        )}

        {s.tools.length > 0 && (
          <>
            <h3>Selected tool</h3>
            {!selTool && (
              <p className="hint">
                Click a pocket in the layout or the 3D view to move, rotate, or set its depth.
              </p>
            )}
            {selTool && (
              <>
                <div style={{ fontWeight: 600 }}>{selTool.name}</div>
                <label className="row">
                  Rotation ({selTool.rot}°)
                  <input
                    type="range"
                    min={-180}
                    max={180}
                    step={1}
                    value={selTool.rot}
                    title="Turn the pocket. Drag the slider or use the arrow keys"
                    aria-valuetext={`${selTool.rot} degrees`}
                    onChange={(e) => patch(selTool.id, { rot: num(e, selTool.rot) })}
                  />
                </label>
                <div className="btnrow">
                  <button
                    className="secondary"
                    title="Turn the tool so its long side is level"
                    onClick={() => patch(selTool.id, { rot: selTool.straightRot })}
                  >
                    Straighten
                  </button>
                  <button
                    className="secondary"
                    title="Turn the pocket a quarter turn clockwise"
                    onClick={() => patch(selTool.id, { rot: selTool.rot + 90 })}
                  >
                    Rotate 90°
                  </button>
                </div>
                <label className="row">
                  Tool thickness (mm)
                  <input
                    type="number"
                    step="0.5"
                    min={1}
                    max={100}
                    value={selTool.thickness}
                    title="How tall the tool is lying flat. The pocket depth follows from this"
                    onChange={(e) => setThickness(selTool, num(e, selTool.thickness))}
                  />
                </label>
                <label className="row">
                  How it sits
                  <select
                    value={selTool.sit}
                    title="How much of the tool sticks up out of the pocket"
                    onChange={(e) => setSit(selTool, e.target.value as Sit)}
                  >
                    <option value="full">Fully sunk</option>
                    <option value="threeq">3/4 sunk</option>
                    <option value="half">Half sunk</option>
                    <option value="custom">Custom</option>
                  </select>
                </label>
                <p className="fieldhint">{sitHint[selTool.sit]}</p>
                <label className="row">
                  Pocket depth (mm)
                  <input
                    type="number"
                    step="0.5"
                    min={1}
                    max={140}
                    value={selTool.depth}
                    disabled={selTool.sit !== 'custom'}
                    title={
                      selTool.sit === 'custom'
                        ? 'How deep the pocket goes'
                        : 'Set by "How it sits". Pick Custom to type your own'
                    }
                    onChange={(e) => patch(selTool.id, { depth: num(e, selTool.depth) })}
                  />
                </label>

                <h3>Finger notches</h3>
                <div className="btnrow">
                  <button
                    className="secondary"
                    title="Two notches, one on each side, so you can pinch the tool out"
                    onClick={() => {
                      addCuts(selTool, 'pair')
                      toast(`Added a pinch pair to ${selTool.name}`)
                    }}
                  >
                    + Pinch pair
                  </button>
                  <button
                    className="secondary"
                    title="One notch on the edge of the pocket"
                    onClick={() => {
                      addCuts(selTool, 'single')
                      toast(`Added a notch to ${selTool.name}`)
                    }}
                  >
                    + Single notch
                  </button>
                </div>
                <p className="hint">
                  A notch is a round bite on the edge of the pocket, half in the bin material, so a fingertip
                  gets under the tool. A pinch pair puts one on each side. Drag a notch to slide it along the
                  outline. Notches go 3 mm below the pocket floor and are clamped to the solid floor.
                </p>
                {selTool.fingerHoles.map((h, i) => (
                  <div key={i} className="toolcard">
                    <div className="name">
                      <span>Notch {i + 1}</span>
                      <button
                        className="ghost"
                        title="Remove this notch"
                        onClick={() => {
                          patch(selTool.id, { fingerHoles: selTool.fingerHoles.filter((_, j) => j !== i) })
                          toast(`Removed notch ${i + 1}`, 'info')
                        }}
                      >
                        Remove
                      </button>
                    </div>
                    <label className="row">
                      Diameter (mm)
                      <input
                        type="number"
                        min={8}
                        max={40}
                        value={h.diameter}
                        title="Width of the notch. 20 mm fits a fingertip"
                        onChange={(e) => patchCut(selTool, i, { diameter: num(e, h.diameter) })}
                      />
                    </label>
                    <label className="row">
                      Below pocket floor (mm)
                      <input
                        type="number"
                        min={0}
                        max={20}
                        step={0.5}
                        value={h.extraDepth}
                        title="How much deeper than the pocket the notch goes. 3 mm is typical"
                        onChange={(e) => patchCut(selTool, i, { extraDepth: num(e, h.extraDepth) })}
                      />
                    </label>
                    <label className="row">
                      Position along edge
                      <input
                        type="range"
                        min={0}
                        max={1}
                        step={0.002}
                        value={h.s}
                        title="Slide the notch around the pocket outline"
                        aria-valuetext={`${Math.round(h.s * 100)}% of the way around`}
                        onChange={(e) => patchCut(selTool, i, { s: num(e, h.s) })}
                      />
                    </label>
                  </div>
                ))}
              </>
            )}
          </>
        )}
        {s.tools.length === 0 && s.view === 'bin' && (
          <p className="hint">
            A plain bin. Want pockets?{' '}
            <button
              className="ghost"
              title="Start a new traced holder from a photo"
              onClick={() => update({ view: 'trace', step: 0 })}
            >
              Trace tools from a photo
            </button>
          </p>
        )}

        <h3>Export</h3>
        <label className="row">
          File name
          <input
            type="text"
            value={name}
            title="Letters, digits, - and _ only. Spaces become dashes"
            onChange={(e) => setName(safeFileName(e.target.value))}
          />
        </label>
        <p className="fieldhint">Saved as {fileBase}.stl (or .3mf / .step)</p>
        <p className="hint">
          Every download is kept in the Library with a 3D snapshot and the layout. STEP keeps exact curves for
          Fusion or FreeCAD.
        </p>
        {busy && (
          <div className="status" role="status">
            <span className="spinner" />
            {busy}
          </div>
        )}
        <div className="cta">
          <button
            className="ghost"
            title={isTrace ? 'Back to tracing' : 'Back to the home screen'}
            onClick={() => (isTrace ? update({ step: 2 }) : update({ view: 'home' }))}
          >
            ← Back
          </button>
          <button
            className="ghost right"
            disabled={!canSave}
            title={
              nothingToSave
                ? 'Nothing to save yet: trace a tool first'
                : busy
                  ? 'Wait for the current job to finish'
                  : 'Save the layout and settings to the Library without downloading'
            }
            onClick={save}
          >
            Save to library
          </button>
          <span className="break" aria-hidden="true" />
          <button
            className="primary grow"
            disabled={!canExport}
            title={exportTitle || 'Download an STL for your slicer'}
            onClick={() => download('stl')}
          >
            Download STL
          </button>
          <button
            className="secondary"
            disabled={!canExport}
            title={exportTitle || 'Download a 3MF (slicer format that keeps units and the name)'}
            onClick={() => download('3mf')}
          >
            3MF
          </button>
          <button
            className="secondary"
            disabled={!canExport}
            title={exportTitle || 'Download a STEP file for CAD (exact curves)'}
            onClick={() => download('step')}
          >
            STEP
          </button>
        </div>
      </aside>
    </>
  )
}
