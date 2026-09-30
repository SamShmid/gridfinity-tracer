import { Component, ReactNode, useEffect, useState } from 'react'

/** Translucent overlay for a canvas while work is in progress. `block` also swallows pointer events. */
export function BusyOverlay({ text, block = false }: { text: string; block?: boolean }) {
  return (
    <div className={'busyoverlay' + (block ? ' block' : '')} role="status" aria-live="polite">
      <div className="busybox">
        <span className="spinner" />
        {text}
      </div>
    </div>
  )
}

// ---- toasts: tiny pub/sub so any module can call toast() without context plumbing
type ToastAction = { label: string; onClick: () => void; title?: string }
type Toast = { id: number; text: string; kind: 'ok' | 'info' | 'err'; action?: ToastAction }
let toasts: Toast[] = []
let nextToast = 1
const listeners = new Set<(t: Toast[]) => void>()
const emit = () => listeners.forEach((l) => l(toasts))
const dismiss = (id: number) => {
  toasts = toasts.filter((x) => x.id !== id)
  emit()
}

/** Show a short confirmation. `action` adds a button (for example Undo) that also closes the toast. */
export function toast(text: string, kind: Toast['kind'] = 'ok', ms = 3500, action?: ToastAction) {
  const t: Toast = { id: nextToast++, text, kind, action }
  toasts = [...toasts, t]
  emit()
  setTimeout(() => dismiss(t.id), ms)
}

export function Toasts() {
  const [list, setList] = useState<Toast[]>(toasts)
  useEffect(() => {
    listeners.add(setList)
    return () => {
      listeners.delete(setList)
    }
  }, [])
  if (!list.length) return null
  return (
    <div className="toasts" aria-live="polite">
      {list.map((t) => (
        <div key={t.id} className={'toast ' + t.kind} role="status">
          {t.text}
          {t.action && (
            <button
              className="ghost"
              title={t.action.title ?? t.action.label}
              onClick={() => {
                t.action?.onClick()
                dismiss(t.id)
              }}
            >
              {t.action.label}
            </button>
          )}
        </div>
      ))}
    </div>
  )
}

// ---- first-visit callouts: shown until dismissed, remembered per browser under gt.tip.<id>
const tipKey = (id: string) => `gt.tip.${id}`
const tipSeen = (id: string) => {
  try {
    return localStorage.getItem(tipKey(id)) === '1'
  } catch {
    return false
  }
}

/** A one-line hint for people who have not used this screen before. "Got it" hides it for good (in this browser). */
export function Tip({
  id,
  children,
  className = '',
}: {
  id: string
  children: ReactNode
  className?: string
}) {
  const [hidden, setHidden] = useState(() => tipSeen(id))
  if (hidden) return null
  const close = () => {
    try {
      localStorage.setItem(tipKey(id), '1')
    } catch {}
    setHidden(true)
  }
  return (
    <div className={'tip ' + className} role="note">
      <span aria-hidden="true" className="tipmark">
        ?
      </span>
      <span className="tiptext">{children}</span>
      <button className="ghost" title="Hide this tip" aria-label="Hide this tip" onClick={close}>
        Got it
      </button>
    </div>
  )
}

// ---- error boundary
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }
  static getDerivedStateFromError(error: Error) {
    return { error }
  }
  componentDidCatch(error: Error) {
    console.error(error)
  }
  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="errcard" role="alert">
        <h2>Something went wrong</h2>
        <p className="hint">{this.state.error.message}</p>
        <div className="btnrow" style={{ justifyContent: 'center' }}>
          <button
            className="primary"
            title="Reload the page and keep your work"
            onClick={() => location.reload()}
          >
            Reload
          </button>
          <button
            className="secondary"
            title="Clear the saved session and reload"
            onClick={() => {
              try {
                localStorage.removeItem('gt.session')
              } catch {}
              location.reload()
            }}
          >
            Start fresh
          </button>
        </div>
        <p className="hint">Reload keeps your session. Start fresh clears it if reloading keeps failing.</p>
      </div>
    )
  }
}
