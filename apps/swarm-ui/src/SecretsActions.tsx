import { useId, useLayoutEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  checkActionsRule,
  deleteActionsSecret,
  githubUrl,
  loadActionsSecrets,
  mintActionsLink,
  setActionsSecret,
  type ActionsEnvironment,
  type ActionsEnvironmentRule,
  type ActionsSecretList,
  type ActionsSecretName,
} from './api'
import { Banner, Button, ButtonLink, Card, Chip, Dialog, TypedConfirm } from './components'
import type { ApiError } from './fetch'
import { UrRegion, useUrRead } from './RepositoriesParts'
import { timeAgo } from './types'
import './styles/keys.css'
import './styles/secrets-actions.css'

/**
 * CONFIG › SECRETS › REPOSITORY › GITHUB ACTIONS (lane SSC-UI-ACTIONS, part of
 * #2158; docs/design/secrets-scopes.md §3 and §6.1, changed by BGD's answers in
 * §11; drawn as config-e.html S4b2, S4f and S4c's Actions branch, BGD's pick of
 * 2026-10-24). The repository scope's second destination: what SwarmCloud
 * writes into the repository's own GitHub Actions, read live from GitHub over
 * SSC-ACTIONS-API's routes. The scope picker, the repository picker and the
 * Agents | GitHub Actions switch are the Secrets page's; this is the pane under
 * the switch.
 *
 * THE VALUE (§11 Q5). swarm-api seals it with PyNaCl, so the page never seals
 * anything and carries no libsodium. The value is typed into ONE uncontrolled
 * field of the one-time form -- never React state, never an attribute, never
 * prefilled -- read once on Set, emptied before the request leaves, and sent
 * as `value` in that one PUT with the form's `link_token`. Every Set opens a
 * one-time form (§6.1): a link a tool minted (`link`), or one the page mints
 * for itself when Open the one-time form is pressed. The link's token is held
 * in a ref, sent once, and never drawn or put in the address.
 *
 * ENVIRONMENTS (§11 Q4). SwarmCloud creates none until user-scoped-secrets
 * step 5 lets the App hold Administration: write. So nothing here creates
 * one: "Create on GitHub" is a link to the page the API answered (the list's
 * `create_url`, served with no environment yet; a checked missing one's
 * own), and Check
 * rule is a GET of an environment's branch rule. Writing into an environment
 * whose rule is not main only asks the environment's name, typed, and sends it
 * as `confirm_environment`, the API's own gate (`guard_environment`).
 *
 * WHO MAY (§11 Q6): a workspace manager whom GitHub, asked at the write with
 * their own token, calls admin of the repository. The list answers it for
 * this caller (`can_write`, from the same two checks a write runs, and
 * `checked_at`, SSC-ACTIONS-API-2): true draws the mock-up's sentence, false
 * takes Set and Delete off and says the API's reason. An answer without it
 * falls back to `canManage` (the overview's word). Neither is the gate: the
 * API authorises the link and the write again.
 *
 * THE APP'S PERMISSION. Until the existing App's installation accepts
 * Secrets and Environments: Read and write, every read and write is refused
 * 409 with `*_not_granted` and the installation's review link; the pane says
 * so and links there. Only a github.com address is ever linked (`githubUrl`).
 */

/** A link a tool minted (`/config/secrets?scope=repository&repo=..&dest=actions&name=..&env=..&t=..`). */
export interface ActionsFormLink {
  name: string
  environment: string | null
  /** The Secrets page's capture of `t`; read off the address when absent. */
  token?: string | null
}

const LIST_ROUTE = 'GET /v1/repositories/{repo_id}/actions/secrets'

/** GitHub's rules for a secret's name (§3.9, `actions_secrets.SECRET_NAME`). */
const NAME_RULE = /^[A-Za-z_][A-Za-z0-9_]{0,199}$/

function nameProblem(name: string): string | null {
  if (name === '') return null
  if (!NAME_RULE.test(name)) return 'Letters, digits and _ only, not starting with a digit.'
  if (/^GITHUB_/i.test(name)) return 'GitHub reserves names starting with GITHUB_.'
  return null
}

/** Drop `t` from the address bar, keeping everything else. Idempotent. */
function dropLinkToken(): void {
  const url = new URL(window.location.href)
  if (!url.searchParams.has('t')) return
  url.searchParams.delete('t')
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`)
}

/** An environment's rule in the reader's words, short (a chip). */
function ruleWords(r: ActionsEnvironmentRule | null): string {
  if (r === null) return 'rule not read'
  if (!r.exists) return 'not on GitHub'
  if (r.rule === 'main_only') return `${r.branch} only`
  if (r.rule === 'protected_branches') return 'protected branches'
  if (r.rule === 'custom_branches') return r.branches.length === 0 ? 'selected branches' : `branches: ${r.branches.map((b) => b.name).join(', ')}`
  return 'any branch'
}

function RuleChip({ rule }: { rule: ActionsEnvironmentRule | null }) {
  const tone = rule === null ? undefined : !rule.exists ? 'bad' : rule.main_only ? 'live' : 'park'
  return <Chip tone={tone}>{ruleWords(rule)}</Chip>
}

/** Who can read a secret written there, in a sentence. */
function readersOf(environment: string | null, rule: ActionsEnvironmentRule | null, repository: string): ReactNode {
  if (environment === null) return <>Read by every workflow in {repository}.</>
  if (rule !== null && rule.main_only) return <>Read by workflows that run in <b>{environment}</b>, on {rule.branch} only. SwarmCloud grants nothing further.</>
  return <>Read by workflows that run in <b>{environment}</b>, which {ruleWords(rule) === 'any branch' ? 'any branch' : `its rule (${ruleWords(rule)})`} can deploy to.</>
}

/** The API's reason as a sentence: capitalised, with its full stop. */
function sentence(text: string): string {
  const t = text.trim()
  return `${t.charAt(0).toUpperCase()}${t.slice(1)}${/[.!?]$/.test(t) ? '' : '.'}`
}

/** "5 d ago", or the API's absence of a time. */
function updatedWords(s: ActionsSecretName): string {
  return s.updated_at === null ? 'not said' : timeAgo(s.updated_at)
}

/**
 * A refusal about the App's installation (`detail.reason`), as the API
 * answers it on any of these routes; null for every other failure.
 */
interface AppRefusal {
  kind: 'not_granted' | 'not_covered' | 'not_installed'
  url: string | null
}

function appRefusalOf(e: ApiError | null | undefined): AppRefusal | null {
  if (e === null || e === undefined) return null
  const d = (typeof e.detail === 'object' && e.detail !== null ? e.detail : {}) as Record<string, unknown>
  const reason = typeof d.reason === 'string' ? d.reason : ''
  if (reason.endsWith('_not_granted')) return { kind: 'not_granted', url: githubUrl(d.review_url) }
  if (reason === 'actions_repository_not_covered') return { kind: 'not_covered', url: githubUrl(d.review_url) }
  if (reason === 'actions_app_not_installed') return { kind: 'not_installed', url: githubUrl(d.install_url) }
  return null
}

function AppRefusalBanner({ refusal, repository }: { refusal: AppRefusal; repository: string }) {
  const out = { target: '_blank', rel: 'noreferrer' }
  if (refusal.kind === 'not_installed') {
    return (
      <Banner tone="warn" title={`The SwarmCloud GitHub App is not installed for ${repository}`}
        actions={refusal.url === null ? undefined : <ButtonLink kind="secondary" size="sm" href={refusal.url} {...out}>Install the App on GitHub ↗</ButtonLink>}>
        Its owner installs it and enables the repository; until then SwarmCloud can neither list nor write its Actions secrets.
      </Banner>
    )
  }
  if (refusal.kind === 'not_covered') {
    return (
      <Banner tone="warn" title={`The App’s installation does not cover ${repository}`}
        actions={refusal.url === null ? undefined : <ButtonLink kind="secondary" size="sm" href={refusal.url} {...out}>Open the installation on GitHub ↗</ButtonLink>}>
        The installation’s owner adds the repository to it. Nothing was written.
      </Banner>
    )
  }
  return (
    <Banner tone="warn" title={`The SwarmCloud GitHub App lacks Secrets permission on ${repository}`}
      actions={refusal.url === null ? undefined : <ButtonLink kind="secondary" size="sm" href={refusal.url} {...out}>Review the App’s permissions on GitHub ↗</ButtonLink>}>
      Until the installation’s owner accepts Secrets and Environments: Read and write on GitHub, SwarmCloud can neither
      list nor write this repository’s Actions secrets. Nothing was written.
    </Banner>
  )
}

/** Where a secret is written: the repository, or one of its environments. */
interface Place {
  environment: string | null
}

/** The sheet's state: which step, and what has been chosen so far. */
interface Sheet {
  step: 2 | 3 | 4
  /** undefined until a destination is picked. */
  environment: string | null | undefined
  name: string
  /** Opened from a tool's link: the form is shown at once, with its token. */
  fromLink: boolean
}

export function SecretsActions({
  repoId,
  repository,
  link = null,
  canManage = null,
}: {
  repoId: string
  /** `owner/repo`, from the registration the Secrets page picked. */
  repository: string
  link?: ActionsFormLink | null
  /** The overview's `can_manage` for this repository, when the Secrets page has it; null is not known. */
  canManage?: boolean | null
}) {
  const list = useUrRead(() => loadActionsSecrets(repoId), `actions-secrets:${repoId}`)
  // A tool's link: its token, read once; null once it has been sent. A ref,
  // never state or a prop of anything drawn.
  const token = useRef<string | null | undefined>(undefined)
  if (token.current === undefined) {
    token.current = link === null
      ? null
      : link.token !== undefined ? link.token : new URLSearchParams(window.location.search).get('t')
  }
  useLayoutEffect(() => {
    if (link !== null) dropLinkToken()
  }, [link])
  const [sheet, setSheet] = useState<Sheet | null>(
    link === null ? null : { step: 4, environment: link.environment, name: link.name, fromLink: true },
  )
  const [deleting, setDeleting] = useState<(Place & { name: string }) | null>(null)
  const [said, setSaid] = useState<string | null>(null)
  const [checking, setChecking] = useState(false)
  const [checked, setChecked] = useState<ActionsEnvironmentRule | null>(null)
  const data = list.state.status === 'ok' || list.state.status === 'stale' ? list.state.data : null
  const refusal = list.state.status === 'error' ? appRefusalOf(list.state.error) : null
  // The API's answer when it gave one (SSC-ACTIONS-API-2), else the overview's
  // word. Either way it decides only what is offered: every write is
  // authorised again by the API, GitHub asked again.
  const writable = data?.can_write ?? null
  const manages = writable ?? canManage !== false

  const open = (s: Omit<Sheet, 'fromLink'>) => {
    setSaid(null)
    setSheet({ ...s, fromLink: false })
  }
  const createUrl = checked?.create_url ?? data?.create_url ?? null

  return (
    <div className="sa-pane">
      <p className="sa-lead">
        Written into {repository} on GitHub. SwarmCloud reads names and update times, never values, and keeps only
        the name, who set it and when.
      </p>
      {refusal !== null ? (
        <AppRefusalBanner refusal={refusal} repository={repository} />
      ) : (
        <>
          {writable === true && data !== null ? (
            <Banner tone="info" title={`You may write here: workspace admin, and GitHub calls you admin of ${repository}${data.checked_at === null ? '' : ` (checked ${timeAgo(data.checked_at)})`}.`}>
              SwarmCloud asks GitHub again at each write, with your own GitHub connection.
            </Banner>
          ) : (
            <Banner tone="info" title={manages ? 'Who may write here' : 'Read only for you'}>
              {manages
                ? <>Setting or deleting takes both: you manage this workspace, and GitHub calls you admin of {repository}. SwarmCloud asks GitHub at each write, with your own GitHub connection.</>
                : <>
                    {data?.cannot_write_because != null && <>{sentence(data.cannot_write_because)} </>}
                    Only a workspace manager whom GitHub calls admin of {repository} sets or deletes these. You can read the names here.
                  </>}
            </Banner>
          )}
          {said !== null && <p className="pk-saved" role="status">{said}</p>}
          <UrRegion state={list.state} route={LIST_ROUTE} what="This repository's GitHub Actions secrets" plural onRetry={list.reload}>
            {(d) => (
              <div className="sa-grid">
                <div role="region" aria-label="Repository secrets" className="sa-cell">
                  <Card title="Repository secrets"
                    action={manages ? <Button kind="primary" size="sm" onClick={() => open({ step: 2, environment: null, name: '' })}>Set a secret</Button> : undefined}>
                    <NameTable rows={d.secrets} label={`Repository secrets of ${repository}`} place={{ environment: null }} manages={manages}
                      empty={`No repository secret on ${repository} yet.`}
                      onSet={(name) => open({ step: 4, environment: null, name })}
                      onDelete={(name) => setDeleting({ environment: null, name })} />
                  </Card>
                </div>
                <div role="region" aria-label="Environments" className="sa-cell">
                  <Card title="Environments" action={
                    <span className="sa-acts">
                      {createUrl !== null && (
                        <ButtonLink kind="secondary" size="sm" href={createUrl} target="_blank" rel="noreferrer">Create on GitHub ↗</ButtonLink>
                      )}
                      <Button kind="secondary" size="sm" aria-expanded={checking} onClick={() => setChecking((c) => !c)}>Check rule</Button>
                    </span>
                  }>
                    {checking && (
                      <CheckRule repoId={repoId} repository={repository} environments={d.environments}
                        onChecked={(r) => {
                          setChecked(r)
                          if (r.exists && !d.environments.some((e) => e.environment === r.environment)) list.reload()
                        }} />
                    )}
                    {d.environments.length === 0 ? (
                      <p className="sa-note">No environment on {repository} yet.</p>
                    ) : (
                      d.environments.map((e) => (
                        <EnvironmentBlock key={e.environment} env={e} repository={repository} manages={manages}
                          onSetHere={() => open({ step: 3, environment: e.environment, name: '' })}
                          onSet={(name) => open({ step: 4, environment: e.environment, name })}
                          onDelete={(name) => setDeleting({ environment: e.environment, name })} />
                      ))
                    )}
                    {d.environments_truncated && (
                      <p className="sa-note">GitHub was read for the first 30 environments only; more may exist.</p>
                    )}
                    <p className="sa-note">
                      SwarmCloud does not create environments yet: create one on GitHub, then Check rule reads its
                      branch rule.
                    </p>
                  </Card>
                </div>
              </div>
            )}
          </UrRegion>
        </>
      )}
      {sheet !== null && (
        <SetSheet
          repoId={repoId}
          repository={repository}
          data={data}
          sheet={sheet}
          createUrl={createUrl}
          onChange={setSheet}
          takeToken={() => {
            const t = token.current ?? null
            token.current = null
            return t
          }}
          onClose={() => setSheet(null)}
          onDone={(words) => {
            setSheet(null)
            setSaid(words)
            list.reload()
          }}
        />
      )}
      {deleting !== null && (
        <DeleteActionsSecret
          repoId={repoId}
          repository={repository}
          target={deleting}
          onKeep={() => setDeleting(null)}
          onDone={(words) => {
            setDeleting(null)
            setSaid(words)
            list.reload()
          }}
        />
      )}
    </div>
  )
}

function NameTable({
  rows,
  label,
  place,
  manages,
  empty,
  onSet,
  onDelete,
}: {
  rows: ActionsSecretName[]
  label: string
  place: Place
  manages: boolean
  empty: ReactNode
  onSet: (name: string) => void
  onDelete: (name: string) => void
}) {
  const where = place.environment === null ? '' : ` in ${place.environment}`
  const from = place.environment === null ? '' : ` from ${place.environment}`
  if (rows.length === 0) return <p className="sa-note">{empty}</p>
  return (
    <div className="table-wrap">
      {/* The registered-MCP-servers table's rules (keys.css), which stack it into cards on a phone. */}
      <table role="table" className="mcp-servers sa-table" aria-label={label}>
        <thead>
          <tr>
            <th scope="col">Name</th>
            <th scope="col">Updated</th>
            <th scope="col">Set by</th>
            {manages && <th scope="col" aria-label="Actions" />}
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.name}>
              <th scope="row" data-label="Name"><code>{s.name}</code></th>
              <td data-label="Updated" title={s.updated_at ?? undefined}>{updatedWords(s)}</td>
              <td data-label="Set by" title={s.set_by_swarmcloud && s.set_by !== null ? `by ${s.set_by}` : undefined}>
                {s.set_by_swarmcloud ? 'SwarmCloud' : 'on GitHub'}
              </td>
              {manages && (
                <td data-label="Actions">
                  <span className="pk-form-row">
                    <Button kind="secondary" size="sm" aria-label={`Set ${s.name}${where}`} onClick={() => onSet(s.name)}>Set</Button>
                    <Button kind="danger" size="sm" aria-label={`Delete ${s.name}${from}`} onClick={() => onDelete(s.name)}>Delete</Button>
                  </span>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function EnvironmentBlock({
  env,
  repository,
  manages,
  onSetHere,
  onSet,
  onDelete,
}: {
  env: ActionsEnvironment
  repository: string
  manages: boolean
  onSetHere: () => void
  onSet: (name: string) => void
  onDelete: (name: string) => void
}) {
  const open = !env.rule.main_only
  return (
    <div className="sa-env">
      <div className="sa-env-h">
        <b><code>{env.environment}</code></b>
        <RuleChip rule={env.rule} />
        {manages && (
          <Button kind="secondary" size="sm" className="sa-env-set" aria-label={`Set a secret in ${env.environment}`} onClick={onSetHere}>
            Set a secret here
          </Button>
        )}
      </div>
      {env.secrets.length > 0 && (
        <NameTable rows={env.secrets} label={`Secrets of ${env.environment}`} place={{ environment: env.environment }} manages={manages}
          empty={null} onSet={onSet} onDelete={onDelete} />
      )}
      {(env.secrets.length === 0 || open) && (
        <p className="sa-note">
          {env.secrets.length === 0 && <>No secret in {env.environment} yet. </>}
          {open && (
            <>
              Writing here asks you to type <b>{env.environment}</b>:{' '}
              {env.rule.rule === 'any_branch' ? 'a workflow on any branch can read it.' : `${ruleWords(env.rule)} can deploy to it, not ${env.rule.branch} alone.`}
            </>
          )}
          {!open && env.secrets.length === 0 && <>Only workflows on {env.rule.branch} in {repository} read it.</>}
        </p>
      )}
    </div>
  )
}

/** Check rule (§11 Q4): reads one environment's branch rule. It writes nothing. */
function CheckRule({
  repoId,
  repository,
  environments,
  onChecked,
}: {
  repoId: string
  repository: string
  environments: ActionsEnvironment[]
  onChecked: (r: ActionsEnvironmentRule) => void
}) {
  const id = useId()
  const [name, setName] = useState(environments[0]?.environment ?? '')
  const [busy, setBusy] = useState(false)
  const [answer, setAnswer] = useState<ActionsEnvironmentRule | null>(null)
  const [failed, setFailed] = useState<ApiError | null>(null)

  async function check(e: FormEvent) {
    e.preventDefault()
    const env = name.trim()
    if (env === '' || busy) return
    setBusy(true)
    setFailed(null)
    setAnswer(null)
    const r = await checkActionsRule(repoId, env)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(r.error)
      return
    }
    if (r.status === 'ok' || r.status === 'stale') {
      setAnswer(r.data)
      onChecked(r.data)
    }
  }

  const refusal = appRefusalOf(failed)
  return (
    <div className="sa-check">
      <form className="pk-form-row" autoComplete="off" aria-label="Check an environment's rule" onSubmit={(e) => void check(e)}>
        <label htmlFor={`${id}-env`}>Environment</label>
        <input id={`${id}-env`} className="ur-search pk-input sa-env-input" value={name} maxLength={255} spellCheck={false}
          autoCapitalize="off" autoCorrect="off" placeholder="repo-secrets" list={`${id}-known`}
          onChange={(e) => setName(e.target.value)} />
        <datalist id={`${id}-known`}>
          {environments.map((e) => <option key={e.environment} value={e.environment} />)}
        </datalist>
        <Button kind="secondary" size="sm" type="submit" busy={busy ? 'Checking…' : false} disabled={name.trim() === ''}>Check</Button>
      </form>
      {answer !== null && answer.exists && (
        <p className="sa-note" role="status">
          <b>{answer.environment}</b> <RuleChip rule={answer} />{' '}
          {answer.main_only
            ? `Only ${answer.branch} deploys to it: a secret written there reaches workflows on ${answer.branch} alone.`
            : `Not main only: writing there asks you to type ${answer.environment}.`}
        </p>
      )}
      {answer !== null && !answer.exists && (
        <Banner tone="warn" title={`No environment named ${answer.environment} on ${repository}`}
          actions={answer.create_url === null ? undefined : (
            <ButtonLink kind="secondary" size="sm" href={answer.create_url} target="_blank" rel="noreferrer">
              Create {answer.environment} on GitHub ↗
            </ButtonLink>
          )}>
          {answer.steps ?? `Create it on GitHub with its deployment branches set to ${answer.branch} only, then check its rule here again.`}
        </Banner>
      )}
      {refusal !== null && <AppRefusalBanner refusal={refusal} repository={repository} />}
      {failed !== null && refusal === null && <Banner tone="bad" title={`The rule was not read: ${failed.message}`} role="alert" />}
    </div>
  )
}

const STEPS = ['Scope', 'Destination', 'Who reads it', 'One-time form'] as const

/**
 * THE SHEET: S4c's GitHub Actions branch, from its second step (the scope is
 * the repository already picked): destination with its environment, the
 * name, then the one-time form.
 */
function SetSheet({
  repoId,
  repository,
  data,
  sheet,
  createUrl,
  onChange,
  takeToken,
  onClose,
  onDone,
}: {
  repoId: string
  repository: string
  data: ActionsSecretList | null
  sheet: Sheet
  createUrl: string | null
  onChange: (s: Sheet) => void
  /** A tool's link token, handed over once; null after, or without one. */
  takeToken: () => string | null
  onClose: () => void
  onDone: (words: string) => void
}) {
  const id = useId()
  const known = data?.environments ?? []
  const [rules, setRules] = useState<Readonly<Record<string, ActionsEnvironmentRule>>>({})
  const ruleOf = (env: string | null | undefined): ActionsEnvironmentRule | null =>
    env === null || env === undefined ? null : rules[env] ?? known.find((e) => e.environment === env)?.rule ?? null
  const env = sheet.environment
  const rule = ruleOf(env)
  const where = env === null || env === undefined ? repository : `${repository} › ${env}`
  const [checkBusy, setCheckBusy] = useState(false)
  const [checkFailed, setCheckFailed] = useState<ApiError | null>(null)

  async function recheck() {
    if (env === null || env === undefined || checkBusy) return
    setCheckBusy(true)
    setCheckFailed(null)
    const r = await checkActionsRule(repoId, env)
    setCheckBusy(false)
    if (r.status === 'error') setCheckFailed(r.error)
    else if (r.status === 'ok' || r.status === 'stale') setRules((m) => ({ ...m, [env]: r.data }))
  }

  const title = sheet.step === 2 ? 'Who is it for?' : sheet.step === 3 ? 'Who reads it, and its name' : 'Set the value in a one-time form'
  const problem = nameProblem(sheet.name.trim())
  const nameOk = sheet.name.trim() !== '' && problem === null
  const back = (step: Sheet['step']) => onChange({ ...sheet, step })

  return (
    <Dialog
      title="Set a GitHub Actions secret"
      onClose={onClose}
      actions={
        sheet.step === 2 ? (
          <>
            <Button kind="ghost" onClick={onClose}>Cancel</Button>
            <Button kind="primary" disabled={env === undefined} onClick={() => back(3)}>Next</Button>
          </>
        ) : sheet.step === 3 ? (
          <>
            <Button kind="ghost" onClick={() => back(2)}>Back</Button>
            <Button kind="primary" disabled={!nameOk} onClick={() => back(4)}>Next</Button>
          </>
        ) : (
          <Button kind="ghost" onClick={sheet.fromLink ? onClose : () => back(3)}>{sheet.fromLink ? 'Close' : 'Back'}</Button>
        )
      }
    >
      <ol className="sa-steps" aria-label="Steps">
        {STEPS.map((s, i) => {
          const n = i + 1
          const state = n < sheet.step ? 'is-done' : n === sheet.step ? 'is-on' : ''
          return (
            <li key={s} className={state} aria-current={n === sheet.step ? 'step' : undefined}>
              <i aria-hidden="true">{n < sheet.step ? '✓' : n}</i>
              {s}
              {n === 1 && <span className="sa-vh">: Repository</span>}
            </li>
          )
        })}
      </ol>
      <h3 className="sa-step-title">{title}</h3>

      {sheet.step === 2 && (
        <div className="sa-step">
          <p className="sa-note">
            GitHub Actions in <b>{repository}</b>. Writing needs GitHub admin of the repository: SwarmCloud asks GitHub
            when the form opens and again at the write.
          </p>
          <fieldset className="sa-opts">
            <legend>Environment</legend>
            <Opt id={`${id}-repo`} name={`${id}-dest`} checked={env === null} onPick={() => onChange({ ...sheet, environment: null })}
              title="Repository level" sub="Every workflow in the repository can read it." />
            {known.map((e) => {
              const r = ruleOf(e.environment)
              return (
                <Opt key={e.environment} id={`${id}-env-${e.environment}`} name={`${id}-dest`} checked={env === e.environment}
                  onPick={() => onChange({ ...sheet, environment: e.environment })}
                  title={`${e.environment} · ${ruleWords(r)}`}
                  sub={r !== null && r.main_only ? `Only workflows on ${r.branch}.` : `You will type ${e.environment} to confirm.`} />
              )
            })}
          </fieldset>
          <div className="pk-form-row">
            {createUrl !== null && (
              <ButtonLink kind="secondary" size="sm" href={createUrl} target="_blank" rel="noreferrer">Create on GitHub ↗</ButtonLink>
            )}
            <Button kind="secondary" size="sm" busy={checkBusy ? 'Checking…' : false} disabled={env === null || env === undefined}
              title={env === null || env === undefined ? 'Pick an environment to read its rule' : undefined} onClick={() => void recheck()}>
              Check rule
            </Button>
          </div>
          {env !== null && env !== undefined && rules[env] !== undefined && (
            <p className="sa-note" role="status"><b>{env}</b> <RuleChip rule={rules[env]!} /> read from GitHub just now.</p>
          )}
          {checkFailed !== null && (appRefusalOf(checkFailed) !== null
            ? <AppRefusalBanner refusal={appRefusalOf(checkFailed)!} repository={repository} />
            : <Banner tone="bad" title={`The rule was not read: ${checkFailed.message}`} role="alert" />)}
          <p className="sa-note">SwarmCloud does not create environments yet: create one on GitHub, then Check rule reads its branch rule.</p>
        </div>
      )}

      {sheet.step === 3 && (
        <div className="sa-step pk-form">
          <p className="sa-note">{readersOf(env ?? null, rule, repository)}</p>
          <label htmlFor={`${id}-name`}>Name</label>
          <input id={`${id}-name`} className="ur-search pk-input" value={sheet.name} maxLength={200} spellCheck={false}
            autoCapitalize="off" autoCorrect="off" autoComplete="off" placeholder="MERGE_APP_PRIVATE_KEY" aria-describedby={`${id}-name-why`}
            onChange={(e) => onChange({ ...sheet, name: e.target.value })} />
          <span id={`${id}-name-why`} className={problem === null ? 'jb-note' : 'jb-note sa-bad'}>
            {problem ?? 'Letters, digits and _; not starting with a digit or GITHUB_. Never the value.'}
          </span>
        </div>
      )}

      {sheet.step === 4 && (
        <OneTimeForm
          key={`${env ?? ''}\u0000${sheet.name}`}
          repoId={repoId}
          repository={repository}
          name={sheet.name.trim()}
          environment={env ?? null}
          rule={rule}
          where={where}
          fromLink={sheet.fromLink}
          takeToken={takeToken}
          onDone={onDone}
        />
      )}
    </Dialog>
  )
}

function Opt({ id, name, checked, onPick, title, sub }: { id: string; name: string; checked: boolean; onPick: () => void; title: string; sub: string }) {
  return (
    <label htmlFor={id} className={checked ? 'sa-opt is-on' : 'sa-opt'}>
      <input id={id} type="radio" name={name} checked={checked} onChange={onPick} aria-describedby={`${id}-sub`} />
      <span>
        <b>{title}</b>
        <small id={`${id}-sub`}>{sub}</small>
      </span>
    </label>
  )
}

/** The `/sc` line that mints the same form from a terminal (§6.4). */
function scLine(name: string, repository: string, environment: string | null): string {
  return `/sc secrets link repository ${name} --repo ${repository} --actions${environment === null ? '' : ` --env ${environment}`}`
}

/**
 * THE ONE-TIME FORM (S4c step 4 and S4d). Before its link exists it offers
 * Open the one-time form, which mints one for exactly this destination and
 * name; a tool's link opens it at once. The value field is uncontrolled and
 * never prefilled; Set reads it once and empties it before the request
 * leaves, whatever the answer. A link is consumed by the attempt, so a
 * refused write drops it and asks for a new one.
 */
function OneTimeForm({
  repoId,
  repository,
  name,
  environment,
  rule,
  where,
  fromLink,
  takeToken,
  onDone,
}: {
  repoId: string
  repository: string
  name: string
  environment: string | null
  rule: ActionsEnvironmentRule | null
  where: string
  fromLink: boolean
  takeToken: () => string | null
  onDone: (words: string) => void
}) {
  const id = useId()
  // The link's token, never state: nothing renders it. `opened` says only whether one is held.
  const held = useRef<string | null>(fromLink ? takeToken() : null)
  const [opened, setOpened] = useState(fromLink)
  const [minting, setMinting] = useState(false)
  const [filled, setFilled] = useState(false)
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)
  const [failedWords, setFailedWords] = useState<string | null>(null)
  const value = useRef<HTMLInputElement>(null)
  // §11 Q4: anything but the API's own "main only" asks the name typed, an
  // unread rule included; the API gates it again.
  const needsTyped = environment !== null && !(rule !== null && rule.main_only)
  const typedOk = !needsTyped || typed === environment

  async function mint() {
    if (minting) return
    setMinting(true)
    setFailed(null)
    setFailedWords(null)
    const r = await mintActionsLink(repoId, name, environment)
    setMinting(false)
    if (r.status === 'error') {
      setFailed(r.error)
      setFailedWords(`The form was not opened: ${r.error.message}`)
      return
    }
    if (r.status === 'ok' || r.status === 'stale') {
      held.current = r.data.token
      setOpened(true)
    }
  }

  async function save(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    // Read once, and emptied before the request leaves, whatever it answers.
    const v = value.current?.value ?? ''
    if (value.current !== null) value.current.value = ''
    setFilled(false)
    if (v === '' || !typedOk) return
    const t = held.current
    held.current = null
    setBusy(true)
    setFailed(null)
    setFailedWords(null)
    const r = await setActionsSecret(repoId, name, v, {
      environment,
      confirm_environment: needsTyped ? typed : null,
      link_token: t,
    })
    setBusy(false)
    if (r.status === 'error') {
      // The API consumed the link before it refused, or the link was the
      // refusal: either way it is spent, and a new one is minted to retry.
      setOpened(false)
      setFailed(r.error)
      setFailedWords(r.error.code === 'SECRET_LINK_INVALID'
        ? `${name} was not set: the one-time link is used, expired or not yours. Open a new one-time form.`
        : `${name} was not set: ${r.error.message}`)
      return
    }
    const created = r.status === 'ok' && r.data.created
    onDone(`${name} was set in ${where}${created ? '' : ', replacing its value'}. The link is used up; the value is not shown again, here or anywhere.`)
  }

  const refusal = appRefusalOf(failed)
  return (
    <div className="sa-step">
      {fromLink && opened && (
        <Banner tone="info" title="Opened from a one-time link">
          Paste the value into the form below: the /sc command and this page’s address never see it. The link works
          once, for ten minutes.
        </Banner>
      )}
      <dl className="sa-kv">
        <dt>Where</dt>
        <dd>
          {repository} › GitHub Actions{environment === null ? ' › repository level' : <> › environment <b>{environment}</b> <RuleChip rule={rule} /></>}
        </dd>
        <dt>Name</dt>
        <dd><code>{name}</code></dd>
      </dl>
      {!opened ? (
        <>
          <p className="sa-note">
            Opens a form for <b>{name}</b> in {where}. It works once, for ten minutes. The value is sealed in SwarmCloud,
            written to GitHub and never shown again.
          </p>
          <div className="sa-terminal">
            <span>From a terminal instead:</span>
            <code>{scLine(name, repository, environment)}</code>
          </div>
          <div className="pk-form-row">
            <Button kind="primary" size="sm" busy={minting ? 'Opening…' : false} onClick={() => void mint()}>Open the one-time form</Button>
          </div>
        </>
      ) : (
        <form className="pk-form" autoComplete="off" aria-label={`Set ${name}`} onSubmit={(e) => void save(e)}>
          <label htmlFor={`${id}-value`}>Value</label>
          <input
            id={`${id}-value`}
            ref={value}
            className="ur-search pk-input"
            type="password"
            autoComplete="off"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            placeholder="Paste the value"
            aria-describedby={`${id}-value-why`}
            onChange={() => setFilled((value.current?.value ?? '') !== '')}
          />
          <span id={`${id}-value-why`} className="jb-note">At most 48 KB. The form is cleared whatever the answer.</span>
          {needsTyped && (
            <>
              <label htmlFor={`${id}-confirm`}>Type {environment} to write into it</label>
              <input id={`${id}-confirm`} className="ur-search pk-input" value={typed} autoComplete="off" spellCheck={false}
                autoCapitalize="off" autoCorrect="off" aria-describedby={`${id}-confirm-why`} onChange={(e) => setTyped(e.target.value)} />
              <span id={`${id}-confirm-why`} className="jb-note">
                {rule === null
                  ? `${environment}'s branch rule was not read, so it is treated as open to any branch.`
                  : `${environment} lets ${ruleWords(rule) === 'any branch' ? 'any branch' : ruleWords(rule)} deploy: a workflow there can read this secret.`}
              </span>
            </>
          )}
          <div className="pk-form-row">
            <Button kind="primary" size="sm" type="submit" busy={busy ? 'Setting…' : false} disabled={!filled || !typedOk}>Set secret</Button>
          </div>
        </form>
      )}
      {refusal !== null && <AppRefusalBanner refusal={refusal} repository={repository} />}
      {failedWords !== null && refusal === null && <Banner tone="bad" title={failedWords} role="alert" />}
    </div>
  )
}

/**
 * DELETE IS TYPED (§3.8): the person types the secret's name, which the
 * API's body repeats as `confirm_name`. GitHub destroys it; SwarmCloud never
 * kept the value, so nothing can bring it back but setting it again.
 */
function DeleteActionsSecret({
  repoId,
  repository,
  target,
  onKeep,
  onDone,
}: {
  repoId: string
  repository: string
  target: Place & { name: string }
  onKeep: () => void
  onDone: (words: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)
  const where = target.environment === null ? repository : `${repository} › ${target.environment}`

  async function confirm() {
    if (busy) return
    setBusy(true)
    setFailed(null)
    const r = await deleteActionsSecret(repoId, target.name, target.name, target.environment)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(r.error)
      return
    }
    onDone(`${target.name} was deleted from ${where}.`)
  }

  const refusal = appRefusalOf(failed)
  return (
    <TypedConfirm
      title={`Delete ${target.name}${target.environment === null ? '' : ` from ${target.environment}`}`}
      name={target.name}
      verb="Delete"
      keep="Keep it"
      busy={busy ? 'Deleting…' : false}
      error={failed === null ? null : refusal !== null ? <AppRefusalBanner refusal={refusal} repository={repository} /> : `${target.name} was not deleted: ${failed.message}`}
      onConfirm={() => void confirm()}
      onClose={onKeep}
    >
      <p>
        GitHub destroys <code>{target.name}</code> in {where}. Workflows that read it fail until it is set again, and
        SwarmCloud cannot restore it: it never kept the value.
      </p>
    </TypedConfirm>
  )
}
