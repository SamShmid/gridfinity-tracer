import type { Session } from './types'
import { initialSession } from './types'

export const SESSION_KEY = 'gt.session'

/** Drop steps that the restored data cannot support (a reload mid-request, or an old shape). */
function sanitize(s: Session): Session {
  const out = {
    ...initialSession,
    ...s,
    tools: Array.isArray(s.tools) ? s.tools : [],
    bin: { ...initialSession.bin, ...(s.bin ?? {}) },
  }
  if (out.view === 'trace') {
    if (out.step >= 3 && !out.tools.length) out.step = 2
    if (out.step >= 2 && !out.rect) out.step = 1
    if (out.step >= 1 && !out.upload) out.step = 0
  }
  if (!['home', 'trace', 'bin', 'library'].includes(out.view)) out.view = 'home'
  return out
}

export function loadSession(): Session | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY)
    if (!raw) return null
    const s = JSON.parse(raw)
    if (!s || typeof s !== 'object' || typeof s.view !== 'string') return null
    return sanitize(s as Session)
  } catch {
    return null
  }
}

export function saveSession(s: Session) {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify(s))
  } catch {
    /* quota or private mode: ignore */
  }
}

export function clearSession() {
  try {
    localStorage.removeItem(SESSION_KEY)
  } catch {}
}

/** Something worth telling the user we restored (not a blank home screen). */
export const isMeaningful = (s: Session) =>
  s.view !== 'home' && (s.view === 'library' || !!s.upload || s.tools.length > 0 || !!s.projectId)
