// MOCK-HOOK FILE
/**
 * VOICE IT CARDS (mock-ups only): one component per card kind the agent
 * creates as the talk goes, each drawn in the console's own card (`Card`),
 * with its state as a mark and a word, and -- where Bogdan must act -- its
 * action. `VoiceCardView` picks the right one for a fixture card.
 *
 * A CARD THAT NEEDS BOGDAN SAYS SO in its own head ("needs you: ..."), so it
 * is findable inside a long column as well as from `NeedsYou`.
 *
 * A CREDENTIAL IS NEVER SHOWN: the credential card opens the secure form and
 * says whether something was stored; it holds no value to show.
 */
import { useState, type ReactNode } from 'react'

import { Button, Card, Chip, Dialog, ToneMark, UsageTrack } from '../components'
import { Diagram } from './Diagram'
import type {
  CapCard,
  CardTone,
  CredentialCard,
  DecisionCardData,
  DesignCard,
  DiagramCard,
  FailureCard,
  InputCard,
  MockupCard,
  PlanCard,
  ReviewCard,
  RunningCard,
  ShippedCard,
  Sketch,
  TaskCard,
  VoiceCard,
} from './fixtures'
import { money } from './parts'

export const TONE: Record<CardTone, string> = { neutral: 'info', live: 'live', waiting: 'warn', bad: 'bad', done: 'ok' }

/** The credential card's kind word. */
const SECURE_INPUT = 'Secure input'

export const KIND_WORD: Record<VoiceCard['kind'], string> = {
  task: 'Task',
  running: 'Work running',
  review: 'Ready to review',
  design: 'Design proposal',
  diagram: 'Diagram',
  mockup: 'Mock-up',
  plan: 'Plan',
  decision: 'Decision',
  input: 'Question',
  credential: SECURE_INPUT,
  failure: 'Failure',
  shipped: 'Shipped',
  cap: 'Budget cap',
}

export function duration(min: number): string {
  if (min < 60) return `${min} min`
  const h = Math.floor(min / 60)
  const m = min % 60
  return m === 0 ? `${h} h` : `${h} h ${m} min`
}

/** The card's frame: its kind, its title, its state, what it needs, its body and its actions. */
export function CardShell({ card, children, actions, className }: { card: VoiceCard; children?: ReactNode; actions?: ReactNode; className?: string }) {
  const needs = card.needs !== undefined
  return (
    <Card
      level={3}
      id={`card-${card.id}`}
      className={`vi-card is-${card.kind}${needs ? ' is-needs' : ''}${className ? ` ${className}` : ''}`}
      title={
        <>
          <span className="vi-card-kind">{KIND_WORD[card.kind]}</span>
          <span className="vi-card-title">{card.title}</span>
        </>
      }
      action={
        <span className="vi-card-state">
          <ToneMark tone={TONE[card.tone]}>{card.state}</ToneMark>
          <time className="vi-card-at">{card.at}</time>
        </span>
      }
    >
      {needs && (
        <p className="vi-card-needs">
          <ToneMark tone="warn" />
          Needs you: {card.needs}
        </p>
      )}
      {children}
      {actions !== undefined && <div className="vi-card-acts">{actions}</div>}
    </Card>
  )
}

// ---------------------------------------------------------------------------

export function TaskCardView({ card }: { card: TaskCard }) {
  return (
    <CardShell card={card}>
      <p className="vi-card-p">{card.what}</p>
      <p className="vi-card-facts">
        {card.repo && <Chip>{card.repo}</Chip>}
        <span>{card.when ?? 'not scheduled'}</span>
      </p>
    </CardShell>
  )
}

export function RunningCardView({ card, max }: { card: RunningCard; max?: number }) {
  const shown = max === undefined ? card.lanes : card.lanes.slice(0, max)
  const metered = card.lanes.filter((l) => l.cost !== null)
  const total = metered.reduce((s, l) => s + (l.cost ?? 0), 0)
  return (
    <CardShell card={card}>
      {card.phase && <p className="vi-card-facts">{card.phase}</p>}
      <ul className="vi-lanes" aria-label="Lanes">
        {shown.map((l) => (
          <li key={l.name} className={`vi-lane is-${l.state.replace(/ /g, '-')}`}>
            <span className="vi-lane-name">
              <ToneMark tone={l.state === 'done' ? 'ok' : l.state === 'failed' ? 'bad' : l.state === 'waiting on you' ? 'warn' : l.state === 'queued' ? 'info' : 'live'} />
              {l.name}
            </span>
            <span className="vi-lane-step">{l.step}</span>
            <span className="vi-lane-bar">
              <UsageTrack pct={l.pct} meter={{ label: `${l.name}: ${l.pct === null ? 'progress not reported yet' : `${l.pct}%`}`, now: l.pct ?? 0, max: 100 }} />
            </span>
            <span className="vi-lane-pct">{l.pct === null ? 'not reported' : `${l.pct}%`}</span>
            <span className="vi-lane-cost">{l.cost === null ? 'not metered' : money(l.cost)}</span>
          </li>
        ))}
      </ul>
      {max !== undefined && card.lanes.length > max && <p className="vi-card-facts">and {card.lanes.length - max} more lanes</p>}
      <p className="vi-card-facts">
        {money(Math.round(total * 100) / 100)} so far
        {metered.length < card.lanes.length && `, ${card.lanes.length - metered.length} lanes not metered yet`}
      </p>
    </CardShell>
  )
}

export function ReviewCardView({ card }: { card: ReviewCard }) {
  const c = card.checks
  const checksTone = c.failed > 0 ? 'bad' : c.pending > 0 ? 'live' : 'ok'
  const checksWord = c.failed > 0 ? `${c.failed} checks failed` : c.pending > 0 ? `${c.pending} checks running` : `all ${c.passed} checks passed`
  return (
    <CardShell
      card={card}
      actions={
        <>
          <Button kind={card.needs ? 'secondary' : 'primary'} size="sm">Review</Button>
          {card.needs && <Button kind="primary" size="sm">Approve the merge</Button>}
        </>
      }
    >
      <p className="vi-card-facts">
        <Chip>{card.repo} #{card.number}</Chip>
        <span>{card.files} files</span>
        <span className="vi-diffn">+{card.added} −{card.removed}</span>
      </p>
      <p className="vi-card-facts">
        <ToneMark tone={checksTone}>{checksWord}</ToneMark>
      </p>
    </CardShell>
  )
}

export function DesignCardView({ card }: { card: DesignCard }) {
  return (
    <CardShell card={card}>
      <p className="vi-card-p">{card.question}</p>
      <div className="vi-ab">
        {card.options.map((o) => {
          const picked = card.picked === o.key
          return (
            <div key={o.key} className={`vi-ab-opt${picked ? ' is-picked' : ''}${card.recommended === o.key ? ' is-rec' : ''}`}>
              <p className="vi-ab-h">
                <b>{o.key}. {o.name}</b>
                {card.recommended === o.key && <Chip faint>recommended</Chip>}
                {picked && <ToneMark tone="ok">picked</ToneMark>}
              </p>
              <p className="vi-card-p">{o.summary}</p>
              <ul className="vi-ab-list">
                {o.good.map((g) => <li key={g}>Good: {g}</li>)}
                {o.cost.map((g) => <li key={g}>Costs: {g}</li>)}
              </ul>
              {card.picked === null && <Button size="sm" kind={card.recommended === o.key ? 'primary' : 'secondary'}>Pick {o.key}</Button>}
            </div>
          )
        })}
      </div>
    </CardShell>
  )
}

export function DiagramCardView({ card }: { card: DiagramCard }) {
  return (
    <CardShell card={card}>
      <p className="vi-card-facts">{card.diagram.kind}</p>
      <Diagram data={card.diagram} label={`${card.title}, ${card.diagram.kind}`} />
    </CardShell>
  )
}

/** A wireframe thumbnail: a few boxes in the token greys, never a real screen. */
export function SketchThumb({ sketch }: { sketch: Sketch }) {
  const bars = (n: number, x: number, y: number, w: number, gap = 10) =>
    Array.from({ length: n }, (_, i) => <rect key={`${x}-${i}`} x={x} y={y + i * gap} width={w - (i % 2) * 14} height="5" rx="2" />)
  let body: ReactNode
  switch (sketch) {
    case 'calendar':
      body = (
        <>
          {Array.from({ length: 4 }, (_, c) => <rect key={c} className="is-col" x={8 + c * 28} y={20} width={24} height={52} rx="2" />)}
          <rect className="is-hit" x={36} y={30} width={24} height={14} rx="2" />
          <rect className="is-hit" x={92} y={46} width={24} height={18} rx="2" />
        </>
      )
      break
    case 'list':
      body = bars(5, 10, 22, 100)
      break
    case 'form':
      body = (
        <>
          {bars(3, 10, 22, 70, 14)}
          <rect className="is-hit" x={10} y={64} width={36} height={9} rx="2" />
        </>
      )
      break
    case 'grid':
      body = Array.from({ length: 4 }, (_, r) => <rect key={r} className={r === 0 ? 'is-hit' : 'is-col'} x={8} y={20 + r * 13} width={112} height={10} rx="1" />)
      break
    default:
      body = (
        <>
          <rect className="is-col" x={8} y={20} width={52} height={52} rx="3" />
          <rect className="is-col" x={68} y={20} width={52} height={52} rx="3" />
          {bars(2, 14, 28, 36)}
          {bars(2, 74, 28, 36)}
        </>
      )
  }
  return (
    <svg className="vi-sketch" viewBox="0 0 128 80" aria-hidden="true" focusable="false">
      <rect className="is-frame" x="0.5" y="0.5" width="127" height="79" rx="4" />
      <rect className="is-top" x="0.5" y="0.5" width="127" height="12" rx="4" />
      {body}
    </svg>
  )
}

export function MockupCardView({ card }: { card: MockupCard }) {
  return (
    <CardShell card={card}>
      <ul className="vi-thumbs">
        {card.frames.map((f) => {
          const picked = card.picked === f.label
          return (
            <li key={f.label} className={`vi-thumb${picked ? ' is-picked' : ''}`}>
              <SketchThumb sketch={f.sketch} />
              <span className="vi-thumb-label">
                {f.label}
                {picked && <ToneMark tone="ok">picked</ToneMark>}
              </span>
              <span className="vi-thumb-acts">
                <Button kind="ghost" size="sm">Open</Button>
                {card.picked === null && <Button kind="secondary" size="sm">Pick</Button>}
              </span>
            </li>
          )
        })}
      </ul>
    </CardShell>
  )
}

export function PlanCardView({ card }: { card: PlanCard }) {
  const lanes = card.phases.reduce((n, p) => n + p.lanes.length, 0)
  return (
    <CardShell
      card={card}
      actions={
        card.needs ? (
          <>
            <Button kind="primary">{card.goLabel}</Button>
            <Button kind="secondary">Change</Button>
          </>
        ) : undefined
      }
    >
      <p className="vi-plan-sum">
        {card.phases.length} {card.phases.length === 1 ? 'phase' : 'phases'}, {lanes} {lanes === 1 ? 'lane' : 'lanes'}, about {money(card.totalCost)} and {duration(card.totalMinutes)}
      </p>
      <ol className="vi-phases">
        {card.phases.map((p) => (
          <li key={p.name} className={`vi-phase is-${p.state ?? 'later'}`}>
            <p className="vi-phase-h">
              <b>{p.name}</b>
              <span className="vi-phase-est">{p.lanes.length} {p.lanes.length === 1 ? 'lane' : 'lanes'}, about {money(p.cost)}, {duration(p.minutes)}</span>
              {p.state && p.state !== 'later' && <ToneMark tone={p.state === 'done' ? 'ok' : p.state === 'running' ? 'live' : 'warn'}>{p.state}</ToneMark>}
            </p>
            <span className="vi-phase-lanes">
              {p.lanes.map((l) => <Chip key={l}>{l}</Chip>)}
            </span>
          </li>
        ))}
      </ol>
    </CardShell>
  )
}

export function DecisionCardView({ card }: { card: DecisionCardData }) {
  const heard = card.spoken !== undefined
  return (
    <CardShell
      card={card}
      actions={
        card.needs ? (
          heard && card.confirmByClick ? (
            <>
              <Button kind="primary">Confirm: approve</Button>
              <Button kind="ghost">That is not what I said</Button>
            </>
          ) : (
            <>
              <Button kind="primary">Approve</Button>
              <Button kind="secondary">Not yet</Button>
            </>
          )
        ) : undefined
      }
    >
      <p className="vi-card-p">{card.question}</p>
      <dl className="vi-rows">
        {card.rows.map((r, i) => (
          <div key={`${r.k}-${i}`} className="vi-row">
            <dt>{r.k}</dt>
            <dd>{r.v}</dd>
          </div>
        ))}
      </dl>
      {heard && (
        <p className="vi-heard">
          You said: <q>{card.spoken}</q>
          {card.confirmByClick && card.needs && <span> Access changes need a click as well.</span>}
        </p>
      )}
    </CardShell>
  )
}

export function InputCardView({ card }: { card: InputCard }) {
  return (
    <CardShell card={card} actions={card.needs ? <Button kind="primary" size="sm">Send</Button> : undefined}>
      <p className="vi-card-p">{card.question}</p>
      {card.options ? (
        <div className="vi-picks" role="radiogroup" aria-label={card.question}>
          {card.options.map((o) => <Chip key={o} onClick={() => undefined} pressed={card.value === o}>{o}</Chip>)}
        </div>
      ) : (
        <input className="c-inp vi-input" aria-label={card.question} placeholder={card.placeholder ?? 'Type or say it'} defaultValue={card.value ?? ''} />
      )}
    </CardShell>
  )
}

/** SwarmCloud's one-time secret form, as a dialog. The value never leaves the field. */
export function SecureFormDialog({ asks, onClose }: { asks: string; onClose: () => void }) {
  return (
    <Dialog
      title={`Enter the ${asks}`}
      onClose={onClose}
      actions={
        <>
          <Button kind="ghost" onClick={onClose}>Cancel</Button>
          <Button kind="primary" onClick={onClose}>Store it</Button>
        </>
      }
    >
      <p className="vi-card-p">It goes straight to Secret Manager. Voice it never hears, reads or shows it; the captions say only that you entered it.</p>
      <input className="c-inp vi-input" type="password" autoComplete="off" aria-label={asks} />
    </Dialog>
  )
}

export function CredentialCardView({ card }: { card: CredentialCard }) {
  const [open, setOpen] = useState(false)
  return (
    <CardShell card={card} actions={card.entered ? undefined : <Button kind="primary" size="sm" onClick={() => setOpen(true)}>Open the secure form</Button>}>
      <p className="vi-card-p">
        {card.entered ? `Stored in the secure form for ${card.storedAs}. It is not shown here or anywhere.` : `Type it in the secure form, never out loud. It is stored for ${card.storedAs}.`}
      </p>
      {open && <SecureFormDialog asks={card.asks} onClose={() => setOpen(false)} />}
    </CardShell>
  )
}

export function FailureCardView({ card }: { card: FailureCard }) {
  return (
    <CardShell card={card}>
      <p className="vi-card-facts">{card.where}</p>
      <p className="vi-card-p">{card.broke}</p>
      {card.log.length > 0 && (
        <pre className="vi-log" aria-label="Log lines">
          {card.log.join('\n')}
        </pre>
      )}
      <p className="vi-card-p">
        <b>Proposed fix:</b> {card.fix}
      </p>
    </CardShell>
  )
}

export function ShippedCardView({ card }: { card: ShippedCard }) {
  return (
    <CardShell card={card}>
      <p className="vi-card-p">
        <a className="ctl-link vi-url" href={card.url} target="_blank" rel="noreferrer">{card.url}</a>
      </p>
      <ul className="vi-changed" aria-label="What changed">
        {card.changed.map((c) => <li key={c}>{c}</li>)}
      </ul>
      <p className="vi-card-facts">
        <span>cost {card.cost === null ? 'not metered' : money(card.cost)}</span>
        <span>took {card.took}</span>
      </p>
    </CardShell>
  )
}

export function CapCardView({ card }: { card: CapCard }) {
  return (
    <CardShell
      card={card}
      actions={
        <>
          <Button kind="primary" size="sm">Raise the cap</Button>
          <Button kind="secondary" size="sm">Stop here</Button>
        </>
      }
    >
      <p className="vi-card-p">
        <b>{money(card.spent)}</b> of {money(card.cap)} {card.which === 'session' ? 'this session' : 'today'}
      </p>
      <UsageTrack pct={(card.spent / card.cap) * 100} tone={card.spent >= card.cap ? 'is-bad' : 'is-warn'} />
      <ul className="vi-changed">
        {card.stopped.map((s) => <li key={s}>{s}</li>)}
      </ul>
    </CardShell>
  )
}

/** Any fixture card, drawn by its kind. */
export function VoiceCardView({ card }: { card: VoiceCard }) {
  switch (card.kind) {
    case 'task': return <TaskCardView card={card} />
    case 'running': return <RunningCardView card={card} />
    case 'review': return <ReviewCardView card={card} />
    case 'design': return <DesignCardView card={card} />
    case 'diagram': return <DiagramCardView card={card} />
    case 'mockup': return <MockupCardView card={card} />
    case 'plan': return <PlanCardView card={card} />
    case 'decision': return <DecisionCardView card={card} />
    case 'input': return <InputCardView card={card} />
    case 'credential': return <CredentialCardView card={card} />
    case 'failure': return <FailureCardView card={card} />
    case 'shipped': return <ShippedCardView card={card} />
    case 'cap': return <CapCardView card={card} />
  }
}
