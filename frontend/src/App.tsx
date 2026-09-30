import { useCallback, useEffect, useRef, useState } from 'react'
import { TRACE_STEPS, TRACE_STEP_HINTS, Session, Update, initialSession } from './types'
import { api, errMsg } from './api'
import { snapshot } from './project'
import { isMeaningful, loadSession, saveSession } from './session'
import { ErrorBoundary, Toasts, toast } from './ui'
import HomeView from './HomeView'
import LibraryView from './LibraryView'
import UploadStep from './steps/UploadStep'
import PaperStep from './steps/PaperStep'
import ToolsStep from './steps/ToolsStep'
import DesignStep from './steps/DesignStep'

type SaveState = 'idle' | 'saving' | 'saved' | 'error'
let restoredToastShown = false

/** Header project name: click to rename. Saved to the Library when the project exists there, else just kept in the session. */
function ProjectName({
  name,
  projectId,
  onRename,
}: {
  name: string
  projectId?: string
  onRename: (n: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(name)
  const inputRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (editing) {
      inputRef.current?.focus()
      inputRef.current?.select()
    }
  }, [editing])
  const start = () => {
    setDraft(name)
    setEditing(true)
  }
  async function commit() {
    setEditing(false)
    const n = draft.trim()
    if (!n || n === name) return
    onRename(n)
    if (projectId) {
      try {
        await api.libraryRename(projectId, n)
        toast(`Renamed to ${n}`)
      } catch (e) {
        toast(`Renamed here, but the Library did not update: ${errMsg(e)}`, 'err', 6000)
      }
    } else toast(`Renamed to ${n}`)
  }
  if (editing) {
    return (
      <input
        ref={inputRef}
        className="projname"
        aria-label="Project name"
        title="Type a new name, then press Enter"
        value={draft}
        maxLength={80}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') commit()
          if (e.key === 'Escape') setEditing(false)
        }}
      />
    )
  }
  return (
    <button
      className="projname"
      title="Click to rename the project"
      aria-label={`Project name: ${name || 'untitled'}. Click to rename`}
      onClick={start}
    >
      {name || 'Untitled project'}
    </button>
  )
}

export default function App() {
  const [s, setS] = useState<Session>(() => loadSession() ?? initialSession)
  const update = useCallback<Update>(
    (patch) => setS((prev) => ({ ...prev, ...(typeof patch === 'function' ? patch(prev) : patch) })),
    [],
  )
  const canGo = (i: number) =>
    i === 0 || (i === 1 && !!s.upload) || (i === 2 && !!s.rect) || (i === 3 && s.tools.length > 0)
  const sRef = useRef(s)
  sRef.current = s

  // ---- (a) local persistence: whole session, debounced 300 ms
  useEffect(() => {
    if (!restoredToastShown) {
      restoredToastShown = true
      if (isMeaningful(s)) toast('Restored your session', 'info')
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const h = setTimeout(() => saveSession(s), 300)
    return () => clearTimeout(h)
  }, [s])

  // ---- (b) server autosave: 3 s after tools/bin/corners change, right away on a step change
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const lastSaved = useRef<string>('')
  const prevStep = useRef(s.step)
  const inFlight = useRef(false)
  const dirty = useRef(false) // a change arrived while a save was in flight: save again after
  const immediate = useRef(false) // a step change is pending: save on the next tick, not after the debounce
  const retryTimer = useRef<number | null>(null)

  const doSave = useCallback(async () => {
    const cur = sRef.current
    if (!cur.projectId) return
    const key = JSON.stringify({ t: cur.tools, b: cur.bin, c: cur.corners, s: cur.step, v: cur.view })
    if (key === lastSaved.current) {
      immediate.current = false
      return
    }
    if (inFlight.current) {
      dirty.current = true
      return
    }
    inFlight.current = true
    immediate.current = false
    dirty.current = false
    setSaveState('saving')
    try {
      await api.saveState(cur.projectId, snapshot(cur))
      lastSaved.current = key
      setSaveState('saved')
    } catch (e) {
      console.warn('autosave failed:', errMsg(e))
      setSaveState('error')
      if (retryTimer.current) clearTimeout(retryTimer.current)
      retryTimer.current = window.setTimeout(() => {
        retryTimer.current = null
        doSave()
      }, 8000)
    } finally {
      inFlight.current = false
      if (dirty.current) {
        dirty.current = false
        doSave()
      }
    }
  }, [])

  // A different project: forget the last key (it belongs to the other project) and treat the step it
  // opens on as the starting point, so opening does not fire an instant re-save of unchanged state.
  // Declared before the autosave effect so it runs first in the same commit.
  useEffect(() => {
    lastSaved.current = ''
    prevStep.current = s.step
    if (!s.projectId) setSaveState('idle')
  }, [s.projectId]) // eslint-disable-line react-hooks/exhaustive-deps
  // Deps are the objects themselves, not a JSON key: hashing every polygon on each render (every
  // pointermove while dragging) is expensive, and re-arming a 3 s timer is not.
  useEffect(() => {
    if (!s.projectId || (s.view !== 'trace' && s.view !== 'bin')) return
    if (prevStep.current !== s.step) immediate.current = true
    prevStep.current = s.step
    const h = setTimeout(doSave, immediate.current ? 0 : 3000)
    return () => clearTimeout(h)
  }, [s.tools, s.bin, s.corners, s.step, s.view, s.projectId, doSave])
  useEffect(
    () => () => {
      if (retryTimer.current) clearTimeout(retryTimer.current)
    },
    [],
  )

  const showSave = !!s.projectId && (s.view === 'trace' || s.view === 'bin') && saveState !== 'idle'

  return (
    <>
      <header>
        <button className="brand" title="Back to the home screen" onClick={() => update({ view: 'home' })}>
          <b>Gridfinity Tracer</b>
        </button>
        {(s.view === 'trace' || s.view === 'bin') && (
          <ProjectName
            name={s.projectName}
            projectId={s.projectId}
            onRename={(projectName) => update({ projectName })}
          />
        )}
        {showSave && (
          <span
            className={'savestate' + (saveState === 'error' ? ' err' : '')}
            role="status"
            aria-live="polite"
          >
            {saveState === 'saving' && (
              <>
                <span className="spinner" />
                Saving…
              </>
            )}
            {saveState === 'saved' && 'Saved'}
            {saveState === 'error' && (
              <>
                Couldn't save, will retry{' '}
                <button className="ghost" title="Try saving to the Library again now" onClick={doSave}>
                  Retry now
                </button>
              </>
            )}
          </span>
        )}
        <nav className="steps">
          {s.view === 'trace' &&
            TRACE_STEPS.map((name, i) => {
              const done = i < s.step
              const ok = canGo(i)
              return (
                <button
                  key={name}
                  className={i === s.step ? 'active' : done ? 'done' : ''}
                  disabled={!ok}
                  aria-current={i === s.step ? 'step' : undefined}
                  title={
                    ok
                      ? `${TRACE_STEP_HINTS[i]}${done ? ' (done, click to go back)' : ''}`
                      : 'Finish the earlier steps first'
                  }
                  onClick={() => update({ step: i })}
                >
                  {done ? '✓ ' : ''}
                  {i + 1}. {name}
                </button>
              )
            })}
          {s.view === 'bin' && (
            <button className="active" aria-current="page" title="A plain Gridfinity bin, no photo needed">
              Bin
            </button>
          )}
          <button
            className={'tab' + (s.view === 'home' ? ' active' : '')}
            title="Start something new or reopen a recent project"
            onClick={() => update({ view: 'home' })}
          >
            Home
          </button>
          <button
            className={'tab' + (s.view === 'library' ? ' active' : '')}
            title="Every project, photo and download you have made"
            onClick={() => update({ view: 'library' })}
          >
            Library
          </button>
        </nav>
      </header>
      <main
        className={
          s.view === 'home' || s.view === 'library' || (s.view === 'trace' && s.step === 0) ? 'sidefirst' : ''
        }
      >
        <ErrorBoundary>
          {s.view === 'home' && <HomeView update={update} />}
          {s.view === 'library' && <LibraryView update={update} />}
          {s.view === 'bin' && <DesignStep s={s} update={update} />}
          {s.view === 'trace' && s.step === 0 && <UploadStep s={s} update={update} />}
          {s.view === 'trace' && s.step === 1 && <PaperStep s={s} update={update} />}
          {s.view === 'trace' && s.step === 2 && <ToolsStep s={s} update={update} />}
          {s.view === 'trace' && s.step === 3 && <DesignStep s={s} update={update} />}
        </ErrorBoundary>
      </main>
      <Toasts />
    </>
  )
}
