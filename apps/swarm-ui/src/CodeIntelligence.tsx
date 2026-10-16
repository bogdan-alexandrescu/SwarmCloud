import { useEffect, useId, useState } from 'react'
import { loadCodeIntelligence, loadJudgment, putCodeIntelligence, type JevBackend } from './api'
import { Banner, Card } from './components'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import { timeAgo } from './types'
import './styles/keys.css'

/**
 * SETTINGS › WORKSPACE › CODE INTELLIGENCE (lane CTXQ-UI over CTXQ-SET;
 * docs/design/context-quality.md §4.7, §5.4). Two switches decide what a step
 * is given about its repository, and only one of them sends anything anywhere:
 *
 *   * repository context -- the ranked pack and the graph tools, built from
 *     the platform's own index. Nothing leaves the platform. Each repository's
 *     Settings tab can turn it on or off, or inherit;
 *   * sending code to Jev -- excerpts of this workspace's code go to Jev for
 *     ranking and judging, at the backend chosen on Settings › API keys. THIS
 *     IS OFF BY DEFAULT for every workspace (owner decision, §4.7), and the
 *     card says what is sent and what never is before anyone turns it on.
 *
 * The setting is one field, `code_intelligence`, of the workspace's one
 * settings document, `workspace_settings/{tenant_id}` (owner decision
 * 2026-10-14). Only the workspace's owner or a platform admin can change it
 * (`can_edit`); the API is the gate, and a member sees the switch read-only.
 */

const ROUTE = 'GET /v1/code-intelligence'

/** Where Jev's judgments go, per backend: what the card says leaves the platform to. */
const DESTINATION: Readonly<Record<JevBackend, string>> = {
  'internal-jev': 'Internal Jev, which stays inside Saga',
  'internal-laya': 'Internal Laya, which stays inside Saga',
  typesafe: 'TypeSafe, a service outside Saga',
}

/** §5.4, in the reader's words: what is sent when the switch is on. */
const SENT =
  "Excerpts of this workspace's code (the files and symbols ranked for a step), with the task's title and the changed paths."

/** §5.4: what never is, even when on. */
const NEVER: readonly string[] = [
  'the content of .env, key, certificate, tfvars and tfstate files, secrets/ and credentials/ directories and the repository’s hard-stop paths (their names only)',
  'any excerpt in which a credential is found: it is dropped whole, not masked',
  'the task’s registered secrets and anything the redaction rules match',
  'issue and pull request authors, another repository’s content, the agent’s transcript and environment variables',
]

export function WorkspaceSettingsScreen() {
  return (
    <div className="pk-page">
      <PageHead title="Workspace" />
      <CodeIntelligenceCard />
    </div>
  )
}

export function CodeIntelligenceCard() {
  const ci = useUrRead(loadCodeIntelligence, 'code-intelligence')
  // Only to name where Jev's judgments go; the switch does not wait on it.
  const judgment = useUrRead(loadJudgment, 'judgment')
  const backend = judgment.state.status === 'ok' || judgment.state.status === 'stale' ? judgment.state.data.jev.backend : null
  return (
    <Card title="Code intelligence" action={<UrRefresh reads={[ci, judgment]} />}>
      <p className="pk-lead">
        What a step on this workspace&rsquo;s repositories is given about its code. Repository context &mdash; a ranked
        pack and the graph tools, built from the platform&rsquo;s own index &mdash; is set per repository on its
        Settings tab, and nothing leaves the platform for it. The switch below is the one that sends code anywhere.
      </p>
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

  const where = backend === null ? "this workspace's Jev backend (Settings › API keys)" : DESTINATION[backend]
  return (
    <div className="jb-choice">
      <label className={`jb-option${picked ? ' is-on' : ''}${!canEdit ? ' is-off' : ''}`}>
        <input
          type="checkbox"
          role="switch"
          aria-label={`Send code to Jev for ${tenantId}`}
          aria-describedby={`${id}-sent ${id}-never`}
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
            {`When on: ${SENT} They go to ${where}, which ranks and judges them. Jev advises; it never decides a verdict.`}
          </span>
        </span>
      </label>
      <div id={`${id}-never`} className="jb-note">
        Never sent, even when on:
        <ul>
          {NEVER.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      </div>
      {!canEdit && <p className="ur-small">Only the workspace&rsquo;s owner or a platform admin can change this.</p>}
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
