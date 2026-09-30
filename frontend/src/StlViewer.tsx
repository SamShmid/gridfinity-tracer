import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js'
import type { Poly } from './api'
import { theme } from './theme'
import { BusyOverlay } from './ui'

export interface StlViewerHandle {
  snapshot: () => string | null
  home: () => void
}
type ViewName = 'home' | 'top' | 'front' | 'side'
const VIEW_TITLES: Record<ViewName, string> = {
  home: 'Reset the view to the default angle',
  top: 'Look straight down',
  front: 'Look from the front',
  side: 'Look from the side',
}

/** Optional 3D editing hooks: the viewer converts hits on the model's top face to layout mm. */
export interface DragApi {
  bin: { W: number; L: number }
  hitTest: (x: number, y: number) => string | null
  onDragStart?: (id: string) => void
  onDrag: (id: string, dx: number, dy: number) => void // total delta since the grab, layout mm (y down)
  onDragEnd?: () => void
  onSelect?: (id: string | null) => void
}

interface Props {
  data: Blob | ArrayBuffer | null
  onStats?: (s: string) => void
  minHeight?: number
  busy?: boolean
  /** Plain-language text for the overlay while `busy` (defaults to a short "Updating…"). */
  busyText?: string
  drag?: DragApi
  /** Pocket outline to highlight, in layout mm (bin top-left origin, y down). Drawn as a flat translucent shape on top of the bin. */
  highlight?: Poly | null
}

/**
 * three.js STL viewer with Tinkercad-style controls:
 *   left-drag on a pocket  moves that tool (when a DragApi is supplied)
 *   left-drag elsewhere    orbits          right-drag   orbits
 *   scroll                 zooms           middle-drag / shift+drag / Pan mode   pans
 *   Home / Top / Front / Side buttons; the camera never goes under the floor.
 */
type Prefs = { flipH: boolean; flipV: boolean; flipZoom: boolean; scroll: 'auto' | 'pan' | 'zoom' }
const DEFAULT_PREFS: Prefs = { flipH: true, flipV: true, flipZoom: false, scroll: 'auto' }
function loadPrefs(): Prefs {
  try {
    return { ...DEFAULT_PREFS, ...JSON.parse(localStorage.getItem('gt.viewer') || '{}') }
  } catch {
    return DEFAULT_PREFS
  }
}

type State = {
  scene: THREE.Scene
  camera: THREE.PerspectiveCamera
  controls: OrbitControls
  renderer: THREE.WebGLRenderer
  mesh?: THREE.Mesh
  grid?: THREE.GridHelper
  gridSpec?: { cells: number; cx: number; cy: number; z: number }
  box?: THREE.Box3
  hl?: THREE.Object3D
}

/** Free an object's GPU buffers (geometry and every material). three.js does not do this on scene.remove. */
function disposeObject(o: THREE.Object3D) {
  o.traverse((c) => {
    const m = c as THREE.Mesh
    m.geometry?.dispose?.()
    const mats = Array.isArray(m.material) ? m.material : m.material ? [m.material] : []
    for (const mat of mats) mat.dispose()
  })
}

/** Floor grid in 42 mm cells under the model, coloured for the current theme. Replaces any previous grid. */
function setGrid(s: State, cells: number, cx: number, cy: number, z: number) {
  if (s.grid) {
    s.scene.remove(s.grid)
    disposeObject(s.grid)
  }
  const grid = new THREE.GridHelper(cells * 42, cells, theme.color('floorMajor'), theme.color('floor'))
  grid.rotation.x = Math.PI / 2
  grid.position.set(cx, cy, z)
  s.scene.add(grid)
  s.grid = grid
  s.gridSpec = { cells, cx, cy, z }
}

const StlViewer = forwardRef<StlViewerHandle, Props>(function StlViewer(
  { data, onStats, minHeight = 420, busy, busyText, drag, highlight },
  ref,
) {
  const mount = useRef<HTMLDivElement>(null)
  const st = useRef<State | null>(null)
  const dragRef = useRef(drag)
  dragRef.current = drag
  const [ready, setReady] = useState(false)
  const [meshVersion, setMeshVersion] = useState(0)
  const [cursor, setCursor] = useState<'grab' | 'grabbing' | 'default'>('default')
  const [prefs, setPrefs] = useState(loadPrefs)
  const prefsRef = useRef(prefs)
  prefsRef.current = prefs
  const [showPrefs, setShowPrefs] = useState(false)
  const [panMode, setPanMode] = useState(false)
  const panRef = useRef(panMode)
  panRef.current = panMode
  useEffect(() => {
    try {
      localStorage.setItem('gt.viewer', JSON.stringify(prefs))
    } catch {}
  }, [prefs])

  useImperativeHandle(ref, () => ({
    snapshot: () => {
      const s = st.current
      if (!s?.mesh) return null
      s.renderer.render(s.scene, s.camera)
      return s.renderer.domElement.toDataURL('image/png')
    },
    home: () => setView('home'),
  }))

  function setView(name: ViewName) {
    const s = st.current
    if (!s?.box) return
    const c = s.box.getCenter(new THREE.Vector3())
    const radius = s.box.getSize(new THREE.Vector3()).length() / 2
    const dirs: Record<ViewName, THREE.Vector3> = {
      home: new THREE.Vector3(0.9, -1, 0.8),
      top: new THREE.Vector3(0, -0.02, 1),
      front: new THREE.Vector3(0, -1, 0.3),
      side: new THREE.Vector3(1, 0, 0.3),
    }
    // distance so the bounding sphere fits in the narrower of the vertical / horizontal field of view
    const fovV = THREE.MathUtils.degToRad(s.camera.fov)
    const fovH = 2 * Math.atan(Math.tan(fovV / 2) * s.camera.aspect)
    const dist = (radius / Math.sin(Math.min(fovV, fovH) / 2)) * 1.05
    s.camera.position.copy(c).add(dirs[name].normalize().multiplyScalar(dist))
    s.controls.target.copy(c)
    s.controls.update()
  }

  // Pan mode: left-drag and one-finger touch pan instead of orbiting.
  useEffect(() => {
    const s = st.current
    if (!s) return
    s.controls.mouseButtons.LEFT = panMode ? THREE.MOUSE.PAN : THREE.MOUSE.ROTATE
    s.controls.touches.ONE = panMode ? THREE.TOUCH.PAN : THREE.TOUCH.ROTATE
  }, [panMode, ready])

  useEffect(() => {
    const el = mount.current!
    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(
      35,
      Math.max(el.clientWidth, 1) / Math.max(el.clientHeight, 1),
      1,
      5000,
    )
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true })
    renderer.setPixelRatio(window.devicePixelRatio)
    renderer.setSize(el.clientWidth, el.clientHeight)
    el.appendChild(renderer.domElement)
    scene.add(new THREE.HemisphereLight(0xffffff, 0x445, 1.0))
    const dir = new THREE.DirectionalLight(0xffffff, 1.3)
    dir.position.set(0.6, -1, 1.4)
    scene.add(dir)
    const dir2 = new THREE.DirectionalLight(0xffffff, 0.4)
    dir2.position.set(-1, 0.5, 0.5)
    scene.add(dir2)
    const controls = new OrbitControls(camera, renderer.domElement)
    // Per-axis direction flips (OrbitControls only offers one rotateSpeed for both axes).
    // These wrap PRIVATE methods of the OrbitControls class in three 0.169 (pinned exactly in package.json).
    // A three upgrade must re-check that _rotateLeft/_rotateUp/_dollyIn/_dollyOut/_pan still exist with
    // these signatures, or the flips and trackpad panning silently stop working.
    const c = controls as unknown as {
      _rotateLeft: (a: number) => void
      _rotateUp: (a: number) => void
      _dollyIn: (s: number) => void
      _dollyOut: (s: number) => void
    }
    const rl = c._rotateLeft.bind(controls),
      ru = c._rotateUp.bind(controls),
      di = c._dollyIn.bind(controls),
      dout = c._dollyOut.bind(controls)
    c._rotateLeft = (a) => rl(prefsRef.current.flipH ? -a : a)
    c._rotateUp = (a) => ru(prefsRef.current.flipV ? -a : a)
    c._dollyIn = (k) => (prefsRef.current.flipZoom ? dout(k) : di(k))
    c._dollyOut = (k) => (prefsRef.current.flipZoom ? di(k) : dout(k))
    controls.enableDamping = true
    controls.dampingFactor = 0.12
    controls.rotateSpeed = 0.8
    controls.zoomSpeed = 1.0
    controls.panSpeed = 1.0
    controls.maxPolarAngle = Math.PI / 2 - 0.03
    controls.minPolarAngle = 0.05
    controls.screenSpacePanning = true // pan parallel to the screen (what CAD tools do)
    controls.zoomToCursor = true // zoom toward the point under the cursor (Onshape/Fusion)
    controls.minDistance = 20
    controls.maxDistance = 3000
    controls.mouseButtons = {
      LEFT: panRef.current ? THREE.MOUSE.PAN : THREE.MOUSE.ROTATE,
      MIDDLE: THREE.MOUSE.PAN,
      RIGHT: THREE.MOUSE.PAN,
    }
    controls.touches = {
      ONE: panRef.current ? THREE.TOUCH.PAN : THREE.TOUCH.ROTATE,
      TWO: THREE.TOUCH.DOLLY_PAN,
    }
    // Arrow keys pan, but only while the view itself has focus. OrbitControls calls preventDefault on every
    // arrow key it sees, so listening on window would break sliders, number boxes and text caret movement.
    renderer.domElement.tabIndex = 0
    renderer.domElement.setAttribute('role', 'application')
    renderer.domElement.setAttribute(
      'aria-label',
      '3D model view. Click it, then use the arrow keys to pan. Use the buttons above it for preset angles',
    )
    controls.listenToKeyEvents(renderer.domElement)
    // Wheel: distinguish a mouse wheel / pinch (zoom) from two-finger trackpad scrolling (pan).
    //   pinch on a trackpad arrives as a wheel event with ctrlKey; mouse wheels report line deltas
    //   or large pixel steps; trackpad scrolls are small pixel deltas, often with a deltaX.
    const onWheel = (e: WheelEvent) => {
      const pref = prefsRef.current.scroll
      const pinch = e.ctrlKey || e.metaKey
      const looksTrackpad =
        e.deltaMode === 0 &&
        !pinch &&
        (e.deltaX !== 0 || (Math.abs(e.deltaY) < 40 && !Number.isInteger(e.deltaY / 40)))
      const pan = pref === 'pan' ? !pinch : pref === 'zoom' ? false : looksTrackpad
      if (!pan) return // let OrbitControls zoom to cursor
      e.preventDefault()
      e.stopImmediatePropagation()
      ;(controls as unknown as { _pan: (x: number, y: number) => void })._pan(-e.deltaX, -e.deltaY)
      controls.update()
    }
    renderer.domElement.addEventListener('wheel', onWheel, { capture: true, passive: false })
    camera.up.set(0, 0, 1)
    camera.position.set(150, -150, 130)
    controls.target.set(0, 0, 0)
    const state: State = { scene, camera, controls, renderer }
    setGrid(state, 10, 0, 0, -0.05)
    st.current = state
    const onKey = (e: KeyboardEvent) => {
      controls.mouseButtons.LEFT = e.shiftKey || panRef.current ? THREE.MOUSE.PAN : THREE.MOUSE.ROTATE
    } // shift+drag pans
    window.addEventListener('keydown', onKey)
    window.addEventListener('keyup', onKey)
    // Follow the OS light/dark switch: CSS variables update on their own, the GPU colours do not.
    const recolor = () => {
      const s = st.current
      if (!s) return
      if (s.mesh) (s.mesh.material as THREE.MeshStandardMaterial).color.set(theme.color('model'))
      if (s.gridSpec) {
        const { cells, cx, cy, z } = s.gridSpec
        setGrid(s, cells, cx, cy, z)
      }
      s.hl?.traverse((o) => {
        const mat = (o as THREE.Mesh).material as THREE.MeshBasicMaterial | undefined
        mat?.color?.set(theme.color('modelSelected'))
      })
    }
    const mq = window.matchMedia?.('(prefers-color-scheme: dark)')
    mq?.addEventListener?.('change', recolor)

    // ---- tool dragging in 3D
    const ray = new THREE.Raycaster()
    const ndc = new THREE.Vector2()
    let dragging: { id: string; plane: THREE.Plane; grab: THREE.Vector3 } | null = null
    const toNdc = (ev: PointerEvent) => {
      const r = renderer.domElement.getBoundingClientRect()
      ndc.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1)
      ray.setFromCamera(ndc, camera)
    }
    const pick = (ev: PointerEvent) => {
      const s = st.current
      const api = dragRef.current
      if (!s?.mesh || !api) return null
      toNdc(ev)
      const hit = ray.intersectObject(s.mesh, false)[0]
      if (!hit) return null
      return { id: api.hitTest(hit.point.x + api.bin.W / 2, api.bin.L / 2 - hit.point.y), point: hit.point }
    }
    const onDown = (ev: PointerEvent) => {
      if (ev.button !== 0 || ev.shiftKey || panRef.current) return
      const h = pick(ev)
      dragRef.current?.onSelect?.(h?.id ?? null)
      if (!h?.id) return
      controls.enabled = false
      renderer.domElement.setPointerCapture(ev.pointerId)
      dragging = {
        id: h.id,
        plane: new THREE.Plane(new THREE.Vector3(0, 0, 1), -h.point.z),
        grab: h.point.clone(),
      }
      dragRef.current?.onDragStart?.(h.id)
      setCursor('grabbing')
      ev.stopPropagation()
    }
    const onMove = (ev: PointerEvent) => {
      const api = dragRef.current
      if (!dragging || !api) {
        if (api && !ev.buttons && !panRef.current) setCursor(pick(ev)?.id ? 'grab' : 'default')
        return
      }
      toNdc(ev)
      const p = new THREE.Vector3()
      if (!ray.ray.intersectPlane(dragging.plane, p)) return
      api.onDrag(dragging.id, p.x - dragging.grab.x, -(p.y - dragging.grab.y))
    }
    const onUp = () => {
      if (dragging) {
        dragging = null
        controls.enabled = true
        setCursor('default')
        dragRef.current?.onDragEnd?.()
      }
    }
    renderer.domElement.addEventListener('pointerdown', onDown, { capture: true })
    renderer.domElement.addEventListener('pointermove', onMove)
    renderer.domElement.addEventListener('pointerup', onUp)
    renderer.domElement.addEventListener('pointercancel', onUp) // a touch the browser took over mid-drag
    renderer.domElement.addEventListener('pointerleave', onUp)
    renderer.domElement.addEventListener('contextmenu', (e) => e.preventDefault())

    let raf = 0
    // Keep the view sane: the pivot (controls.target) must stay inside a box around the model and the
    // camera must stay above the floor. Screen-space panning can otherwise drag the pivot under the
    // floor, the camera follows it, and orbit/pan feel inverted from underneath.
    const clampView = () => {
      const s = st.current
      const box = s?.box
      const t = controls.target
      if (box) {
        const c = box.getCenter(new THREE.Vector3())
        const size = box.getSize(new THREE.Vector3())
        const pad = Math.max(size.x, size.y, 42) * 1.0
        t.x = THREE.MathUtils.clamp(t.x, c.x - pad, c.x + pad)
        t.y = THREE.MathUtils.clamp(t.y, c.y - pad, c.y + pad)
        t.z = THREE.MathUtils.clamp(t.z, box.min.z, box.max.z + size.z)
      } else {
        t.z = Math.max(t.z, 0)
      }
      const floor = (box?.min.z ?? 0) + 2
      if (camera.position.z < floor) {
        const dz = floor - camera.position.z
        camera.position.z += dz
        t.z += dz
      }
    }
    const loop = () => {
      controls.update()
      clampView()
      renderer.render(scene, camera)
      raf = requestAnimationFrame(loop)
    }
    loop()
    const onResize = () => {
      // The pane can be 0 px tall mid-layout (the phone breakpoint, a collapsing grid row): a 0/0 aspect
      // is NaN and nothing renders until the next resize, so clamp like the constructor does.
      const w = Math.max(el.clientWidth, 1),
        h = Math.max(el.clientHeight, 1)
      camera.aspect = w / h
      camera.updateProjectionMatrix()
      renderer.setSize(w, h)
    }
    window.addEventListener('resize', onResize)
    const ro = new ResizeObserver(onResize)
    ro.observe(el)
    setReady(true)
    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      window.removeEventListener('resize', onResize)
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('keyup', onKey)
      mq?.removeEventListener?.('change', recolor)
      renderer.domElement.removeEventListener('wheel', onWheel, { capture: true } as EventListenerOptions)
      controls.stopListenToKeyEvents()
      controls.dispose()
      // Free the GPU side too: renderer.dispose() alone leaves buffers and the WebGL context alive until the
      // browser gets round to collecting it, and browsers cap live contexts at about 16.
      disposeObject(scene)
      renderer.forceContextLoss()
      renderer.dispose()
      el.innerHTML = ''
      st.current = null
    }
  }, [])

  useEffect(() => {
    if (!ready || !st.current || !data) return
    const s = st.current
    let cancelled = false
    const load = async () => {
      const buf = data instanceof Blob ? await data.arrayBuffer() : data
      if (cancelled || !st.current) return
      const geom = new STLLoader().parse(buf)
      geom.computeVertexNormals()
      geom.computeBoundingBox()
      const hadMesh = !!s.mesh
      if (s.mesh) {
        s.scene.remove(s.mesh)
        disposeObject(s.mesh) // geometry and material: every preview would otherwise leak a material
      }
      const mesh = new THREE.Mesh(
        geom,
        new THREE.MeshStandardMaterial({
          color: new THREE.Color(theme.color('model')),
          roughness: 0.6,
          metalness: 0.05,
        }),
      )
      s.scene.add(mesh)
      s.mesh = mesh
      const box = geom.boundingBox!
      s.box = box
      const sz = box.getSize(new THREE.Vector3())
      const c = box.getCenter(new THREE.Vector3())
      setGrid(s, Math.ceil(Math.max(sz.x, sz.y) / 42) + 2, c.x, c.y, box.min.z - 0.05)
      if (!hadMesh) setView('home')
      setMeshVersion((v) => v + 1)
      onStats?.(
        `${sz.x.toFixed(1)} × ${sz.y.toFixed(1)} × ${sz.z.toFixed(1)} mm · ${(geom.getAttribute('position').count / 3).toLocaleString()} triangles`,
      )
    }
    load()
    return () => {
      cancelled = true
    }
  }, [data, ready]) // eslint-disable-line react-hooks/exhaustive-deps

  // ---- selected pocket highlight: flat translucent shape (plus outline) sitting just above the bin's top
  useEffect(() => {
    const s = st.current
    if (!s) return
    if (s.hl) {
      s.scene.remove(s.hl)
      disposeObject(s.hl)
      s.hl = undefined
    }
    if (!highlight || highlight.length < 3 || !s.box) return
    // layout mm (top-left origin, y down)  ->  model mm (bin centred on the origin, y up)
    const box = s.box
    const pts = highlight.map(([x, y]) => new THREE.Vector2(box.min.x + x, box.max.y - y))
    const shape = new THREE.Shape(pts)
    const color = new THREE.Color(theme.color('modelSelected'))
    const group = new THREE.Group()
    const fill = new THREE.Mesh(
      new THREE.ShapeGeometry(shape),
      new THREE.MeshBasicMaterial({
        color,
        transparent: true,
        opacity: 0.45,
        side: THREE.DoubleSide,
        depthWrite: false,
      }),
    )
    const outline = new THREE.LineLoop(
      new THREE.BufferGeometry().setFromPoints(pts.map((p) => new THREE.Vector3(p.x, p.y, 0))),
      new THREE.LineBasicMaterial({ color }),
    )
    group.add(fill, outline)
    group.position.z = box.max.z + 0.3
    group.renderOrder = 10
    s.scene.add(group)
    s.hl = group
  }, [highlight, meshVersion, ready])

  return (
    <div className="viewerwrap" style={{ minHeight, cursor: panMode ? 'move' : cursor }}>
      <div className="canvas" ref={mount} style={{ minHeight }} />
      <div className="viewbtns" role="toolbar" aria-label="3D view">
        {(['home', 'top', 'front', 'side'] as ViewName[]).map((v) => (
          <button key={v} title={VIEW_TITLES[v]} onClick={() => setView(v)}>
            {v}
          </button>
        ))}
        <button
          className={panMode ? 'on' : ''}
          aria-pressed={panMode}
          title="Pan mode: drag slides the view instead of turning it"
          onClick={() => setPanMode((v) => !v)}
        >
          pan
        </button>
        <button
          title="Mouse and trackpad settings"
          aria-label="Mouse and trackpad settings"
          aria-expanded={showPrefs}
          onClick={() => setShowPrefs((v) => !v)}
        >
          ⚙
        </button>
      </div>
      {showPrefs && (
        <div className="viewprefs" role="group" aria-label="Mouse and trackpad settings">
          <label title="Swap which way the model turns when you drag sideways">
            <input
              type="checkbox"
              checked={prefs.flipH}
              onChange={(e) => setPrefs({ ...prefs, flipH: e.target.checked })}
            />{' '}
            flip left/right orbit
          </label>
          <label title="Swap which way the model tilts when you drag up or down">
            <input
              type="checkbox"
              checked={prefs.flipV}
              onChange={(e) => setPrefs({ ...prefs, flipV: e.target.checked })}
            />{' '}
            flip up/down orbit
          </label>
          <label title="Swap which scroll direction zooms in">
            <input
              type="checkbox"
              checked={prefs.flipZoom}
              onChange={(e) => setPrefs({ ...prefs, flipZoom: e.target.checked })}
            />{' '}
            flip scroll zoom
          </label>
          <label>
            two-finger scroll
            <select
              value={prefs.scroll}
              title="What a two-finger trackpad scroll does"
              onChange={(e) => setPrefs({ ...prefs, scroll: e.target.value as Prefs['scroll'] })}
            >
              <option value="auto">auto (trackpad pans, wheel zooms)</option>
              <option value="pan">always pans</option>
              <option value="zoom">always zooms</option>
            </select>
          </label>
          <span className="hint">
            pinch or mouse wheel zooms to the cursor · right/middle-drag or shift-drag pans · arrow keys pan
            once the view is clicked · saved in this browser
          </span>
        </div>
      )}
      {busy && <BusyOverlay text={busyText ?? 'Building the 3D model…'} />}
      <div className="viewhint">
        {panMode ? 'Pan mode: drag to slide · ' : drag ? 'Drag a pocket to move it · ' : ''}
        {panMode ? '' : 'drag: orbit · '}right-drag: pan · scroll or pinch: zoom
      </div>
    </div>
  )
})
export default StlViewer
