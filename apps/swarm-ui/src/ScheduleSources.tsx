/**
 * AUTOMATE › SCHEDULES, THE SOURCES THAT PROPOSE (docs/design/plans-pipeline.md
 * §2.4, lane U6 PLAN-SOURCES; the owner's pick on the tracking issue: "a list
 * of sources that propose, drawing only the gates that exist").
 *
 * `automate/schedules?proposes=1`: the tenant's schedules whose TYPE the
 * catalogue marks `proposes` (`GET /v1/schedule-types`), so the narrowing is
 * the server's and never a list of names kept here. Each card draws:
 *
 *   * the gate AS STORED: a field (`run`, `plan`, `merge`, `approvers`,
 *     `approval_ttl_hours`) is drawn only when the schedule's read carries it;
 *     no type default is filled in, and an absent field is not a dash;
 *   * today's spend against the tripwire (`SpendBar`, as the full list);
 *   * `proposals_7d` (§3.10): the served count, "at least" when the server's
 *     bounded read was full, and a dash with its reason when the read carries
 *     none -- never 0;
 *   * Manage, which opens the schedule's sheet, where the merge switch is.
 *
 * A catalogue without `proposes` (an API before plans-pipeline P5) cannot
 * narrow, and says so rather than listing every schedule as a source.
 */
import { loadScheduleTypes, loadSchedules, type Schedule, type ScheduleTypeRow } from './api'
import { Info } from './AccountsVerdict'
import { Button, EmptyState, Segmented, Tag, buttonClass } from './components'
import type { ApiError, Result } from './fetch'
import { InApp } from './InApp'
import { addressToPath } from './paths'
import { useRoleGate } from './roleGate'
import { NEW_SCHEDULE, SCHEDULES_ADDRESS, scheduleAddress } from './schedulePaths'
import { DryRunChip, SpendBar } from './Schedules'
import { Screen } from './Shell'
import { pluralise } from './types'

/** The narrowed list's address. */
export const SOURCES_ADDRESS = `${SCHEDULES_ADDRESS}?proposes=1`

/** Whether a Schedules address asks for the sources (`proposes=1`). */
export function proposesOf(view: string | null): boolean {
  return view !== null && new URLSearchParams(view).get('proposes') === '1'
}

/** `GET /v1/schedules`' `proposals_7d` (§3.10). */
interface Proposals7d {
  proposed: number
  approved: number
  auto_approved: number
  rejected: number
  expired: number
  running: number
  merged: number
  window_days: number
  complete: boolean
}

type Source = Schedule & { proposals_7d?: Proposals7d | null }
type TypeRow = ScheduleTypeRow & { proposes?: boolean }

interface Sources {
  /** The listed schedules whose type proposes; empty when `narrowed` is false. */
  rows: Source[]
  tenant_id: string
  /** False when the catalogue serves no `proposes`: nothing can be narrowed. */
  narrowed: boolean
}

/** The gate's fields in the order the schedule's Gate tab draws them, with their labels. */
const GATE_FIELDS: readonly { key: keyof Schedule['gate']; label: string }[] = [
  { key: 'run', label: 'Run' },
  { key: 'plan', label: 'Plan' },
  { key: 'merge', label: 'Merge' },
  { key: 'approvers', label: 'Approvers' },
  { key: 'approval_ttl_hours', label: 'Expires' },
]

/** The parts of the seven days' count, drawn when not zero. */
const PARTS: readonly { key: keyof Proposals7d; label: string }[] = [
  { key: 'approved', label: 'approved' },
  { key: 'auto_approved', label: 'auto-approved' },
  { key: 'rejected', label: 'rejected' },
  { key: 'expired', label: 'expired' },
  { key: 'running', label: 'running' },
  { key: 'merged', label: 'merged' },
]

/** "Mine | Proposing": the full list and the sources, as links (buttons, not prose). */
export function SourcesSwitch({ on, go }: Readonly<{ on: boolean; go: (to: string) => void }>) {
  return (
    <Segmented<'all' | 'proposes'>
      label="Which schedules"
      small
      value={on ? 'proposes' : 'all'}
      options={[
        { key: 'all', label: 'All', href: addressToPath(SCHEDULES_ADDRESS) },
        { key: 'proposes', label: 'Proposing', href: addressToPath(SOURCES_ADDRESS), title: 'Schedules whose firings propose work for approval' },
      ]}
      onChange={(k) => go(k === 'proposes' ? SOURCES_ADDRESS : SCHEDULES_ADDRESS)}
    />
  )
}

function gateValue(key: keyof Schedule['gate'], value: string | number): string {
  return key === 'approval_ttl_hours' ? `${value} h` : String(value)
}

function Gate({ gate }: Readonly<{ gate: Schedule['gate'] }>) {
  const served = GATE_FIELDS.filter((f) => gate[f.key] !== undefined && gate[f.key] !== null)
  if (served.length === 0) return null
  return (
    <dl className="src-gate" aria-label="Gate">
      {served.map((f) => (
        <div key={f.key} data-testid={`src-gate-${f.key}`}>
          <dt className="acd-k">{f.label}</dt>
          <dd className="acd-mono">{gateValue(f.key, gate[f.key]!)}</dd>
        </div>
      ))}
    </dl>
  )
}

function SevenDays({ counts }: Readonly<{ counts: Proposals7d | null | undefined }>) {
  if (counts === undefined || counts === null) {
    return (
      <div className="acd-next" data-testid="src-7d">
        <span className="acd-k">Proposed · 7 days</span>
        <span className="src-7d-v">
          <span className="acd-big is-none" data-testid="src-7d-n">&mdash;</span>
          <Info about="Not counted" tip="This API's schedule list carries no seven-day count." />
        </span>
      </div>
    )
  }
  const parts = PARTS.filter((p) => (counts[p.key] as number) > 0)
  return (
    <div className="acd-next" data-testid="src-7d">
      <span className="acd-k">Proposed · {counts.window_days} days</span>
      <span className="src-7d-v">
        <span className="acd-big" data-testid="src-7d-n">{counts.complete ? String(counts.proposed) : `at least ${counts.proposed}`}</span>
        {!counts.complete && <Info about="At least" tip="The server's bounded read was full, so some proposals may be missing." />}
      </span>
      {parts.length > 0 && <span className="acd-line acd-dim" data-testid="src-7d-parts">{parts.map((p) => `${counts[p.key] as number} ${p.label}`).join(' · ')}</span>}
    </div>
  )
}

function SourceCard({ s, go }: Readonly<{ s: Source; go: (to: string) => void }>) {
  return (
    <section className="acd-card" aria-label={s.name} data-source={s.schedule_id} data-testid="src-card">
      <div className="acd-card-h">
        <Tag title={s.tier ?? undefined}>{s.type}</Tag>
        <h3>{s.name}</h3>
        <DryRunChip s={s} />
      </div>
      <Gate gate={s.gate ?? {}} />
      <SevenDays counts={s.proposals_7d} />
      <SpendBar spend={s.spend_today} perDay={s.budget?.per_day_usd} />
      <div className="acd-card-f">
        <InApp go={go} to={scheduleAddress(s.schedule_id)} className={`acd-manage ${buttonClass({ kind: 'secondary', size: 'sm' })}`}>
          Manage
        </InApp>
      </div>
    </section>
  )
}

function SourcesBody({ d, go }: Readonly<{ d: Sources; go: (to: string) => void }>) {
  const roleGate = useRoleGate('member', 'create schedules')
  if (!d.narrowed) {
    return (
      <div className="acd-page" data-testid="src-not-served">
        <EmptyState kind="failed" heading="Proposing types are not served here">
          <span className="acd-mono">GET /v1/schedule-types</span>{' '}
          <Info about="Not served" tip="This API's schedule types do not say which ones propose, so the list cannot be narrowed." />
        </EmptyState>
      </div>
    )
  }
  if (d.rows.length === 0) {
    const action =
      roleGate === null ? (
        <InApp go={go} to={scheduleAddress(NEW_SCHEDULE)} className={buttonClass({ kind: 'primary', size: 'sm' })}>
          New schedule
        </InApp>
      ) : (
        <Button kind="primary" size="sm" disabledReason={roleGate}>
          New schedule
        </Button>
      )
    return (
      <div className="acd-page">
        <div data-testid="src-none">
          <EmptyState kind="empty" heading="No schedule proposes work" action={action}>
            Only schedules of a type that proposes work for approval are listed.
          </EmptyState>
        </div>
      </div>
    )
  }
  return (
    <div className="acd-page">
      <div className="acd-grid">
        {d.rows.map((s) => (
          <SourceCard key={s.schedule_id} s={s} go={go} />
        ))}
      </div>
    </div>
  )
}

/** A failure of either read, as `Result` carries it: stale keeps its rows, error has none. */
function failureOf(r: Result<unknown>): ApiError | null {
  return r.status === 'error' || r.status === 'stale' ? r.error : null
}

/**
 * The list and the catalogue in one read, so the Screen's ⟳ refreshes both.
 * Either failing with nothing to show is the page's failure; either answering
 * from older data makes the page stale, with that read's error.
 */
async function loadSources(): Promise<Result<Sources>> {
  const [list, types] = await Promise.all([loadSchedules(), loadScheduleTypes()])
  if (list.status === 'loading' || list.status === 'error') return list
  if (types.status === 'loading' || types.status === 'error') return types
  const schedules = (list.status === 'empty' ? [] : list.data.schedules) as Source[]
  const catalogue = (types.status === 'empty' ? [] : types.data.types) as TypeRow[]
  const narrowed = catalogue.some((t) => typeof t.proposes === 'boolean')
  const proposing = new Set(catalogue.filter((t) => t.proposes === true).map((t) => t.name))
  const data: Sources = {
    rows: narrowed ? schedules.filter((s) => proposing.has(s.type)) : [],
    tenant_id: list.status === 'empty' ? '' : list.data.tenant_id,
    narrowed,
  }
  const fetchedAt = Math.min(list.fetchedAt, types.fetchedAt)
  const stale = failureOf(list) ?? failureOf(types)
  return stale === null ? { status: 'ok', fetchedAt, data } : { status: 'stale', fetchedAt, data, error: stale }
}

/** The sources' screen: the Schedules page narrowed, with the switch back to the full list. */
export function ScheduleSources({ go }: Readonly<{ go: (to: string) => void }>) {
  return (
    <>
      <div className="acd-bar-row">
        <SourcesSwitch on go={go} />
      </div>
      <Screen
        title="Schedules"
        load={loadSources}
        summary={(d) => (d.narrowed ? `${pluralise(d.rows.length, 'schedule')} proposing` : 'proposing: — not served by this API')}
      >
        {(d) => <SourcesBody d={d} go={go} />}
      </Screen>
    </>
  )
}
