/**
 * THE PLAN REVIEW CARD (docs/design/plans-pipeline.md §2.5, §6.4, §7.8; lane U11).
 *
 * The workspace's level of the three plan-review settings, as one
 * `GET/PUT /v1/workspaces/{tenant_id}/plan-settings`:
 *
 *   * diagrams in plans: on, off, or the platform's, the last naming what the
 *     platform's resolves to now ("platform’s (on)");
 *   * the plan chat's model class: one of the classes `claude-code` carries,
 *     or the platform's, named the same way;
 *   * the caps: one read-only line. They are the platform's alone (§7.8: a
 *     platform admin on Admin › Settings); the route refuses a `caps` key, so
 *     nothing here draws an input for them, for anyone.
 *
 * WHO. The server says, in `can_edit` (`plansettings.can_edit`: the
 * workspace's admin, a personal workspace's owner, a platform admin). Anyone
 * else reads the same choices with every segment disabled; the console never
 * widens what the server decided (§8).
 *
 * A PRESS SAVES. A choice is a small fixed set, so pressing a segment sends
 * the one key it changes; the answer is the view the page then draws. A
 * refusal is drawn with the server's own reason (`Refusal`), and the choice
 * stays where the server left it.
 *
 * NO PROSE ON THE PAGE (the owner's rule, 2026-11-05): the owner's examples of
 * where the planner draws and where it does not (§6.3) sit behind the
 * switch's (i), never as sentences on the card.
 */

import { useRef, useState } from 'react'

import { savePlanSettings, type PlanSettings, type PlanSettingsBody } from './api'
import { Card, Segmented } from './components'
import type { ApiError } from './fetch'
import { Refusal } from './GitHubConnect'
import { InfoTip } from './InfoTip'
import './styles/automate-settings.css'

/** The segment that follows the level above, as the PUT spells it. */
const INHERIT = 'inherit'

/** The groups' accessible names: the words beside each control. */
export const DIAGRAMS_LABEL = 'Diagrams in plans'
export const MODEL_LABEL = 'Chat model'

/** The owner's examples (§6.3), behind the switch's (i), here and on Admin › Settings' Plan review. */
export const DIAGRAM_EXAMPLES =
  'Drawn: a plan of two or more steps, or one that crosses components (a route in apps/swarm-api and its console page). ' +
  'Not drawn: a one-line value change in a tfvars file, a wording fix, a rename in one file.'

/** A setting's choice that follows the platform, naming what that is now; bare when the read does not say. */
export function inheritedLabel(level: string, value: string | null): string {
  return value === null ? level : `${level} (${value})`
}

/** "$25" for a whole figure, "$25.50" otherwise. */
function dollars(n: number): string {
  return Number.isInteger(n) ? `$${n}` : `$${n.toFixed(2)}`
}

export function PlanSettingsCard({ settings, onSaved }: { readonly settings: PlanSettings; readonly onSaved: (next: PlanSettings) => void }) {
  const inFlight = useRef(false)
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const editable = settings.can_edit

  // What the platform's resolves to is known only while the read resolved to it
  // (the workspace follows it, or its class fell through, §7.8).
  const platformDiagrams = settings.effective.diagrams.from === 'platform' ? settings.effective.diagrams.value : null
  const platformModel = settings.effective.chat_model_class.from === 'platform' ? settings.effective.chat_model_class.value : null
  const diagrams = settings.plans.diagrams ?? INHERIT
  const model = settings.plans.chat_model_class ?? INHERIT

  async function save(body: PlanSettingsBody) {
    if (!editable || inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setRefused(null)
    const r = await savePlanSettings(settings.tenant_id, body)
    inFlight.current = false
    setBusy(false)
    if (r.status === 'error') setRefused(r.error)
    else if (r.status === 'ok' || r.status === 'stale') onSaved(r.data)
  }

  const caps = settings.caps
  return (
    <Card title="Plan review" className="as-card">
      <div className="as-fields" data-testid="plan-review">
        <div className="as-field">
          <span className="as-k">
            {DIAGRAMS_LABEL}
            <InfoTip about={DIAGRAMS_LABEL} tip={DIAGRAM_EXAMPLES} />
          </span>
          <Segmented
            className="as-seg"
            label={DIAGRAMS_LABEL}
            value={diagrams}
            disabled={!editable || busy}
            options={[
              { key: 'on', label: 'on' },
              { key: 'off', label: 'off' },
              { key: INHERIT, label: inheritedLabel('platform’s', platformDiagrams) },
            ]}
            onChange={(k) => {
              if (k !== diagrams) void save({ diagrams: k })
            }}
          />
        </div>
        <div className="as-field">
          <span className="as-k">{MODEL_LABEL}</span>
          <Segmented
            className="as-seg"
            label={MODEL_LABEL}
            value={model}
            disabled={!editable || busy}
            options={[
              ...settings.chat_model_classes.map((c) => ({ key: c, label: c })),
              { key: INHERIT, label: inheritedLabel('platform’s', platformModel) },
            ]}
            onChange={(k) => {
              if (k !== model) void save({ chat_model_class: k })
            }}
          />
        </div>
        <p className="as-caps" data-testid="plan-caps" title={caps.set_by === null ? undefined : `Set by ${caps.set_by}`}>
          <span className="as-k">Caps</span>
          <span>{caps.replans} re-plans</span>
          <span>{caps.diagram_asks} diagram asks</span>
          <span>{caps.chat_turns} chat turns</span>
          <span>{dollars(caps.review_usd)} per review</span>
          <span className="as-who">platform admin</span>
        </p>
      </div>
      {refused !== null && <Refusal error={refused} title="Not saved" />}
    </Card>
  )
}
