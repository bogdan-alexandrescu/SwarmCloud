// MOCK-HOOK FILE
// The two invented diagrams of the margin mock-up, drawn as the console would draw a Mermaid
// source after §6.6's strict render: themed by the console's tokens, so light and dark follow.

/** "Which checks a rebase re-runs": a flow, wide or tall (the phone's layout of the same source). */
export function FlowDiagram({ tall = false }: { tall?: boolean }) {
  const box = (x: number, y: number, w: number, label: string, cls = '') => (
    <g className={`mgd-box ${cls}`} key={label}>
      <rect x={x} y={y} width={w} height={40} rx={8} />
      <text x={x + w / 2} y={y + 25} textAnchor="middle">
        {label}
      </text>
    </g>
  )
  const diamond = (cx: number, cy: number, label: string) => (
    <g className="mgd-box is-q" key={label}>
      <path d={`M${cx} ${cy - 30} L${cx + 92} ${cy} L${cx} ${cy + 30} L${cx - 92} ${cy} Z`} />
      <text x={cx} y={cy + 5} textAnchor="middle">
        {label}
      </text>
    </g>
  )
  const arrow = (d: string, label?: string, lx = 0, ly = 0) => (
    <g className="mgd-edge" key={d}>
      <path d={d} markerEnd="url(#mgd-arrow)" />
      {label && (
        <text x={lx} y={ly} textAnchor="middle">
          {label}
        </text>
      )}
    </g>
  )
  if (tall)
    return (
      <svg className="mgd-svg" viewBox="0 0 300 560" role="img" aria-label="Which checks a rebase re-runs">
        <Defs />
        {box(70, 10, 160, 'rebase lands')}
        {arrow('M150 50 V78')}
        {diamond(150, 112, 'result > 24 h old?')}
        {arrow('M150 142 V190', 'no', 166, 170)}
        {arrow('M242 112 H270 V470 H232', 'yes', 280, 104)}
        {diamond(150, 222, 'read paths changed?')}
        {arrow('M150 252 V300', 'no', 166, 280)}
        {arrow('M58 222 H30 V470 H68', 'yes', 20, 214)}
        {box(60, 300, 180, 'keep the cached pass', 'is-keep')}
        {arrow('M150 340 V380')}
        {box(60, 380, 180, 'check counts as passed')}
        {box(68, 450, 164, 're-run the check', 'is-run')}
        {arrow('M150 490 V516')}
        {box(70, 516, 160, 'store new result')}
      </svg>
    )
  return (
    <svg className="mgd-svg" viewBox="0 0 980 190" role="img" aria-label="Which checks a rebase re-runs">
      <Defs />
      {box(10, 75, 130, 'rebase lands')}
      {arrow('M140 95 H178')}
      {diamond(272, 95, 'result > 24 h old?')}
      {arrow('M364 95 H412', 'no', 388, 87)}
      {diamond(506, 95, 'read paths changed?')}
      {arrow('M598 95 H646', 'no', 622, 87)}
      {box(646, 75, 170, 'keep the cached pass', 'is-keep')}
      {arrow('M816 95 H846')}
      {box(846, 75, 124, 'counts as passed')}
      {arrow('M272 125 V160 H646', 'yes', 290, 150)}
      {arrow('M506 125 V160', 'yes', 524, 146)}
      {box(646, 140, 170, 're-run the check', 'is-run')}
      {arrow('M816 160 H846')}
      {box(846, 140, 124, 'store new result')}
      <text className="mgd-note" x={730} y={58} textAnchor="middle">
        a docs-only rebase keeps all 14 passes
      </text>
    </svg>
  )
}

function Defs() {
  return (
    <defs>
      <marker id="mgd-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0 0 L10 5 L0 10 z" className="mgd-head" />
      </marker>
    </defs>
  )
}

/** "Merge step, check cache and CI after a rebase": a sequence, narrow enough for the margin. */
export function SeqDiagram() {
  const lanes = [
    { x: 46, label: 'merge step' },
    { x: 135, label: 'check cache' },
    { x: 224, label: 'CI' },
  ]
  const msg = (y: number, from: number, to: number, label: string, dashed = false) => (
    <g className={`mgd-edge${dashed ? ' is-dash' : ''}`} key={label}>
      <path d={`M${from} ${y} H${to + (to > from ? -4 : 4)}`} markerEnd="url(#mgd-arrow)" />
      <text x={(from + to) / 2} y={y - 6} textAnchor="middle">
        {label}
      </text>
    </g>
  )
  return (
    <svg className="mgd-svg is-seq" viewBox="0 0 270 250" role="img" aria-label="Merge step, check cache and CI after a rebase">
      <Defs />
      {lanes.map((l) => (
        <g key={l.label} className="mgd-lane">
          <rect x={l.x - 42} y={6} width={84} height={28} rx={6} />
          <text x={l.x} y={25} textAnchor="middle">
            {l.label}
          </text>
          <path d={`M${l.x} 34 V244`} />
        </g>
      ))}
      {msg(66, 46, 135, 'key per check')}
      {msg(98, 135, 46, 'hit 12 · miss 2', true)}
      {msg(132, 46, 224, 're-run the 2')}
      {msg(166, 224, 46, 'results', true)}
      {msg(200, 46, 135, 'store, aged 0 h')}
      <text className="mgd-note" x={135} y={236} textAnchor="middle">
        older than 24 h counts as a miss
      </text>
    </svg>
  )
}
