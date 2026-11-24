/**
 * AUTOMATE › PIPELINE (docs/design/plans-pipeline.md §2.1, lane U1): the board
 * of what automatic runs propose, at `/automate/pipeline`, read from
 * `GET /v1/pipeline` (§3.2). Five stages -- found, planned or waiting,
 * approved and running, pull request and CI, merged or ended -- each card
 * placed by the server, never re-derived here.
 *
 * WHAT WAITS ON A PERSON IS AMBER. `waits_on_person` is the inbox's own
 * pending projection (a pending plan, hold, merge or proposal); such a card is
 * edged amber in whatever column it sits, sorts first, oldest first, and
 * names its time to expiry. A card that waits on nobody shows no state word:
 * a status shows its word only where it asks for something (the owner's rule).
 *
 * A CARD OPENS ITS DECISION PAGE AT THE APPROVAL'S OWN ADDRESS,
 * `/automate/approvals/<id>` (the owner 10:08Z), carrying `from=pipeline` and
 * the board's filters, so that page's Back is a link to the board as it was.
 * A card with no approval (a run in PLANNING, a run that ended) opens the run
 * in Work › Runs.
 *
 * NOTHING ON THE BOARD DRAGS: a card moves only when its run or approval
 * moves. The board is a read; it writes nothing and holds no capacity
 * (invariant 1).
 *
 * STATES, KEPT APART: loading is the Screen's skeleton; a measured empty board
 * is one line and its action; a failed read is the Screen's failure, a failed
 * refresh its stale state; a read named in `partial` fails only the columns
 * it feeds (C:P6c); a 404 is "not served", with Approvals one press away
 * (C:P6d).
 *
 * AT 390 PIXELS one stage at a time (C:P7a): a row of stage buttons with
 * counts, the stage holding a waiting card chosen first.
 */
import { useState } from 'react'
import { loadPipeline, PIPELINE_STAGES, type PipelineBoard, type PipelineCard, type PipelineColumn, type PipelineStage } from './api'
import { APPROVAL_KIND, APPROVALS_ADDRESS, approvalAddress, boardQuery, pipelineAddress } from './approvalPaths'
import { Button, Dash, Tag } from './components'
import type { Result } from './fetch'
import { InApp } from './InApp'
import { InfoTip } from './InfoTip'
import { runAddress } from './IssueSubmit'
import { untilWords } from './Schedules'
import { Screen, timeAgo } from './Shell'
import { useNow } from './useNow'
import './styles/pipeline.css'

/** Each stage as the board names it (§2.1). */
export const STAGE_WORD: Readonly<Record<PipelineStage, string>> = {
  found: 'Found',
  planned: 'Planned or waiting',
  running: 'Approved and running',
  pr_ci: 'Pull request and CI',
  ended: 'Merged or ended',
}

/** A card's source in words (§3.1): a run's kind, or a proposal's schedule type. */
export const SOURCE_WORD: Readonly<Record<string, string>> = {
  sweep: 'Issue sweep',
  failure_review: 'Failure review',
  plan_only: 'Plan only',
  hand: 'By hand',
  observer: 'Observer',
  'ci-flake-hunter': 'Flake hunter',
  proposal: 'Proposal',
}

/** `?state=`, the route's own filter (§3.2), in the chips' words. */
const STATE_WORD: Readonly<Record<string, string>> = {
  waiting: 'Waiting',
  running: 'Running',
  decided: 'Decided',
  ended: 'Ended',
}

/** Which columns a state filter leaves (`pipeline.STATE_STAGES`). */
const STATE_STAGES: Readonly<Record<string, readonly PipelineStage[]>> = {
  waiting: ['found', 'planned'],
  running: ['running', 'pr_ci'],
  decided: ['ended'],
  ended: ['ended'],
}

/** Which columns a failed read feeds (`pipeline.py`): runs feed every stage; approvals the proposals' and merges'. */
const READ_FEEDS: Readonly<Record<string, readonly PipelineStage[]>> = {
  runs: PIPELINE_STAGES,
  approvals: ['found', 'pr_ci', 'ended'],
}

const DAY_MS = 86_400_000

export interface BoardFilters {
  source: string | null
  state: string | null
  waits: boolean
}

/** The board's filters off its address. A value the route does not know is dropped by the route's 422, so it is kept as given. */
export function filtersOf(view: string | null): BoardFilters {
  const q = new URLSearchParams(view ?? '')
  const pick = (k: string) => {
    const v = q.get(k)
    return v === null || v === '' ? null : v
  }
  return { source: pick('source'), state: pick('state'), waits: q.get('waits') === 'person' }
}

function queryOf(f: BoardFilters): string {
  const q = new URLSearchParams()
  if (f.source !== null) q.set('source', f.source)
  if (f.state !== null) q.set('state', f.state)
  if (f.waits) q.set('waits', 'person')
  return q.toString()
}

type BoardRead = { kind: 'board'; board: PipelineBoard } | { kind: 'empty' } | { kind: 'not_served' }

/**
 * The board's read. A measured empty board and a 404 are answers the page
 * draws in its own line; every other failure is the Screen's, kept apart.
 */
async function readBoard(f: BoardFilters): Promise<Result<BoardRead>> {
  const r = await loadPipeline({ source: f.source, state: f.state, waits: f.waits ? 'person' : null })
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: { kind: 'board', board: r.data } }
  if (r.status === 'empty') return { status: 'ok', data: { kind: 'empty' }, fetchedAt: r.fetchedAt }
  if (r.status === 'error' && r.error.httpStatus === 404) return { status: 'ok', data: { kind: 'not_served' }, fetchedAt: Date.now() }
  return r
}

const at = (iso: string | null | undefined) => (iso ? Date.parse(iso) || 0 : 0)

/** Waiting cards first, oldest first; then the rest, newest first: the server's order, held here too. */
export function ordered(cards: readonly PipelineCard[]): PipelineCard[] {
  const waiting = cards.filter((c) => c.waits_on_person).sort((a, b) => at(a.since) - at(b.since))
  const rest = cards.filter((c) => !c.waits_on_person).sort((a, b) => at(b.since) - at(a.since))
  return [...waiting, ...rest]
}

/** Where a card goes: its approval's own page, the board kept, else its run. */
function cardAddress(card: PipelineCard, board: string): string | null {
  const approval = card.subject.approval_id
  if (typeof approval === 'string' && approval !== '') return approvalAddress(approval, null, board)
  const run = card.subject.run_id
  if (typeof run === 'string' && run !== '') return runAddress(run)
  return null
}

function Card({ card, board, go, now }: { card: PipelineCard; board: string; go: (to: string) => void; now: number }) {
  const to = cardAddress(card, board)
  const source = SOURCE_WORD[card.source.kind] ?? card.source.kind
  const waits = card.waits_on_person
  const kind = card.approval_kind === null ? null : (APPROVAL_KIND[card.approval_kind] ?? card.approval_kind)
  // The kind is drawn only where it adds to the source's word ("Proposal" beside "Proposal" says nothing twice).
  const showKind = waits && kind !== null && !source.toLowerCase().includes(kind.toLowerCase())
  const body = (
    <>
      <span className="pl-card-h">
        <span className="pl-src">{source}</span>
        {showKind && <Tag>{kind}</Tag>}
      </span>
      <span className="pl-title">{card.subject.title || card.subject.issue || card.card_id}</span>
      <span className="pl-meta">
        {card.subject.issue !== undefined && card.subject.title !== '' && <span className="pl-mono">{card.subject.issue}</span>}
        {waits && card.expires_at !== null ? (
          <span className="pl-due" title={card.expires_at}>
            expires {untilWords(card.expires_at, now)}
          </span>
        ) : card.since !== null ? (
          <span title={card.since}>{timeAgo(card.since, now)}</span>
        ) : null}
      </span>
    </>
  )
  return (
    <li className={`pl-card${waits ? ' is-waits' : ''}`} data-card={card.card_id} data-waits={waits ? 'person' : undefined}>
      {to === null ? (
        <span className="pl-card-a">{body}</span>
      ) : (
        <InApp go={go} to={to} className="pl-card-a">
          {body}
        </InApp>
      )}
    </li>
  )
}

function Column({
  stage,
  column,
  cards,
  failed,
  shown,
  one,
  board,
  go,
  now,
}: {
  stage: PipelineStage
  column: PipelineColumn | undefined
  cards: PipelineCard[]
  failed: string[]
  shown: boolean
  one: boolean
  board: string
  go: (to: string) => void
  now: number
}) {
  const count = column?.count ?? cards.length
  const hidden = column?.hidden ?? 0
  const oldest = column?.oldest_at ?? null
  const old = stage !== 'ended' && oldest !== null && now - Date.parse(oldest) > DAY_MS
  const cls = ['pl-col', old ? 'is-old' : '', shown ? 'is-shown' : '', one ? 'is-one' : ''].filter(Boolean).join(' ')
  return (
    <section className={cls} data-stage={stage} aria-label={STAGE_WORD[stage]}>
      <header className="pl-col-h">
        <h2>{STAGE_WORD[stage]}</h2>
        <span className="pl-n" data-testid={`pl-count-${stage}`}>
          {count}
        </span>
        {hidden > 0 && (
          <span className="pl-dim" data-testid={`pl-hidden-${stage}`}>
            +{hidden} hidden
          </span>
        )}
        {oldest !== null && cards.length > 0 && (
          <span className="pl-dim" title={oldest}>
            oldest {timeAgo(oldest, now)}
          </span>
        )}
      </header>
      {failed.length > 0 ? (
        <p className="pl-col-err" role="alert" data-testid={`pl-error-${stage}`}>
          Not read
          <InfoTip about="Not read" tip={`The ${failed.join(' and ')} read failed; the other columns are as read.`} />
        </p>
      ) : cards.length === 0 ? (
        <p className="pl-col-none">
          <Dash why={hidden > 0 ? 'every card here is filtered out' : 'nothing in this stage'} />
        </p>
      ) : (
        <ol className="pl-cards">
          {cards.map((c) => (
            <Card key={c.card_id} card={c} board={board} go={go} now={now} />
          ))}
        </ol>
      )}
    </section>
  )
}

/** A row of chips: one choice of several, its count where the read measured it. */
function Chips({ label, options, chosen, pick }: { label: string; options: [string | null, string, number | null][]; chosen: string | null; pick: (v: string | null) => void }) {
  return (
    <div className="pl-chips" role="group" aria-label={label}>
      {options.map(([v, word, n]) => (
        <Button key={v ?? 'all'} size="sm" kind={v === chosen ? 'primary' : 'secondary'} aria-pressed={v === chosen} onClick={() => pick(v)}>
          {word}
          {n !== null && <span className="pl-chip-n"> · {n}</span>}
        </Button>
      ))}
    </div>
  )
}

function Board({ board, filters, go }: { board: PipelineBoard; filters: BoardFilters; go: (to: string) => void }) {
  const now = useNow()
  const query = queryOf(filters)
  const to = (f: BoardFilters) => go(pipelineAddress(queryOf(f)))
  const cards = ordered(board.cards)
  const byStage = (s: PipelineStage) => cards.filter((c) => c.stage === s)
  const failedFor = (s: PipelineStage) => board.partial.filter((p) => (READ_FEEDS[p.read] ?? []).includes(s)).map((p) => p.read)
  const count = (s: PipelineStage) => board.by_stage[s]?.count ?? byStage(s).length
  // A state filter leaves its columns; one that leaves one column turns the
  // board into that column, full width (C:P1c).
  const stages = (filters.state !== null ? STATE_STAGES[filters.state] : undefined) ?? PIPELINE_STAGES
  const one = stages.length === 1
  // On a phone, one stage at a time: the one holding a waiting card first.
  const first = stages.find((s) => byStage(s).some((c) => c.waits_on_person)) ?? stages.find((s) => count(s) > 0) ?? stages[0]!
  const [picked, setPicked] = useState<PipelineStage | null>(null)
  const shown = picked !== null && stages.includes(picked) ? picked : first
  const sources = Array.from(new Set([...board.cards.map((c) => c.source.kind), ...(filters.source === null ? [] : [filters.source])]))
  return (
    <div className="pl-page" data-testid="pl-board">
      <div className="pl-bar">
        {board.waits_on_person > 0 && (
          <Button
            size="sm"
            kind={filters.waits ? 'primary' : 'secondary'}
            className="pl-waits"
            aria-pressed={filters.waits}
            data-testid="pl-waits"
            onClick={() => to({ ...filters, waits: !filters.waits })}
          >
            {board.waits_on_person} waiting on a person
          </Button>
        )}
        {sources.length > 1 || filters.source !== null ? (
          <Chips
            label="Source"
            options={[[null, 'Every source', null], ...sources.map((k): [string, string, null] => [k, SOURCE_WORD[k] ?? k, null])]}
            chosen={filters.source}
            pick={(v) => to({ ...filters, source: v })}
          />
        ) : null}
        <Chips
          label="State"
          options={[[null, 'Every state', null], ...Object.entries(STATE_WORD).map(([k, w]): [string, string, null] => [k, w, null])]}
          chosen={filters.state}
          pick={(v) => to({ ...filters, state: v })}
        />
      </div>
      {stages.length > 1 && (
        <div className="pl-stages" role="group" aria-label="Stage">
          {stages.map((s) => (
            <Button key={s} size="sm" kind={s === shown ? 'primary' : 'secondary'} aria-pressed={s === shown} onClick={() => setPicked(s)}>
              {STAGE_WORD[s]}
              <span className="pl-chip-n"> · {count(s)}</span>
            </Button>
          ))}
        </div>
      )}
      <div className={`pl-cols${one ? ' is-one' : ''}`}>
        {stages.map((s) => (
          <Column key={s} stage={s} column={board.by_stage[s]} cards={byStage(s)} failed={failedFor(s)} shown={s === shown} one={one} board={query} go={go} now={now} />
        ))}
      </div>
    </div>
  )
}

export function PipelineScreen({ view, go }: { view: string | null; go: (to: string) => void }) {
  const filters = filtersOf(view)
  const query = boardQuery(view)
  return (
    <Screen key={`pipeline:${query}`} title="Pipeline" load={() => readBoard(filters)}>
      {(d) =>
        d.kind === 'board' ? (
          <Board board={d.board} filters={filters} go={go} />
        ) : d.kind === 'empty' ? (
          <p className="pl-none" data-testid="pl-empty">
            <span>Nothing on the board</span>
            <Button size="sm" onClick={() => go('automate/schedules')}>
              Schedules
            </Button>
          </p>
        ) : (
          <p className="pl-none" data-testid="pl-not-served">
            <span>
              Not served here
              <InfoTip about="Not served here" tip="This deployment's API has no GET /v1/pipeline yet." />
            </span>
            <Button size="sm" onClick={() => go(APPROVALS_ADDRESS)}>
              Approvals
            </Button>
          </p>
        )
      }
    </Screen>
  )
}
