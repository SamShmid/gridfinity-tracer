import { api } from './api'
import { nearestS, recentre } from './geom'
import type { Session, Tool } from './types'
import { defaultBin, depthFor } from './types'
import { uid } from './util'

/** Everything needed to reopen a project later from the Library. */
export function snapshot(s: Session) {
  const { view, step, upload, corners, paper, customW, customH, orientation } = s
  const { rect, rectKey, tools, bin, projectName } = s
  return {
    view,
    step,
    upload,
    corners,
    paper,
    customW,
    customH,
    orientation,
    rect,
    rectKey,
    tools,
    bin,
    projectName,
  }
}

/** Restore a saved project into the session (fills defaults for fields added since it was saved). */
export async function openProject(id: string, update: (p: Partial<Session>) => void): Promise<string | null> {
  const full = await api.libraryGet(id)
  api.libraryTouch(id).catch(() => {}) // keep it from expiring; not fatal if the server lacks the route
  const st = full.state
  if (!st) {
    if (!full.has_photo) {
      update({
        view: 'bin',
        projectId: id,
        projectName: full.name,
        tools: [],
        bin: { ...defaultBin, solid: false },
        upload: undefined,
        rect: undefined,
      })
      return null
    }
    return 'This project has no saved layout yet (only the photo). Upload it again to start over.'
  }
  const tools: Tool[] = (st.tools ?? [])
    .filter((t: Partial<Tool>) => Array.isArray(t.offset) && t.offset.length >= 3)
    .map((t: Partial<Tool>) => {
      const base: Tool = {
        id: t.id || uid(),
        name: t.name ?? 'Tool',
        source: t.source ?? '',
        raw: t.raw ?? [],
        offset: t.offset ?? [],
        clearance: t.clearance ?? 0.5,
        printerOffset: t.printerOffset ?? 0.2,
        gaussian: t.gaussian ?? 1.0,
        tolerance: t.tolerance ?? 0.3,
        smooth: t.smooth ?? 0,
        bridge: t.bridge ?? 1.5,
        snap: t.snap ?? false,
        symmetric: t.symmetric ?? false,
        convex: t.convex ?? false,
        thickness: t.thickness ?? 12,
        sit: t.sit ?? 'custom',
        depth: t.depth ?? 15,
        x: t.x ?? 0,
        y: t.y ?? 0,
        rot: t.rot ?? 0,
        straightRot: t.straightRot ?? 0,
        fingerHoles: (t.fingerHoles ?? []).map((h: any) => ({
          kind: 'notch' as const,
          s: typeof h.s === 'number' ? h.s : nearestS(recentre(t.offset ?? []), [h.x ?? 0, h.y ?? 0]),
          diameter: h.diameter ?? h.width ?? 20,
          extraDepth: Math.min(h.extraDepth ?? 3, 6),
        })),
      }
      base.depth = depthFor(base)
      return base
    })
  const hasPhoto = !!st.upload
  update({
    view: hasPhoto ? 'trace' : 'bin',
    projectId: id,
    projectName: full.name,
    upload: st.upload,
    corners: st.corners,
    paper: st.paper ?? 'letter',
    customW: st.customW ?? 215.9,
    customH: st.customH ?? 279.4,
    orientation: st.orientation ?? 'auto',
    rect: st.rect,
    rectKey: typeof st.rectKey === 'string' ? st.rectKey : undefined,
    tools,
    bin: st.bin ?? defaultBin,
    step: tools.length ? 3 : st.rect ? 2 : 1,
  })
  return null
}
