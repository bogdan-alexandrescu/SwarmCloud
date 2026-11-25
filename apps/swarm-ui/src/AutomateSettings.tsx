/**
 * AUTOMATE › SETTINGS (docs/design/plans-pipeline.md §2.5, §6.4, §7.8; lane
 * U11; the owner's PD4 pick, B, frames `DB:D5a` and `DB:D5b`), at
 * `/automate/settings`, after Approvals.
 *
 * Two parts, each saved through its own level's one route (§6.4: each level
 * is edited one way):
 *
 *   * the Plan review card (`PlanSettingsCard`): the workspace's level,
 *     `PUT /v1/workspaces/{tenant_id}/plan-settings`, its admin's;
 *   * under it, every schedule whose type `proposes` (§2.4), each with
 *     follow the workspace, on or off, saved through the schedule's own
 *     `PATCH` with its revision and `plan_diagrams` alone: the same field the
 *     schedule's edit sheet sets (lane U12), so the two controls are one value
 *     and one audit trail. A type that does not propose has no plan to draw
 *     in, and no row; a catalogue too old to say `proposes` lists none.
 *
 * WHO MAY CHANGE A ROW is whoever may edit the schedule (§8): the server
 * decides on the PATCH and its refusal is drawn with its own reason, as the
 * edit sheet draws it. An issue run submitted by hand has no schedule and
 * follows the workspace; the list's (i) says so, not a sentence on the page.
 */

import { useRef, useState } from 'react'

import {
  loadPlanSettings,
  loadSchedules,
  loadScheduleTypes,
  updateSchedule,
  type PlanSettings,
  type Schedule,
  type ScheduleEditBody,
  type ScheduleTypeRow,
} from './api'
import { Card, Segmented } from './components'
import type { ApiError, Result } from './fetch'
import { Refusal } from './GitHubConnect'
import { InfoTip } from './InfoTip'
import { DIAGRAMS_LABEL, PlanSettingsCard, inheritedLabel } from './PlanSettingsCard'
import { Screen } from './Shell'
import type { PlanDiagrams, SchedulePlanDiagrams } from './types'
import './styles/automate-settings.css'

/** A schedule as this page reads it: with its diagrams choice (absent means `inherit`). */
type Stored = Schedule & SchedulePlanDiagrams
/** `proposes` (§2.4) is absent from an older API's catalogue. */
type TypeRow = ScheduleTypeRow & { proposes?: boolean }

interface Data {
  settings: PlanSettings
  /** The schedules whose type proposes, by name. */
  planning: Stored[]
}

/** A read that carried no data, as the page's failed read. */
function unread<T>(r: Exclude<Result<T>, { status: 'ok' } | { status: 'stale' }>): Result<Data> {
  if (r.status === 'error' || r.status === 'loading') return r
  return { status: 'error', error: { kind: 'server_error', httpStatus: null, code: null, message: 'The read answered nothing.' } }
}

/** The schedules, the catalogue and the workspace's plan settings, in one read for the page. */
async function readSettings(): Promise<Result<Data>> {
  const [schedules, types] = await Promise.all([loadSchedules(), loadScheduleTypes()])
  if (schedules.status !== 'ok' && schedules.status !== 'stale') return unread(schedules)
  if (types.status !== 'ok' && types.status !== 'stale') return unread(types)
  const settings = await loadPlanSettings(schedules.data.tenant_id)
  if (settings.status !== 'ok' && settings.status !== 'stale') return unread(settings)
  const proposing = new Set((types.data.types as TypeRow[]).filter((t) => t.proposes === true).map((t) => t.name))
  const planning = (schedules.data.schedules as Stored[])
    .filter((s) => proposing.has(s.type))
    .sort((a, b) => a.name.localeCompare(b.name))
  return { status: 'ok', fetchedAt: settings.fetchedAt, data: { settings: settings.data, planning } }
}

export function AutomateSettingsScreen() {
  return (
    <Screen title="Settings" load={readSettings}>
      {(d) => <SettingsBody data={d} />}
    </Screen>
  )
}

function SettingsBody({ data }: { readonly data: Data }) {
  // The card's save answers the view the rows' "workspace’s" then names.
  const [settings, setSettings] = useState(data.settings)
  return (
    <div className="as-page">
      <PlanSettingsCard settings={settings} onSaved={setSettings} />
      <Card
        className="as-card"
        title={
          <>
            Schedules
            <InfoTip about="Schedules" tip="Each schedule whose type plans. An issue run submitted by hand has no schedule and follows the workspace." />
          </>
        }
      >
        {data.planning.length === 0 ? (
          <p className="as-none">None</p>
        ) : (
          <ul className="as-rows">
            {data.planning.map((s) => (
              <ScheduleRow key={s.schedule_id} schedule={s} workspace={settings.effective.diagrams.value} />
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}

const ROW_OPTIONS: readonly PlanDiagrams[] = ['inherit', 'on', 'off']

function ScheduleRow({ schedule, workspace }: { readonly schedule: Stored; readonly workspace: 'on' | 'off' }) {
  const inFlight = useRef(false)
  const [stored, setStored] = useState<{ value: PlanDiagrams; revision: number }>({
    value: schedule.plan_diagrams ?? 'inherit',
    revision: schedule.revision,
  })
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)

  async function choose(value: PlanDiagrams) {
    if (value === stored.value || inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setRefused(null)
    // The revision it read and the one field: nothing else of the schedule moves.
    const body: ScheduleEditBody & SchedulePlanDiagrams = { revision: stored.revision, plan_diagrams: value }
    const r = await updateSchedule(schedule.schedule_id, body)
    inFlight.current = false
    setBusy(false)
    if (r.status === 'error') {
      setRefused(r.error)
      return
    }
    if (r.status === 'ok' || r.status === 'stale') {
      const next = r.data.schedule as Stored
      setStored({ value: next.plan_diagrams ?? value, revision: next.revision })
    }
  }

  return (
    <li className="as-row">
      <span className="as-name">{schedule.name}</span>
      <span className="as-type mono">{schedule.type}</span>
      <Segmented
        className="as-seg"
        label={`${DIAGRAMS_LABEL}: ${schedule.name}`}
        value={stored.value}
        disabled={busy}
        options={ROW_OPTIONS.map((k) => ({ key: k, label: k === 'inherit' ? inheritedLabel('workspace’s', workspace) : k }))}
        onChange={(k) => void choose(k)}
      />
      {refused !== null && (
        <div className="as-refusal">
          <Refusal error={refused} title="Not saved" />
        </div>
      )}
    </li>
  )
}
