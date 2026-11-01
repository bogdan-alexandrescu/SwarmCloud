// MOCK-HOOK FILE
/**
 * A SMALL INLINE-SVG DIAGRAM: boxes on a grid and arrows between them, for an
 * architecture, a data flow, a user flow or a data model. Fed by fixture data
 * (`Diagram` in fixtures.ts): each node names its column and row, so the
 * layout is the data's and nothing is measured at run time.
 *
 * A node's tone says where it stands: `new` (this work adds it, a solid edge),
 * `existing` (already there, the soft edge) or `outside` (a service we call,
 * dashed). The legend says so in words.
 *
 * DRAWN AT ITS OWN SIZE, NEVER SCALED DOWN: a scaled SVG would take its text
 * under the 12px floor, so a diagram wider than its card scrolls sideways.
 */
import { useId } from 'react'

import type { Diagram as DiagramData, DiagramNode } from './fixtures'

const BW = 128
const BH = 46
const GX = 64
const GY = 34
const PAD = 8

interface Box { x: number; y: number; n: DiagramNode }

function edgePoint(from: Box, to: Box): [number, number] {
  // Where the line between the two centres leaves `from`'s rectangle.
  const cx = from.x + BW / 2
  const cy = from.y + BH / 2
  const dx = to.x + BW / 2 - cx
  const dy = to.y + BH / 2 - cy
  if (dx === 0 && dy === 0) return [cx, cy]
  const sx = dx === 0 ? Infinity : BW / 2 / Math.abs(dx)
  const sy = dy === 0 ? Infinity : BH / 2 / Math.abs(dy)
  const s = Math.min(sx, sy)
  return [cx + dx * s, cy + dy * s]
}

export function Diagram({ data, label, className }: { data: DiagramData; label?: string; className?: string }) {
  const mid = useId().replace(/:/g, '')
  const cols = Math.max(...data.nodes.map((n) => n.col)) + 1
  const rows = Math.max(...data.nodes.map((n) => n.row)) + 1
  const W = cols * BW + (cols - 1) * GX + PAD * 2
  const H = rows * BH + (rows - 1) * GY + PAD * 2
  const boxes = new Map<string, Box>(
    data.nodes.map((n) => [n.id, { n, x: PAD + n.col * (BW + GX), y: PAD + n.row * (BH + GY) }]),
  )
  const tones = new Set(data.nodes.map((n) => n.tone ?? 'new'))
  return (
    <figure className={`vi-diagram${className ? ` ${className}` : ''}`}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label ?? `${data.kind} diagram: ${data.nodes.map((n) => n.label).join(', ')}`} width={W} height={H}>
        <defs>
          <marker id={`ah${mid}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" className="vi-dg-head" />
          </marker>
        </defs>
        {data.edges.map((e) => {
          const a = boxes.get(e.from)
          const b = boxes.get(e.to)
          if (a === undefined || b === undefined) return null
          const [x1, y1] = edgePoint(a, b)
          const [x2, y2] = edgePoint(b, a)
          const lx = (x1 + x2) / 2
          const ly = (y1 + y2) / 2
          return (
            <g key={`${e.from}-${e.to}`} className="vi-dg-edge">
              <line x1={x1} y1={y1} x2={x2} y2={y2} markerEnd={`url(#ah${mid})`} />
              {e.label && (
                <text x={lx} y={ly - 5} textAnchor="middle" className="vi-dg-elabel">
                  {e.label}
                </text>
              )}
            </g>
          )
        })}
        {[...boxes.values()].map(({ n, x, y }) => (
          <g key={n.id} className={`vi-dg-node is-${n.tone ?? 'new'}`}>
            <rect x={x} y={y} width={BW} height={BH} rx="8" />
            <text x={x + BW / 2} y={n.sub ? y + 20 : y + BH / 2 + 5} textAnchor="middle" className="vi-dg-label">
              {n.label}
            </text>
            {n.sub && (
              <text x={x + BW / 2} y={y + 36} textAnchor="middle" className="vi-dg-sub">
                {n.sub}
              </text>
            )}
          </g>
        ))}
      </svg>
      {tones.size > 1 && (
        <figcaption className="vi-dg-legend">
          {tones.has('new') && <span className="vi-dg-key is-new">new</span>}
          {tones.has('existing') && <span className="vi-dg-key is-existing">already there</span>}
          {tones.has('outside') && <span className="vi-dg-key is-outside">outside service</span>}
        </figcaption>
      )}
    </figure>
  )
}
