// MOCK-HOOK FILE
// Automate › Pipeline, the picked five-column board (docs/design/plans-pipeline.md §2.1), on
// invented cards: GET /v1/pipeline is not served yet. Its states are reached with ?mock=
// (nowait, partial, notserved) and the board's own filters (?source=, ?state=, ?waits=person).
import { useState } from 'react'
import { Button, Dash } from './components'
import { InfoTip } from './People'
import { InApp } from './Schedules'
import { Screen } from './Shell'
import { CARDS, HIDDEN_ENDED, MOCK_RUN, NEXT_FIRING, type MockCard, type SourceKind, type Stage } from './marginFixture'
import './styles/margin-mock.css'

const STAGES: { id: Stage; label: string }[] = [
  { id: 'found', label: 'Found' },
  { id: 'planned', label: 'Planned / waiting' },
  { id: 'running', label: 'Approved and running' },
  { id: 'pr_ci', label: 'Pull request and CI' },
  { id: 'ended', label: 'Merged or ended' },
]
const SOURCE_WORD: Record<SourceKind, string> = {
  sweep: 'issue sweep',
  failure_review: 'failure review',
  observer: 'observer',
  flake: 'flake',
  hand: 'by hand',
}
const STATE_FILTER: { id: string; label: string; stages: Stage[] }[] = [
  { id: '', label: 'Every stage', stages: ['found', 'planned', 'running', 'pr_ci', 'ended'] },
  ...STAGES.map((s) => ({ id: s.id as string, label: s.label, stages: [s.id] })),
]
/** Which of the server's reads (§3.2 `partial`) a card came from. */
type Read = 'runs' | 'approvals' | 'causes'
const READ_WORD: Record<Read, string> = { runs: 'Runs', approvals: 'Approvals', causes: 'Failure causes' }
const readOf = (c: MockCard): Read => (c.id.startsWith('apr_') ? 'approvals' : c.id.startsWith('fr-') ? 'causes' : 'runs')

const dur = (min: number) => (min < 60 ? `${min} min` : min < 1440 ? `${Math.round(min / 60)} h` : `${Math.round(min / 1440)} d`)
const q = () => new URLSearchParams(typeof location === 'undefined' ? '' : location.search)

export function PipelineMockScreen({ go }: { go: (to: string) => void }) {
  const mock = q().get('mock')
  return (
    <Screen
      title="Pipeline"
      load={async () => ({ status: 'ok' as const, data: 1, fetchedAt: Date.now() })}
    >
      {() => (mock === 'notserved' ? <NotServed go={go} /> : <Board go={go} />)}
    </Screen>
  )
}

function Board({ go }: { go: (to: string) => void }) {
  const p = q()
  const mock = p.get('mock')
  const [source, setSource] = useState<string>(p.get('source') ?? '')
  const [state, setState] = useState<string>(p.get('state') ?? '')
  const [waits, setWaits] = useState(p.get('waits') === 'person')
  const nowait = mock === 'nowait'
  // board-partial: one read failed; the cards it feeds are missing, so every count they enter is a dash.
  const failed: Read[] = mock === 'partial' ? ['causes'] : []
  const missing = (pred: (c: MockCard) => boolean) => failed.find((r) => CARDS.some((c) => readOf(c) === r && pred(c)))
  const all = CARDS.filter((c) => !failed.includes(readOf(c))).map((c) => (nowait ? { ...c, waits: false, ask: undefined, stage: c.waits && c.stage !== 'found' ? ('running' as Stage) : c.stage } : c)).filter(
    (c) => !(nowait && c.waits === false && c.id.startsWith('apr_') && c.stage === 'found'),
  )
  const fil = STATE_FILTER.find((s) => s.id === state) ?? STATE_FILTER[0]!
  const shown = (c: MockCard) => (source === '' || c.source === source) && (!waits || c.waits)
  const waiting = all.filter((c) => c.waits)
  const stages = STAGES.filter((s) => fil.stages.includes(s.id))
  // On a phone the stage whose wait is oldest comes first, then the other waiting stages, then the rest.
  const oldestWaitIn = (s: Stage) => all.filter((c) => c.stage === s && c.waits).reduce((m, c) => Math.max(m, c.since_min), -1)
  const phoneOrder = [...stages].sort((a, b) => oldestWaitIn(b.id) - oldestWaitIn(a.id))
  const [phoneStage, setPhoneStage] = useState<Stage>(phoneOrder[0]?.id ?? 'planned')
  const sort = (a: MockCard, b: MockCard) => Number(b.waits) - Number(a.waits) || b.since_min - a.since_min
  const cardsOf = (s: Stage) => all.filter((c) => c.stage === s && shown(c)).sort(sort)
  const hiddenOf = (s: Stage) => all.filter((c) => c.stage === s && !shown(c)).length
  const olderOf = (s: Stage) => (s === 'ended' ? HIDDEN_ENDED : 0)
  const sources = (Object.keys(SOURCE_WORD) as SourceKind[]).map((k) => ({ k, n: all.filter((c) => c.source === k).length, err: missing((c) => c.source === k) }))
  // Any failed read may hold cards that wait on a person, so the lead's count is unknown while one fails.
  const waitErr = failed[0]
  const oldestWait = waiting.reduce((m, c) => Math.max(m, c.since_min), 0)
  return (
    <div className="pl" data-testid="pl-board">
      <div className="pl-lead">
        {nowait ? (
          <p className="pl-lead-l is-calm">
            <b>Nothing waits on a person</b>
            <span className="ab-dim">
              next firing: {NEXT_FIRING.name} in {dur(NEXT_FIRING.in_min)}
            </span>
          </p>
        ) : (
          <p className="pl-lead-l">
            {waitErr ? (
              <b className="pl-amber">
                <Dash why={`${READ_WORD[waitErr]} not read: ${waiting.length} known to wait`} /> wait on a person
              </b>
            ) : (
              <b className="pl-amber">{waiting.length} wait on a person</b>
            )}
            <span className="ab-dim">oldest {dur(oldestWait)}</span>
            <Button size="sm" kind={waits ? 'primary' : 'secondary'} aria-pressed={waits} onClick={() => setWaits(!waits)}>
              {waits ? 'Show every card' : 'Only these'}
            </Button>
          </p>
        )}
        <div className="pl-filters">
          <div className="pl-chips" role="group" aria-label="Source">
            <button type="button" className="pl-chip" aria-pressed={source === ''} onClick={() => setSource('')}>
              Every source
            </button>
            {sources.map((s) => (
              <button key={s.k} type="button" className="pl-chip" aria-pressed={source === s.k} onClick={() => setSource(source === s.k ? '' : s.k)}>
                {SOURCE_WORD[s.k]} {s.err ? <Dash why={`${READ_WORD[s.err]} not read`} /> : <span className="ab-dim">{s.n}</span>}
              </button>
            ))}
          </div>
          <label className="pl-select">
            <span className="c-lbl">Stage</span>
            <select className="c-inp" value={state} onChange={(e) => setState(e.target.value)}>
              {STATE_FILTER.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
          <Button size="sm" kind="ghost" onClick={() => go('automate/schedules')}>
            Sources
          </Button>
          <InfoTip about="the board" tip="Every proposal, planned run, running run, pull request and ended run from the last 14 days, in the stage it is in. A card that waits on a person is edged amber and sorts first. Nothing drags: a card moves when its run or approval moves." end />
        </div>
      </div>

      <div className="pl-phone-stages" role="tablist" aria-label="Stages">
        {phoneOrder.map((s) => {
          const n = cardsOf(s.id).length
          const w = cardsOf(s.id).filter((c) => c.waits).length
          return (
            <button key={s.id} type="button" role="tab" aria-selected={phoneStage === s.id} className={`pl-chip${w > 0 ? ' is-amber' : ''}`} onClick={() => setPhoneStage(s.id)}>
              {s.label} <b>{n}</b>
            </button>
          )
        })}
      </div>

      {stages.length === 1 ? (
        <StageTable stage={stages[0]!} cards={cardsOf(stages[0]!.id)} hidden={hiddenOf(stages[0]!.id)} older={olderOf(stages[0]!.id)} go={go} />
      ) : (
        <div className="pl-cols">
          {stages.map((s) => {
            const cards = cardsOf(s.id)
            const err = missing((c) => c.stage === s.id)
            const oldest = cards.reduce((m, c) => Math.max(m, c.since_min), 0)
            return (
              <section key={s.id} className={`pl-col${oldest > 1440 ? ' is-old' : ''}`} data-stage={s.id} data-phone-on={phoneStage === s.id ? 'true' : 'false'} aria-label={s.label}>
                <header className="pl-col-h">
                  <h3>{s.label}</h3>
                  {err ? (
                    <span className="pl-count">
                      <Dash why={`${READ_WORD[err]} not read: ${cards.length} known`} />
                    </span>
                  ) : (
                    <span className="pl-count">
                      <b>{cards.length}</b>
                      {hiddenOf(s.id) > 0 && <span className="ab-dim"> · {hiddenOf(s.id)} hidden</span>}
                    </span>
                  )}
                  {!err && cards.length > 0 && <span className="ab-dim pl-oldest">oldest {dur(oldest)}</span>}
                </header>
                {err && (
                  <div className="pl-err" role="alert">
                    <b>{READ_WORD[err]} not read</b>
                    <span className="ab-dim">503 · its cards are missing from this column</span>
                    <Button size="sm">Read again</Button>
                  </div>
                )}
                {cards.length === 0 ? (
                  err ? null : <p className="pl-empty ab-dim">{source !== '' || waits ? 'none for this filter' : 'none'}</p>
                ) : (
                  <ol className="pl-list">
                    {cards.map((c) => (
                      <li key={c.id}>
                        <Card c={c} go={go} source={source} state={state} />
                      </li>
                    ))}
                  </ol>
                )}
                <Older n={olderOf(s.id)} go={go} />
              </section>
            )
          })}
        </div>
      )}
    </div>
  )
}

function cardTo(c: MockCard, source: string, state: string): string {
  if (c.id === `run:${MOCK_RUN}`) {
    const keep = new URLSearchParams({ plans: 'margin', from: 'pipeline' })
    if (source) keep.set('source', source)
    if (state) keep.set('state', state)
    return `automate/approvals/run:${MOCK_RUN}?${keep.toString()}`
  }
  return c.id.startsWith('run:') ? `work/runs/${c.id.slice(4)}` : `automate/approvals/${c.id}`
}

function Card({ c, go, source, state }: { c: MockCard; go: (to: string) => void; source: string; state: string }) {
  return (
    <article className={`pl-card${c.waits ? ' is-waits' : ''}`} data-testid="pl-card">
      <p className="pl-card-m is-top">
        <span className="pl-src">{c.source === 'hand' && c.by ? `by hand · ${c.by}` : SOURCE_WORD[c.source]}</span>
        <span className="ab-dim">{dur(c.since_min)}</span>
      </p>
      <h4 className="pl-title">
        <InApp go={go} to={cardTo(c, source, state)}>
          {c.title}
        </InApp>
      </h4>
      <p className="pl-card-m">
        {c.issue !== null ? <span className="mono">{c.issue.replace(/^example-org\//, '')}</span> : <span className="ab-dim">no issue yet</span>}
        {c.revision != null && <span className="ab-dim">r{c.revision}</span>}
        {c.pr != null && <span className="mono">#{c.pr}</span>}
        {c.checks && <span className="ab-dim">{c.checks}</span>}
      </p>
      {c.last_word && (
        <p className="pl-word">
          <b>{c.last_word.by}</b> {c.last_word.text}
        </p>
      )}
      {c.waits && (
        <p className="pl-ask">
          <b>{c.ask}</b>
          {c.expires_min != null && <span>expires in {dur(c.expires_min)}</span>}
        </p>
      )}
      {(c.stage === 'running' || c.stage === 'pr_ci' || c.stage === 'ended') && (
        <p className="pl-card-m">
          <span className="ab-dim">spent</span>
          {c.cost != null ? <span>${c.cost.toFixed(2)}</span> : <Dash why={c.cost_why ?? 'not served for this card'} />}
        </p>
      )}
    </article>
  )
}

/** Ended cards past the board's bound (`by_stage.ended.hidden`, §3.2): not a filter, so a button to Runs, not "hidden". */
function Older({ n, go }: { n: number; go: (to: string) => void }) {
  if (n === 0) return null
  return (
    <Button size="sm" kind="ghost" onClick={() => go('work/runs')}>
      {n} older in Runs
    </Button>
  )
}

function StageTable({ stage, cards, hidden, older, go }: { stage: { id: Stage; label: string }; cards: MockCard[]; hidden: number; older: number; go: (to: string) => void }) {
  return (
    <section className="pl-table" aria-label={stage.label}>
      <header className="pl-col-h">
        <h3>{stage.label}</h3>
        <span className="pl-count">
          <b>{cards.length}</b>
          {hidden > 0 && <span className="ab-dim"> · {hidden} hidden</span>}
        </span>
      </header>
      <table className="c-table">
        <thead>
          <tr>
            <th>Plan</th>
            <th>Source</th>
            <th>Waiting</th>
            <th>Last word</th>
            <th>Revision</th>
          </tr>
        </thead>
        <tbody>
          {cards.map((c) => (
            <tr key={c.id} className={c.waits ? 'is-waits' : ''}>
              <td>
                <InApp go={go} to={cardTo(c, '', stage.id)}>
                  {c.title}
                </InApp>
                <div className="mono ab-dim">{c.issue?.replace(/^example-org\//, '') ?? '—'}</div>
              </td>
              <td>{c.source === 'hand' && c.by ? `by hand · ${c.by}` : SOURCE_WORD[c.source]}</td>
              <td>
                {c.waits ? (
                  <span className="pl-ask is-inline">
                    <b>{c.ask}</b> <span>expires in {dur(c.expires_min ?? 0)}</span>
                  </span>
                ) : (
                  <span className="ab-dim">{dur(c.since_min)}</span>
                )}
              </td>
              <td className="pl-wordcell">{c.last_word ? <><b>{c.last_word.by}</b> {c.last_word.text}</> : <Dash why="no thread yet" />}</td>
              <td>{c.revision != null ? `r${c.revision}` : <Dash why="no plan yet" />}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Older n={older} go={go} />
    </section>
  )
}

/** A deployment without GET /v1/pipeline (404): the section's state, the way to Approvals kept. */
function NotServed({ go }: { go: (to: string) => void }) {
  return (
    <section className="pl-404" role="status" data-testid="pl-not-served">
      <b>This deployment does not serve the board</b>
      <span className="ab-dim">
        GET /v1/pipeline · 404
        <InfoTip about="the missing board" tip="The board needs a newer swarm-api. Plans waiting on a person are still decided from Approvals." />
      </span>
      <Button kind="primary" size="sm" onClick={() => go('automate/approvals')}>
        Open Approvals
      </Button>
    </section>
  )
}
