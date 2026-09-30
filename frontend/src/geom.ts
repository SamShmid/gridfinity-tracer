import type { Poly, Pt } from './api'

/** Unsigned polygon area (shoelace). */
export function area(p: Poly) {
  let a = 0
  for (let i = 0; i < p.length; i++) {
    const [x0, y0] = p[i],
      [x1, y1] = p[(i + 1) % p.length]
    a += x0 * y1 - x1 * y0
  }
  return Math.abs(a) / 2
}

export function centroid(p: Poly): Pt {
  let a = 0,
    cx = 0,
    cy = 0
  for (let i = 0; i < p.length; i++) {
    const [x0, y0] = p[i],
      [x1, y1] = p[(i + 1) % p.length]
    const f = x0 * y1 - x1 * y0
    a += f
    cx += (x0 + x1) * f
    cy += (y0 + y1) * f
  }
  if (!p.length) return [0, 0]
  if (Math.abs(a) < 1e-9) return [p[0][0], p[0][1]]
  a *= 0.5
  return [cx / (6 * a), cy / (6 * a)]
}

export function bbox(p: Poly) {
  if (!p.length) return { minx: 0, miny: 0, maxx: 0, maxy: 0, w: 0, h: 0 }
  let minx = Infinity,
    miny = Infinity,
    maxx = -Infinity,
    maxy = -Infinity
  for (const [x, y] of p) {
    minx = Math.min(minx, x)
    miny = Math.min(miny, y)
    maxx = Math.max(maxx, x)
    maxy = Math.max(maxy, y)
  }
  return { minx, miny, maxx, maxy, w: maxx - minx, h: maxy - miny }
}

/** Rotate about origin (deg, screen coords) then translate. */
export function transform(p: Poly, rotDeg: number, tx: number, ty: number): Poly {
  const r = (rotDeg * Math.PI) / 180,
    c = Math.cos(r),
    s = Math.sin(r)
  return p.map(([x, y]) => [x * c - y * s + tx, x * s + y * c + ty])
}

/** Re-centre polygon on its centroid so rotation/translation are intuitive. */
export function recentre(p: Poly): Poly {
  const [cx, cy] = centroid(p)
  return p.map(([x, y]) => [x - cx, y - cy])
}

export function pointInPoly(p: Poly, x: number, y: number) {
  let inside = false
  for (let i = 0, j = p.length - 1; i < p.length; j = i++) {
    const [xi, yi] = p[i],
      [xj, yj] = p[j]
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside
  }
  return inside
}

export function toPath(p: Poly) {
  return p.length ? 'M' + p.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join('L') + 'Z' : ''
}

/** Point on the closed polygon's perimeter at fraction s (0..1) of its total length. */
export function pointAt(p: Poly, s: number): Pt {
  const n = p.length
  if (n === 0) return [0, 0]
  const segs: number[] = []
  let total = 0
  for (let i = 0; i < n; i++) {
    const [x0, y0] = p[i],
      [x1, y1] = p[(i + 1) % n]
    const l = Math.hypot(x1 - x0, y1 - y0)
    segs.push(l)
    total += l
  }
  let d = (((s % 1) + 1) % 1) * total
  for (let i = 0; i < n; i++) {
    if (d <= segs[i] || i === n - 1) {
      const [x0, y0] = p[i],
        [x1, y1] = p[(i + 1) % n]
      const f = segs[i] ? d / segs[i] : 0
      return [x0 + (x1 - x0) * f, y0 + (y1 - y0) * f]
    }
    d -= segs[i]
  }
  return p[0]
}

/** Fraction s (0..1) along the perimeter of the point nearest to q. */
export function nearestS(p: Poly, q: Pt): number {
  const n = p.length
  if (n === 0) return 0
  let total = 0,
    best = 0,
    bestD = Infinity,
    acc = 0
  const lens: number[] = []
  for (let i = 0; i < n; i++) {
    const [x0, y0] = p[i],
      [x1, y1] = p[(i + 1) % n]
    lens.push(Math.hypot(x1 - x0, y1 - y0))
    total += lens[i]
  }
  for (let i = 0; i < n; i++) {
    const [x0, y0] = p[i],
      [x1, y1] = p[(i + 1) % n]
    const dx = x1 - x0,
      dy = y1 - y0,
      l2 = dx * dx + dy * dy
    const t = l2 ? Math.max(0, Math.min(1, ((q[0] - x0) * dx + (q[1] - y0) * dy) / l2)) : 0
    const d = Math.hypot(q[0] - (x0 + dx * t), q[1] - (y0 + dy * t))
    if (d < bestD) {
      bestD = d
      best = (acc + lens[i] * t) / total
    }
    acc += lens[i]
  }
  return total > 0 && Number.isFinite(best) ? best : 0
}

/** Regular polygon approximating a circle. */
export function circlePoly(cx: number, cy: number, r: number, n = 24): Poly {
  return Array.from({ length: n }, (_, i) => {
    const a = (2 * Math.PI * i) / n
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)] as Pt
  })
}
