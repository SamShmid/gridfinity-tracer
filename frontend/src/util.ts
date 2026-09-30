import type { LibraryProject } from './api'

/** Parse a number input; keep `prev` when the box is empty or not a number (so clearing a field never sends NaN). */
export function numOr(v: string, prev: number): number {
  if (v.trim() === '') return prev
  const n = Number(v)
  return Number.isFinite(n) ? n : prev
}

/** Unique id for tools. crypto.randomUUID is missing on plain-http LAN origins, so fall back. */
export function uid(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  } catch {
    /* not a secure context */
  }
  return 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10)
}

/** "Deletes in N days" when a project expires within `within` days, else ''. */
export function expiryNote(p: Pick<LibraryProject, 'expires_at'>, within = 15): string {
  if (!p.expires_at) return ''
  const days = Math.ceil((p.expires_at - Date.now() / 1000) / 86400)
  if (days > within) return ''
  if (days <= 0) return 'Deletes today'
  return days === 1 ? 'Deletes tomorrow' : `Deletes in ${days} days`
}

/** "14:05" for default project names ("Trace 14:05"). */
export function stamp(d = new Date()): string {
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/** "Letter", "A4", "Tabloid" from the backend's paper keys. */
export function paperName(key: string): string {
  if (key === 'custom') return 'Custom'
  return /^[a-z]\d/.test(key) ? key.toUpperCase() : key.charAt(0).toUpperCase() + key.slice(1)
}

/** Plain words for where a tool outline came from (the backend's `source` tag stays in a title). */
export function sourceLabel(source: string): string {
  if (source.startsWith('sam')) return 'Click-traced'
  if (source.startsWith('classical')) return 'Contrast-traced'
  if (source === 'refined') return 'Re-traced'
  return 'Auto-traced'
}

/** A safe file name: spaces become dashes, anything but letters, digits, - and _ is dropped. */
export const safeFileName = (s: string) =>
  s
    .trim()
    .replace(/\s+/g, '-')
    .replace(/[^a-zA-Z0-9-_]/g, '')
