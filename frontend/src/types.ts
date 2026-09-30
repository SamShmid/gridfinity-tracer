import type { BinIn, Poly, RectifyResponse, UploadResponse } from './api'

/** A finger notch: a round cut centred ON the pocket outline (half in the pocket, half in the bin
 *  material beside the tool) so a fingertip can get under the tool's edge. `s` is the position along
 *  the outline perimeter (0..1). Two opposite notches make a pinch. Never cuts below the solid floor. */
export interface FingerHole {
  kind: 'notch'
  s: number
  diameter: number
  extraDepth: number
}

export type Sit = 'full' | 'threeq' | 'half' | 'custom'

export interface Tool {
  id: string
  name: string
  source: string
  raw: Poly // rectified-image mm, as traced
  offset: Poly // pocket outline after smoothing/clearance (from backend)
  // outline options
  clearance: number // fit clearance mm
  printerOffset: number // FDM compensation mm
  gaussian: number // contour smoothing sigma mm
  tolerance: number
  smooth: number
  bridge: number // fill gaps narrower than 2x this
  snap: boolean
  symmetric: boolean
  convex: boolean
  // depth
  thickness: number // tool thickness mm (user-entered)
  sit: Sit // how deep the tool sits
  depth: number // resulting pocket depth mm (custom when sit === 'custom')
  // layout (bin top-left origin, mm, y down): centroid position + rotation
  x: number
  y: number
  rot: number
  straightRot: number
  fingerHoles: FingerHole[]
}

export type View = 'home' | 'trace' | 'bin' | 'library'

export interface Session {
  view: View
  step: number // trace wizard: 0 upload, 1 paper, 2 tools, 3 design
  projectId?: string
  projectName: string
  upload?: UploadResponse
  corners?: Poly
  paper: string
  customW: number
  customH: number
  orientation: 'auto' | 'landscape' | 'portrait'
  rect?: RectifyResponse
  /** What `rect` was made from (corners, paper, orientation), so "Looks right" can skip a no-op re-straighten. */
  rectKey?: string
  tools: Tool[]
  bin: BinIn
}

/** Session patch, or a function of the latest session for changes that must land after an await
 *  (a detection that finishes after the user has moved on, say). */
export type Update = (p: Partial<Session> | ((prev: Session) => Partial<Session>)) => void

/** The inputs that decide the straightened photo. Same key, same rect. */
export const rectKeyOf = (s: Pick<Session, 'corners' | 'paper' | 'customW' | 'customH' | 'orientation'>) =>
  JSON.stringify({ c: s.corners, p: s.paper, w: s.customW, h: s.customH, o: s.orientation })

export const defaultBin: BinIn = {
  grid_x: 2,
  grid_y: 2,
  height_units: 3,
  lip: 'regular',
  holes: 'none',
  solid: true,
  dividers_x: 0,
  dividers_y: 0,
  scoop: false,
  scoop_radius: 10,
  label_tab: false,
  label_width: 13,
}

export const initialSession: Session = {
  view: 'home',
  step: 0,
  projectName: '',
  paper: 'letter',
  customW: 215.9,
  customH: 279.4,
  orientation: 'auto',
  tools: [],
  bin: defaultBin,
}

/** The one set of step names used everywhere: header pills, panel headings, home how-to. */
export const TRACE_STEPS = ['Photo', 'Paper', 'Trace', 'Design']
export const TRACE_STEP_HINTS = [
  'Upload a photo of the tools on a sheet of paper',
  'Check the four paper corners so the scale is right',
  'Trace each tool and set its clearance and thickness',
  'Arrange the pockets in a bin and download the model',
]

export const MIN_POCKET_FLOOR = 7.0 // keep in sync with backend gridfinity.MIN_POCKET_FLOOR (top of the solid floor)
// Bin bounds the server enforces (backend schemas.py BinIn). Anything past these is a 422, so clamp here first.
export const MAX_GRID = 10
export const MAX_HEIGHT_UNITS = 20
export const PITCH = 42

export function depthFor(t: Pick<Tool, 'thickness' | 'sit' | 'depth'>): number {
  switch (t.sit) {
    case 'full':
      return Math.round((t.thickness + 1) * 2) / 2
    case 'threeq':
      return Math.round(t.thickness * 0.75 * 2) / 2
    case 'half':
      return Math.round(t.thickness * 0.5 * 2) / 2
    default:
      return t.depth
  }
}

export function maxPocketDepth(bin: BinIn): number {
  return bin.height_units * 7 - MIN_POCKET_FLOOR
}
