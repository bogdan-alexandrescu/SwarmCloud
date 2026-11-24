import { useEffect, useId, useState } from 'react'
import { loadCodeIntelligence, loadJudgment, putCodeIntelligence, type JevBackend } from './api'
import { Banner, Card } from './components'
import { HelpNote } from './HelpCard'
import { CONFIG_SECRETS_ADDRESS, addressToPath } from './paths'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { timeAgo } from './types'
import './styles/keys.css'

/**
 * CONFIG › WORKSPACE SETTINGS › CODE INTELLIGENCE (lane CTXQ-UI over CTXQ-SET;
 * docs/design/context-quality.md §4.7, §5.4). Two switches decide what a step
 * is given about its repository, and only one of them sends anything anywhere:
 *
 *   * repository context -- the ranked pack and the graph tools, built from
 *     the platform's own index. Nothing leaves the platform. Each repository's
 *     Settings tab can turn it on or off, or inherit;
 *   * sending code to Jev -- excerpts of this workspace's code go to Jev for
 *     ranking and judging, at the backend chosen on Config › Secrets › Keys for agents and MCP. THIS
 *     IS OFF BY DEFAULT for every workspace (owner decision, §4.7), and the
 *     card says what is sent and what never is before anyone turns it on.
 *
 * The setting is one field, `code_intelligence`, of the workspace's one
 * settings document, `workspace_settings/{tenant_id}` (owner decision
 * 2026-10-14). Only the workspace's owner or a platform admin can change it
 * (`can_edit`); the API is the gate, and a member sees the switch read-only.
 *
 * ONE SHORT LINE PER DISCLOSURE (part of #81: no visible run of nine words or
 * two sentences). The card keeps what a reader must see before flipping the
 * switch -- that it is the only thing that sends code, where the excerpts go,
 * what never goes, who may change it. The intro, §5.4's full "sent" and
 * "never sent" lists and "Jev advises; it never decides" are the
 * `workspace-settings` help topic's long form (help.ts), opened from the
 * page head's `?`, and the topic's short form is published to the switch
 * through `<HelpNote>`.
 */

const ROUTE = 'GET /v1/code-intelligence'

/** Where Jev's judgments go, per backend: what the card says leaves the platform to. */
const DESTINATION: Readonly<Record<JevBackend, string>> = {
  'internal-jev': 'Internal Jev, inside your network',
  'internal-laya': 'Internal Laya, inside your network',
  typesafe: 'TypeSafe, outside your network',
}

export function CodeIntelligenceCard() {
  const ci = useUrRead(loadCodeIntelligence, 'code-intelligence')
  // Only to name where Jev's judgments go; the switch does not wait on it.
  const judgment = useUrRead(loadJudgment, 'judgment')
  const backend = judgment.state.status === 'ok' || judgment.state.status === 'stale' ? judgment.state.data.jev.backend : null
  return (
    <Card title="Code intelligence" action={<UrRefresh reads={[ci, judgment]} />}>
      <p className="pk-lead">Only this switch sends code off the platform.</p>
      <UrRegion state={ci.state} route={ROUTE} what="the Code intelligence setting" onRetry={ci.reload}>
        {(d) => (
          <JevSwitch tenantId={d.tenant_id} canEdit={d.can_edit} on={d.jev} backend={backend} setAt={d.set_at} setBy={d.set_by} onChange={ci.reload} />
        )}
      </UrRegion>
    </Card>
  )
}

function JevSwitch({
  tenantId,
  canEdit,
  on,
  backend,
  setAt,
  setBy,
  onChange,
}: {
  tenantId: string
  canEdit: boolean
  on: boolean
  backend: JevBackend | null
  setAt: string | null
  setBy: string | null
  onChange: () => void
}) {
  const id = useId()
  // What the switch shows: the server's value, or the one being saved.
  const [picked, setPicked] = useState(on)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  useEffect(() => {
    if (!busy) setPicked(on)
  }, [on, busy])

  async function flip(next: boolean) {
    if (busy || !canEdit || next === picked) return
    const was = picked
    setPicked(next)
    setBusy(true)
    setSaved(false)
    setFailed(null)
    const r = await putCodeIntelligence(tenantId, next)
    setBusy(false)
    if (r.status === 'error') {
      setPicked(was)
      setFailed(`Code intelligence was not changed: ${r.error.message}`)
      return
    }
    setSaved(true)
    onChange()
  }

  return (
    <div className="jb-choice">
      <label className={`jb-option${picked ? ' is-on' : ''}${!canEdit ? ' is-off' : ''}`}>
        <input
          type="checkbox"
          role="switch"
          aria-label={`Send code to Jev for ${tenantId}`}
          aria-describedby={`${id}-sent ${id}-never ${id}-help`}
          checked={picked}
          disabled={!canEdit || busy}
          onChange={(e) => void flip(e.target.checked)}
        />
        <span className="jb-text">
          <span className="jb-name">
            Send code to Jev
            <span className="jb-default">{picked ? ' — on' : on || saved ? ' — off' : ' — off (default)'}</span>
          </span>
          <span id={`${id}-sent`} className="jb-note">
            {backend === null ? (
              <>
                Sends excerpts to the{' '}
                <a className="ctl-link" href={addressToPath(CONFIG_SECRETS_ADDRESS)}>
                  Jev backend
                </a>
                .
              </>
            ) : (
              `Sends excerpts to ${DESTINATION[backend]}.`
            )}
          </span>
        </span>
      </label>
      <p id={`${id}-never`} className="jb-note">
        Never sent: .env files, credentials, secrets, transcripts.
      </p>
      <HelpNote topic="workspace-settings" id={`${id}-help`} />
      {!canEdit && <p className="ur-small">Read-only: owner or platform admin only.</p>}
      {setBy !== null && <p className="ur-small">{`Set by ${setBy}${setAt !== null ? ` ${timeAgo(setAt)}` : ''}.`}</p>}
      {saved && (
        <p className="pk-saved" role="status">
          Saved.
        </p>
      )}
      {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
    </div>
  )
}
