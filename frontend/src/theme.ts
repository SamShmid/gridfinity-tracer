/**
 * THE design system. Every colour, font, size, radius, and shadow in the app comes from here.
 *
 *  - CSS gets them as custom properties on :root (applyTheme() writes them at startup).
 *  - TypeScript (three.js viewer, SVG overlays) imports `theme` directly.
 *
 * Change a value here and it changes everywhere. Nothing else should hard-code a colour.
 */

export const fonts = {
  ui: "'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
  mono: "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
  size: { xs: '11.5px', sm: '13px', base: '14.5px', md: '16px', lg: '18px', xl: '24px', xxl: '30px' },
  weight: { normal: 400, medium: 500, semibold: 600, bold: 700 },
  lineHeight: 1.45,
}

export const space = { 1: '4px', 2: '8px', 3: '12px', 4: '16px', 5: '20px', 6: '24px', 8: '32px', 10: '40px' }
export const radius = { sm: '6px', md: '10px', lg: '14px', pill: '999px' }
export const layout = { sidebar: '380px', header: '52px', gutter: '16px' }

/** Semantic colours. `light` and `dark` must define the same keys. */
export const colors = {
  light: {
    bg: '#f3f4f7', // page background
    panel: '#ffffff', // cards, sidebar, canvases
    panelAlt: '#f7f8fa', // inputs, subtle areas
    ink: '#16191f', // primary text
    muted: '#66707d', // secondary text
    line: '#dfe3e9', // borders
    lineStrong: '#bfc6cf',
    accent: '#2563eb', // primary actions, selection
    accentInk: '#ffffff',
    accentSoft: 'rgba(37, 99, 235, 0.14)',
    ok: '#15803d',
    warn: '#b45309',
    danger: '#b91c1c',
    dangerSoft: 'rgba(185, 28, 28, 0.16)',
    // domain colours
    trace: '#2563eb', // traced outline
    pocket: '#f59e0b', // pocket outline with clearance, finger cuts
    grid: '#c4cad3', // 42 mm grid lines
    model: '#4f8ef7', // 3D mesh
    modelSelected: '#f59e0b',
    floor: '#e6e9ee', // 3D grid floor lines
    floorMajor: '#aab2bd',
    tip: '#eaf1ff', // first-visit callouts
    shadow: '0 1px 2px rgba(16, 24, 40, 0.06), 0 1px 3px rgba(16, 24, 40, 0.10)',
  },
  dark: {
    bg: '#0f1216',
    panel: '#171b21',
    panelAlt: '#1e232b',
    ink: '#e7eaee',
    muted: '#98a2ae',
    line: '#2a313b',
    lineStrong: '#3b4450',
    accent: '#5b8def',
    accentInk: '#ffffff',
    accentSoft: 'rgba(91, 141, 239, 0.22)',
    ok: '#3fb950',
    warn: '#e3a008',
    danger: '#f16b6b',
    dangerSoft: 'rgba(241, 107, 107, 0.2)',
    trace: '#5b8def',
    pocket: '#f5b041',
    grid: '#39424e',
    model: '#5b8def',
    modelSelected: '#f5b041',
    floor: '#262c35',
    floorMajor: '#414b58',
    tip: '#1b2536',
    shadow: '0 1px 2px rgba(0, 0, 0, 0.5)',
  },
}
export type ColorKey = keyof typeof colors.light

export type Mode = 'light' | 'dark'
export function currentMode(): Mode {
  const forced = document.documentElement.dataset.theme as Mode | undefined
  if (forced === 'light' || forced === 'dark') return forced
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/** Runtime accessor for TS consumers (three.js etc.). */
export const theme = {
  color: (k: ColorKey, mode: Mode = currentMode()) => colors[mode][k],
  fonts,
  space,
  radius,
  layout,
}

const kebab = (s: string) => s.replace(/[A-Z]/g, (m) => '-' + m.toLowerCase()).replace(/^(\d)/, 'n$1')

/** Write every token to :root as CSS variables: --c-accent, --f-ui, --fs-md, --sp-4, --r-md, --l-sidebar ... */
export function applyTheme(mode: Mode = currentMode()) {
  const root = document.documentElement.style
  for (const [k, v] of Object.entries(colors[mode])) root.setProperty(`--c-${kebab(k)}`, v)
  root.setProperty('--f-ui', fonts.ui)
  root.setProperty('--f-mono', fonts.mono)
  root.setProperty('--f-lh', String(fonts.lineHeight))
  for (const [k, v] of Object.entries(fonts.size)) root.setProperty(`--fs-${k}`, v)
  for (const [k, v] of Object.entries(fonts.weight)) root.setProperty(`--fw-${k}`, String(v))
  for (const [k, v] of Object.entries(space)) root.setProperty(`--sp-${k}`, v)
  for (const [k, v] of Object.entries(radius)) root.setProperty(`--r-${k}`, v)
  for (const [k, v] of Object.entries(layout)) root.setProperty(`--l-${k}`, v)
  document.documentElement.dataset.mode = mode
}

/** Re-apply when the OS theme flips. */
export function watchTheme() {
  const mq = window.matchMedia?.('(prefers-color-scheme: dark)')
  mq?.addEventListener?.('change', () => applyTheme())
}
