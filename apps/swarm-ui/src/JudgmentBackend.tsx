import { useEffect, useId, useState } from 'react'
import { loadJudgment, putJudgmentBackend, type JevBackend, type JevOption } from './api'
import { Banner, Card } from './components'
import { UrRegion, useUrRead } from './RepositoriesParts'
import { timeAgo } from './types'
import './styles/keys.css'

/**
 * SETTINGS › API KEYS › JEV BACKEND (lane JUDGE-SELECT, owner decisions
 * 2026-10-14; swarm_api/judgment.py). Where this workspace's Jev judgments
 * go: Internal Jev (THE DEFAULT) or Internal Laya -- your internal decision
 * API, reached through the cluster's tailnet egress, so page text stays
 * inside your network and no TypeSafe key is needed -- or TypeSafe, which
 * receives page text and needs a key (the card below this one).
 *
 * An option the platform cannot serve now is disabled with the API's reason:
 * the internal ones while the tailnet egress is off, TypeSafe while no key
 * resolves for you here. A chosen backend that cannot run is said plainly --
 * Jev is then unavailable, never quietly sent elsewhere. Only the workspace's
 * owner or a platform admin can change it (`can_edit`); the API is the gate.
 */

const ROUTE = 'GET /v1/judgment'

/** What each option says about where page text goes. */
const OPTIONS: ReadonlyArray<{ backend: JevBackend; name: string; note: string }> = [
  { backend: 'internal-jev', name: 'Internal Jev', note: 'Stays inside your network.' },
  { backend: 'internal-laya', name: 'Internal Laya', note: 'Stays inside your network.' },
  { backend: 'typesafe', name: 'TypeSafe', note: 'Sends page text to TypeSafe; needs a key.' },
]

export function JevBackendCard() {
  const judgment = useUrRead(loadJudgment, 'judgment')
  return (
    <Card title="Jev backend">
      <p className="pk-lead">
        Where Jev, the browser&rsquo;s judgment model, sends what it is asked to judge for agents in this workspace.
      </p>
      <UrRegion state={judgment.state} route={ROUTE} what="the Jev backend" onRetry={judgment.reload}>
        {(d) => <JevBackendChoice tenantId={d.tenant_id} canEdit={d.can_edit} chosen={d.jev.backend} options={d.jev.options}
          available={d.jev.available} reason={d.jev.reason} setAt={d.jev.set_at} setBy={d.jev.set_by} onChange={judgment.reload} />}
      </UrRegion>
    </Card>
  )
}

function JevBackendChoice({
  tenantId,
  canEdit,
  chosen,
  options,
  available,
  reason,
  setAt,
  setBy,
  onChange,
}: {
  tenantId: string
  canEdit: boolean
  chosen: JevBackend
  options: JevOption[]
  available: boolean
  reason: string | null
  setAt: string | null
  setBy: string | null
  onChange: () => void
}) {
  const id = useId()
  // What the radio shows: the server's choice, or the one being saved.
  const [picked, setPicked] = useState<JevBackend>(chosen)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  useEffect(() => {
    if (!busy) setPicked(chosen)
  }, [chosen, busy])

  async function choose(backend: JevBackend) {
    if (busy || backend === picked) return
    const was = picked
    setPicked(backend)
    setBusy(true)
    setSaved(false)
    setFailed(null)
    const r = await putJudgmentBackend(tenantId, backend)
    setBusy(false)
    if (r.status === 'error') {
      setPicked(was)
      setFailed(`The Jev backend was not changed: ${r.error.message}`)
      return
    }
    setSaved(true)
    onChange()
  }

  const byBackend = new Map(options.map((o) => [o.backend, o]))
  return (
    <div className="jb-choice">
      <div className="jb-options" role="radiogroup" aria-label={`Jev backend for ${tenantId}`}>
        {OPTIONS.map((o) => {
          const status = byBackend.get(o.backend)
          const open = status?.available === true
          const off = !canEdit || busy || !open
          const why = `${id}-${o.backend}-why`
          return (
            <label key={o.backend} className={`jb-option${picked === o.backend ? ' is-on' : ''}${!open ? ' is-off' : ''}`}>
              <input
                type="radio"
                name={`${id}-jev-backend`}
                value={o.backend}
                checked={picked === o.backend}
                disabled={off}
                aria-describedby={why}
                onChange={() => void choose(o.backend)}
              />
              <span className="jb-text">
                <span className="jb-name">
                  {o.name}
                  {o.backend === 'internal-jev' && <span className="jb-default"> (default)</span>}
                </span>
                <span id={why} className="jb-note">
                  {o.note}
                  {!open && status?.reason ? ` Unavailable: ${status.reason}.` : ''}
                </span>
              </span>
            </label>
          )
        })}
      </div>
      {!available && (
        <p className="warn-text">{`Jev is unavailable for agents here: ${reason ?? 'its backend cannot be reached'}. Page text is not sent anywhere else instead.`}</p>
      )}
      {!canEdit && <p className="ur-small">Only the workspace&rsquo;s owner or a platform admin can change this.</p>}
      {setBy !== null && (
        <p className="ur-small">{`Chosen by ${setBy}${setAt !== null ? ` ${timeAgo(setAt)}` : ''}.`}</p>
      )}
      {saved && (
        <p className="pk-saved" role="status">
          Saved.
        </p>
      )}
      {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
    </div>
  )
}
