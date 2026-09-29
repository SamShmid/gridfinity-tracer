export type Pt = [number, number]
export type Poly = Pt[]

export interface UploadResponse {
  image_id: string; width: number; height: number; corners: Poly; detected: boolean
  orientation: 'landscape' | 'portrait'; project_id: string
}
export interface LibraryExport { id: string; project_id: string; created_at: number; format: string; filename: string; size: number; summary: string }
export interface LibraryProject {
  id: string; created_at: number; updated_at: number; name: string; paper: string; original_filename: string; original_ext: string
  original_size: number; image_id: string; width: number; height: number; notes: string; exports: LibraryExport[]; has_state: boolean
  has_snapshot: boolean; has_photo: boolean
  expires_at?: number | null   // unix seconds; projects are cleaned up after this
}
export interface RectifyResponse {
  rect_id: string; width: number; height: number; px_per_mm: number; margin_mm: number
  paper_w_mm: number; paper_h_mm: number; paper_px: [number, number, number, number]
}
export interface ToolOutline { polygon: Poly; area_mm2: number; bbox: number[]; centroid: Pt; source: string }
export interface BinIn {
  grid_x: number; grid_y: number; height_units: number; lip: 'regular' | 'none'
  holes: 'none' | 'magnet' | 'screw' | 'magnet_screw'; solid: boolean
  dividers_x: number; dividers_y: number; scoop: boolean; scoop_radius: number
  label_tab: boolean; label_width: number
}
export interface FingerHoleIn { x: number; y: number; diameter: number; length: number; angle_deg: number; extra_depth: number }
export interface PocketIn { polygon: Poly; depth: number; finger_holes: FingerHoleIn[] }

/** Every failed request becomes one of these. `message` is safe to show to people as-is. */
export class ApiError extends Error {
  status: number
  detail: string
  constructor(message: string, status = 0, detail = '') { super(message); this.name = 'ApiError'; this.status = status; this.detail = detail }
}

/** Text for an unknown thrown value: ApiError/Error message, never "Error: ..." or a URL. */
export function errMsg(e: unknown): string {
  if (e instanceof Error) return e.message || 'Something went wrong.'
  if (typeof e === 'string') return e
  return 'Something went wrong.'
}
export const isAbort = (e: unknown) => e instanceof DOMException && e.name === 'AbortError'

type Ctx = 'upload' | 'generate' | 'generic'

function friendly(status: number, detail: string, ctx: Ctx): string {
  const d = detail.trim()
  const low = d.toLowerCase()
  if (status === 413) return ctx === 'upload' ? 'That photo is too big. Try one under 40 MB.' : 'That is too much data to send. Try a simpler design.'
  if (status === 415 || (status === 400 && (low.includes('decode') || low.includes('image')))) {
    return 'That file does not look like a photo we can read. Try a JPEG, PNG, HEIC or WebP.'
  }
  if (status === 408 || status === 504 || low.includes('timed out') || low.includes('timeout')) return 'That took too long. Try again.'
  if (status === 404 && (low.includes('rect') || low.includes('image'))) return 'The server no longer has this photo. Upload it again to continue.'
  if (status === 404) return d || 'Not found.'
  if (status === 422) return d ? `Some values are out of range: ${d}` : 'Some values are out of range. Check the numbers and try again.'
  if (status === 429) return 'The server is busy right now. Wait a moment and try again.'
  if (status >= 500) return ctx === 'generate' ? (d ? `Could not build the model: ${stripPrefix(d)}` : 'Could not build the model. Try again.') : (d ? stripPrefix(d) : 'The server hit a problem. Try again.')
  return d ? stripPrefix(d) : `Request failed (${status}).`
}
const stripPrefix = (d: string) => d.replace(/^CAD generation failed:\s*/i, '').replace(/^Could not decode image:\s*/i, '')

/** Turn a non-OK response into an ApiError. Reads JSON {"detail": ...} when present, else the body text, else the status text. */
async function fail(r: Response, ctx: Ctx = 'generic'): Promise<never> {
  let detail = ''
  try {
    const text = await r.text()
    try {
      const j = JSON.parse(text)
      const dd = j?.detail
      if (typeof dd === 'string') detail = dd
      else if (Array.isArray(dd)) detail = dd.map((x: { msg?: string; loc?: unknown[] }) => `${Array.isArray(x.loc) ? x.loc.slice(-1)[0] + ': ' : ''}${x.msg ?? ''}`).join('; ')
      else if (dd != null) detail = JSON.stringify(dd)
      else if (typeof j?.message === 'string') detail = j.message
      else if (text && !text.startsWith('<')) detail = text
    } catch { if (text && !text.startsWith('<')) detail = text }
  } catch { /* body unreadable */ }
  if (!detail) detail = r.statusText || ''
  throw new ApiError(friendly(r.status, detail, ctx), r.status, detail)
}

/** fetch() that only ever throws ApiError (or the caller's own AbortError). */
async function req(url: string, init: RequestInit, ctx: Ctx = 'generic'): Promise<Response> {
  let r: Response
  try { r = await fetch(url, init) }
  catch (e) {
    if (isAbort(e)) throw e
    throw new ApiError('Cannot reach the server. Check that it is running and try again.', 0, errMsg(e))
  }
  if (!r.ok) await fail(r, ctx)
  return r
}

async function j<T>(url: string, body?: unknown, method = 'POST', signal?: AbortSignal): Promise<T> {
  const r = await req(url, {
    method, headers: body ? { 'content-type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined, signal,
  })
  return r.json()
}

export const api = {
  paperSizes: () => j<Record<string, { w_mm: number; h_mm: number }>>('/api/paper-sizes', undefined, 'GET'),
  upload: async (file: File, paper: string, customW?: number, customH?: number, name?: string) => {
    const fd = new FormData(); fd.append('file', file); fd.append('paper_size', paper); if (name) fd.append('name', name)
    if (paper === 'custom' && customW && customH) { fd.append('custom_w_mm', String(customW)); fd.append('custom_h_mm', String(customH)) }
    const r = await req('/api/upload', { method: 'POST', body: fd }, 'upload')
    return (await r.json()) as UploadResponse
  },
  rectify: (body: { image_id: string; corners: Poly; paper: string; custom_w_mm?: number; custom_h_mm?: number; orientation: string }) =>
    j<RectifyResponse>('/api/rectify', body),
  detectAuto: (rect_id: string, method: 'auto' | 'classical', min_area_mm2 = 150) =>
    j<{ tools: ToolOutline[] }>('/api/detect/auto', { rect_id, method, min_area_mm2 }),
  detectSam: (rect_id: string, points: { x: number; y: number; label: number }[]) =>
    j<{ tools: ToolOutline[] }>('/api/detect/sam', { rect_id, points }),
  refine: (rect_id: string, polygon: Poly) => j<{ tools: ToolOutline[] }>('/api/detect/refine', { rect_id, polygon }),
  samWarm: (rect_id: string) => j<{ ok: boolean }>('/api/sam/warm', { rect_id }),
  offset: (body: { polygon: Poly; clearance_mm: number; printer_offset_mm?: number; gaussian_mm?: number; tolerance_mm?: number; smooth_mm?: number; bridge_mm?: number; snap?: boolean; symmetric?: boolean; convex?: boolean; tool_height_mm?: number; camera_distance_mm?: number }) =>
    j<{ polygon: Poly; area_mm2: number; bbox: number[]; straighten_deg: number }>('/api/polygon/offset', body),
  generate: async (body: { bin: BinIn; pockets: PocketIn[]; format: 'stl' | '3mf' | 'step'; filename?: string; tolerance?: number; project_id?: string; save?: boolean; state?: unknown }, signal?: AbortSignal) => {
    const r = await req('/api/generate', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body), signal }, 'generate')
    return r.blob()
  },
  library: () => j<{ projects: LibraryProject[] }>('/api/library', undefined, 'GET'),
  libraryGet: (id: string) => j<LibraryProject & { state: any }>(`/api/library/${id}`, undefined, 'GET'),
  libraryRename: (id: string, name: string) => j<{ ok: boolean }>(`/api/library/${id}`, { name }, 'PUT'),
  libraryDelete: (id: string) => j<{ ok: boolean }>(`/api/library/${id}`, undefined, 'DELETE'),
  libraryTouch: (id: string) => j<{ ok: boolean }>(`/api/library/${id}/touch`, undefined, 'PUT'),
  saveState: (id: string, state: unknown) => j<{ ok: boolean }>(`/api/library/${id}/state`, state),
  libraryNew: (name: string) => j<{ project_id: string }>('/api/library/new', { name }),
  saveSnapshot: async (id: string, dataUrl: string) => {
    const blob = await (await fetch(dataUrl)).blob()
    const fd = new FormData(); fd.append('file', blob, 'snapshot.png')
    await req(`/api/library/${id}/snapshot`, { method: 'POST', body: fd })
  },
  exportBlob: async (eid: string) => (await req(`/api/library/exports/${eid}?download=false`, { method: 'GET' })).blob(),
}
