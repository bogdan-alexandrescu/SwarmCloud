import { useEffect, useId, useLayoutEffect, useRef, useState, type FormEvent, type MouseEvent, type ReactNode } from 'react'
import {
  deleteBrowserLogin,
  deleteProviderKey,
  loadRepositories,
  loadSecretsOverview,
  loadWorkspaceSecretCatalogue,
  mintProviderKeyLink,
  mintRunnerKeyLink,
  putProviderKey,
  putRunnerKey,
  secretStore,
  type MintedLink,
  type SecretStoreApi,
  type SecretStoreTarget,
  type SecretsOverview,
  type SecretsRow,
  type SecretsScope,
  type SecretsShadow,
  type WorkspaceSecretCatalogue,
  type WorkspaceSecretConsumer,
} from './api'
import { Banner, Button, Card, Segmented, TypedConfirm, routedClick } from './components'
import { chooseTenant, type Result } from './fetch'
import { addressToPath, secretsLinkToken, secretsLinkView } from './paths'
import { UrRefresh, UrRegion, useUrRead, type UrRead } from './RepositoriesParts'
import type { RepoRecord } from './RepositoriesData'
import { JevBackendCard } from './JudgmentBackend'
import { PageHead } from './Shell'
import { timeAgo } from './types'
import {
  AddSecret,
  DEFAULT_KIND,
  DEFAULT_PROVIDER,
  GROUPS,
  GrantMatrix,
  KIND_WORDS,
  RotateSecret,
  SecretList,
  cellOf,
  type AddFixed,
} from './WorkspaceSecrets'
import './styles/keys.css'
import './styles/secrets.css'

/**
 * CONFIG › SECRETS (lane SSC-UI, part of #2158; docs/design/secrets-scopes.md
 * §6.1 with BGD's answers in its §11, drawn as Config proposal E S4,
 * docs/web-ui/mockups/config-e.html, BGD's pick of 2026-10-24). Every secret
 * an agent, a tool or a repository's QA run reads, at the level it belongs
 * to, on one page. NO STORE MIGRATES (§4): this page is a view over the
 * stores that already exist, read through `GET /v1/secrets/overview`, and
 * each Set opens THAT store's one-time form.
 *
 * THE ADDRESS IS THE PAGE (`/config/secrets?scope=..&repo=..&dest=..`, paths.ts
 * `secretsLinkView`): the scope picker, the repository and its destination,
 * and which one-time form is open. A copied address opens the same view. The
 * link's token is never part of it.
 *
 * SHADOWING (Q1: the person's, then the repository's, then the workspace's)
 * is drawn as the overview reports it, beside the row it concerns: a loser
 * says which scope wins and where, a winner what it overrides.
 *
 * EVERY SET OPENS A ONE-TIME FORM, never prefilled (§6.1). Either the link a
 * tool minted -- App read its token off the bar before the router rewrote it
 * (`secretsLinkToken`), and this page drops `t` from the bar in a layout
 * effect, before anything paints -- or a link this page mints for itself when
 * the form opens. The token lives in a ref, goes back once as `link_token`
 * with the write its link was minted for, and is never drawn, stored or put
 * in an address. The form writes to the store its view names and to no other
 * (`SecretStoreTarget`), so a form opened on one scope cannot be submitted to
 * another; a personal secret is written only in the person's own workspace.
 *
 * THE GITHUB ACTIONS DESTINATION is SSC-UI-ACTIONS' pane: this page draws the
 * switch and SwarmCloud's record of what it wrote there (§3.8), and leaves
 * GitHub's live list, the Set form and Delete to that lane.
 */

type Dest = 'agents' | 'actions'

/** The view, as `secretsLinkView` keeps it. */
interface SecretsAt {
  scope: SecretsScope
  repo: string | null
  dest: Dest
  action: 'add' | 'rotate' | null
  name: string | null
  env: string | null
  runnerKey: string | null
  key: 'typesafe' | null
}

export function parseSecretsView(view: string | null): SecretsAt {
  const q = new URLSearchParams(secretsLinkView(view ?? ''))
  const scope = q.get('scope')
  const action = q.get('action')
  return {
    scope: scope === 'personal' || scope === 'repository' ? scope : 'workspace',
    repo: q.get('repo'),
    dest: q.get('dest') === 'actions' ? 'actions' : 'agents',
    action: action === 'add' || action === 'rotate' ? action : null,
    name: q.get('name'),
    env: q.get('env'),
    runnerKey: q.get('runner_key'),
    key: q.get('key') === 'typesafe' ? 'typesafe' : null,
  }
}

/** The view's query, in `secretsLinkView`'s one spelling. */
function viewOf(at: Partial<SecretsAt>): string {
  const q = new URLSearchParams()
  if (at.scope !== undefined && at.scope !== 'workspace') q.set('scope', at.scope)
  if (at.scope === 'repository' && at.repo) q.set('repo', at.repo)
  if (at.scope === 'repository' && at.repo && at.dest === 'actions') q.set('dest', 'actions')
  if (at.action) q.set('action', at.action)
  if (at.name) q.set('name', at.name)
  if (at.env) q.set('env', at.env)
  if (at.runnerKey) q.set('runner_key', at.runnerKey)
  if (at.key) q.set('key', at.key)
  return secretsLinkView(q.toString())
}

function addressOf(view: string): string {
  return view === '' ? 'config/secrets' : `config/secrets?${view}`
}

/** Move to another view of this page: the router reads the bar, and the page follows. */
function goView(view: string): void {
  window.history.pushState(null, '', addressToPath(addressOf(view)))
  window.dispatchEvent(new PopStateEvent('popstate'))
}

/** An in-app link, routed like the panel's own. */
function AppLink({ address, children }: { address: string; children: ReactNode }) {
  const go = (e: MouseEvent<HTMLAnchorElement>) => {
    if (!routedClick(e)) return
    e.preventDefault()
    window.history.pushState(null, '', addressToPath(address))
    window.dispatchEvent(new PopStateEvent('popstate'))
  }
  return <a className="ctl-link" href={addressToPath(address)} onClick={go}>{children}</a>
}

/** Drop `t` from the address bar, keeping everything else. Idempotent. */
function dropLinkToken(): void {
  const url = new URL(window.location.href)
  if (!url.searchParams.has('t')) return
  url.searchParams.delete('t')
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`)
}

const isForm = (at: SecretsAt) => at.action !== null || at.runnerKey !== null || at.key !== null

const dataOf = <T,>(r: Result<T>): T | null => (r.status === 'ok' || r.status === 'stale' ? r.data : null)

const SCOPE_WORDS: Readonly<Record<SecretsScope, string>> = { workspace: 'the workspace’s', personal: 'yours', repository: 'the repository’s' }

/** A shadow entry in the reader's words: "Yours wins on agents of ana@…". */
function winnerWords(s: SecretsShadow): string {
  const who = s.scope === 'personal' ? 'Yours' : s.scope === 'repository' ? `${s.repository ?? 'the repository'}’s` : 'The workspace’s'
  return s.scope === 'personal' && s.person !== null ? `${s.person}’s wins on their agents` : `${who} wins on ${s.where}`
}

/** The chips beside a row the overview reports shadowing for (§2.4). */
function ShadowNotes({ row }: { row: SecretsRow | undefined }) {
  if (row === undefined || (row.shadowed_by.length === 0 && row.shadows.length === 0)) return null
  const lost = Array.from(new Set(row.shadowed_by.map(winnerWords)))
  const won = Array.from(new Set(row.shadows.map((s) => `overrides ${SCOPE_WORDS[s.scope]}${s.scope === 'workspace' ? '' : ` on ${s.where}`}`)))
  return (
    <>
      {won.map((w) => <span key={w} className="sec-chip is-info">{w}</span>)}
      {lost.map((w) => <span key={w} className="sec-chip is-shadowed">shadowed: {w}</span>)}
    </>
  )
}

/** A time in the reader's words: "in 7 h" ahead, "2d ago" behind. */
function when(iso: string): string {
  const ms = Date.parse(iso)
  if (Number.isNaN(ms)) return iso
  const ahead = ms - Date.now()
  if (ahead <= 0) return timeAgo(iso)
  const h = ahead / 3_600_000
  return h < 1 ? `in ${Math.max(1, Math.round(ahead / 60_000))} min` : h < 48 ? `in ${Math.round(h)} h` : `in ${Math.round(h / 24)} d`
}

/** A GitHub token's kind, as git_tokens records it, in words. */
const TOKEN_KINDS: Readonly<Record<string, string>> = Object.fromEntries([
  ['app_user', 'GitHub App user token'],
  ['app_installation', 'GitHub App installation token'],
  ['pat', 'personal access token'],
  ['fine_grained_pat', 'fine-grained personal access token'],
])

function setWords(row: Pick<SecretsRow, 'set_at' | 'set_by'>): string {
  if (row.set_at === null) return row.set_by === null ? 'set' : `set by ${row.set_by}`
  return row.set_by === null ? `set ${timeAgo(row.set_at)}` : `set ${timeAgo(row.set_at)} by ${row.set_by}`
}

/** Rows that block something until they are set: a QA sign-in, an MCP server's credential. */
function neededOf(rows: readonly SecretsRow[]): SecretsRow[] {
  return rows.filter((r) => r.status === 'unset' && ((r.section === 'keys' && r.store === 'mcp_servers') || (r.section === 'qa_sign_in' && r.field === 'credential')))
}

const OVERVIEW_ROUTE = 'GET /v1/secrets/overview'
const CATALOGUE_ROUTE = 'GET /v1/workspace/secrets/consumers'

export function SecretsScreen({ view, token = null }: { view: string | null; token?: string | null }) {
  const at = parseSecretsView(view)
  const viewKey = viewOf(at)
  // THE LINK'S TOKEN, for the form its link opened and no other: read once,
  // from App's capture or, rendered alone, off the bar. A ref, never state or
  // a prop of anything drawn.
  const link = useRef<{ view: string; token: string | null } | null>(null)
  if (link.current === null) {
    const t = token ?? (typeof window === 'undefined' ? null : secretsLinkToken(window.location.pathname, window.location.search))
    link.current = { view: viewKey, token: isForm(at) || at.dest === 'actions' ? t : null }
  }
  // Which view a link opened, for the form's banner; it outlives the token.
  const linked = useRef(link.current.token === null ? null : link.current.view)
  useLayoutEffect(() => {
    dropLinkToken()
  }, [viewKey])
  const takeLink = (forView: string): string | null => {
    const held = link.current
    if (held === null || held.view !== forView || held.token === null) return null
    const t = held.token
    link.current = { view: held.view, token: null }
    return t
  }

  const repos = useUrRead(loadRepositories, 'secrets-repositories')
  const registered = repos.state.status === 'empty' ? [] : dataOf(repos.state)
  const repoId = at.scope !== 'repository' ? null : at.repo ?? registered?.[0]?.repo_id ?? null
  const known = repoId === null || registered === null ? null : registered.some((r) => r.repo_id === repoId)
  const overviewKey = `secrets-overview:${at.scope}:${repoId ?? ''}`
  const overview = useUrRead(
    () => (at.scope === 'repository' && (repoId === null || known !== true)
      ? Promise.resolve<Result<SecretsOverview>>({ status: 'loading', since: Date.now() })
      : loadSecretsOverview(at.scope, repoId)),
    `${overviewKey}:${known === true ? 'k' : 'u'}`,
  )
  const catalogue = useUrRead(loadWorkspaceSecretCatalogue, 'secrets-catalogue')
  const ov = dataOf(overview.state)
  // Whether the selected workspace is the person's own: their personal
  // workspace's id is the one the personal scope names.
  const personalSelected = ov !== null && ov.personal_tenant_id !== null && ov.personal_tenant_id === ov.tenant_id
  const [draft, setDraft] = useState<Draft | null>(null)
  const [wizard, setWizard] = useState<WizardStart | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const go = (next: Partial<SecretsAt>) => goView(viewOf(next))
  const back = (words: string | null = null) => {
    setDone(words)
    go({ scope: at.scope, repo: repoId, dest: at.dest })
  }

  const canManage = ov?.can_manage === true
  const formOpen = isForm(at)
  return (
    <div className="pk-page sec-page">
      <PageHead title={formOpen ? 'Set a secret' : 'Secrets'}>
        <UrRefresh reads={[overview, repos]} ageOf={[overview]} />
        {!formOpen && (canManage || at.scope === 'personal') && (
          <Button kind="primary" size="sm" onClick={() => setWizard({ scope: at.scope, repo: repoId, consumer: null })}>
            Add a secret
          </Button>
        )}
      </PageHead>
      {formOpen ? (
        <OneTimeForm
          key={viewKey}
          at={at}
          repoId={repoId}
          known={known}
          overview={ov}
          personalSelected={personalSelected}
          catalogue={dataOf(catalogue.state)}
          draft={draft}
          takeLink={() => takeLink(viewKey)}
          fromLink={linked.current === viewKey}
          onBack={() => back(null)}
          onDone={(words) => {
            setDraft(null)
            overview.reload()
            setDone(words)
          }}
          onAnother={() => back(null)}
        />
      ) : (
        <>
          <ScopeBar at={at} repoId={repoId} repos={registered} tenant={ov?.tenant_id ?? null} onScope={(scope) => {
            setDone(null)
            go({ scope, repo: scope === 'repository' ? repoId : null })
          }} onRepo={(repo) => go({ scope: 'repository', repo })} />
          {done !== null && <p className="pk-saved" role="status">{done}</p>}
          {at.dest === 'actions' && at.scope === 'repository' && at.name !== null && (
            <Banner tone="info" title={`Opened from a one-time link for ${at.name}${at.env === null ? '' : ` in ${at.env}`}`}>
              Writing a GitHub Actions secret comes with this repository&rsquo;s GitHub Actions pane, which this console does
              not draw yet. Nothing was written and the link is unused; it expires ten minutes after it was minted.
            </Banner>
          )}
          {at.scope === 'repository' && known === false ? (
            <Banner tone="warn" title="That repository is not registered in this workspace">
              The Repository scope lists only this workspace&rsquo;s registrations. Pick one above, or register it on{' '}
              <AppLink address="config/repositories">Config › Repositories</AppLink>.
            </Banner>
          ) : at.scope === 'repository' && registered !== null && registered.length === 0 ? (
            <Banner tone="info" title="No repository is registered in this workspace yet">
              A repository&rsquo;s secrets are released only to agents that clone it, so they wait for a registration.
              Register one on <AppLink address="config/repositories">Config › Repositories</AppLink>.
            </Banner>
          ) : (
            <UrRegion state={overview.state} route={OVERVIEW_ROUTE} what="This scope's secrets" plural onRetry={overview.reload}>
              {(d) => (
                <ScopeBody
                  at={at}
                  overview={d}
                  repoId={repoId}
                  personalSelected={personalSelected}
                  catalogue={catalogue}
                  onChange={overview.reload}
                  go={go}
                  onAdd={(consumer) => setWizard({ scope: at.scope, repo: repoId, consumer })}
                />
              )}
            </UrRegion>
          )}
          {/* Which backend Jev judges with (JUDGE-SELECT): it sat over the
              TypeSafe key on Settings › API keys and moved here with it, since
              that key is needed only when the backend is TypeSafe. Its own
              read, so it draws whatever the overview answered. */}
          {at.scope === 'workspace' && <JevBackendCard />}
        </>
      )}
      {wizard !== null && (
        <AddWizard
          start={wizard}
          repos={registered ?? []}
          tenant={ov?.tenant_id ?? null}
          personalSelected={at.scope === 'personal' ? personalSelected : null}
          catalogue={dataOf(catalogue.state)}
          onClose={() => setWizard(null)}
          onOpen={(d) => {
            setWizard(null)
            setDraft(d)
            go({ scope: d.scope, repo: d.target.scope === 'repository' ? d.target.repoId : null, action: 'add' })
          }}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// The scope picker
// ---------------------------------------------------------------------------

function ScopeBar({
  at,
  repoId,
  repos,
  tenant,
  onScope,
  onRepo,
}: {
  at: SecretsAt
  repoId: string | null
  repos: RepoRecord[] | null
  tenant: string | null
  onScope: (s: SecretsScope) => void
  onRepo: (repo: string) => void
}) {
  const id = useId()
  return (
    <div className="sec-scope">
      <div className="sec-scope-row">
        <span className="sec-lbl">Scope</span>
        <Segmented<SecretsScope>
          label="Scope"
          value={at.scope}
          onChange={onScope}
          options={[
            { key: 'workspace', label: tenant === null ? 'Workspace' : `Workspace · ${tenant}` },
            { key: 'personal', label: 'Yours' },
            { key: 'repository', label: 'Repository' },
          ]}
        />
        {at.scope === 'repository' && repos !== null && repos.length > 0 && (
          <span className="sec-repo">
            <label htmlFor={`${id}-repo`} className="sec-lbl">Repository</label>
            <select id={`${id}-repo`} value={repoId ?? ''} onChange={(e) => onRepo(e.target.value)}>
              {repoId !== null && !repos.some((r) => r.repo_id === repoId) && <option value={repoId}>not registered here</option>}
              {repos.map((r) => <option key={r.repo_id} value={r.repo_id}>{r.owner}/{r.repo}</option>)}
            </select>
          </span>
        )}
      </div>
      <p className="sec-hint">Same name in several scopes: yours wins, then the repository&rsquo;s, then the workspace&rsquo;s.</p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// One scope's sections, in §6.1's order
// ---------------------------------------------------------------------------

function ScopeBody({
  at,
  overview,
  repoId,
  personalSelected,
  catalogue,
  onChange,
  go,
  onAdd,
}: {
  at: SecretsAt
  overview: SecretsOverview
  repoId: string | null
  personalSelected: boolean
  catalogue: UrRead<WorkspaceSecretCatalogue>
  onChange: () => void
  go: (next: Partial<SecretsAt>) => void
  onAdd: (consumer: string | null) => void
}) {
  const rows = overview.rows
  const of = (section: string) => rows.filter((r) => r.section === section)
  const needed = neededOf(rows)
  if (at.scope === 'repository' && repoId !== null) {
    const repository = overview.repository ?? 'this repository'
    return (
      <>
        <NeedLead rows={needed} manages={overview.can_manage === true} scope="repository" go={go} onAdd={onAdd} />
        <div className="sec-dest">
          <Segmented<Dest>
            label="Destination"
            value={at.dest}
            onChange={(dest) => go({ scope: 'repository', repo: repoId, dest })}
            options={[{ key: 'agents', label: 'Agents' }, { key: 'actions', label: 'GitHub Actions' }]}
          />
          <span className="sec-hint">
            {at.dest === 'actions'
              ? 'Written into the repository on GitHub. SwarmCloud reads names and update times, never values.'
              : `Released only to agents that clone ${repository}.`}
          </span>
        </div>
        {at.dest === 'actions' ? (
          <ActionsRecord rows={of('actions')} repository={repository} />
        ) : (
          <>
            <StoreCard
              title="For agents"
              label="Repository secrets"
              target={{ scope: 'repository', repoId }}
              overview={overview}
              catalogue={catalogue}
              onChange={onChange}
              go={go}
              at={at}
              hint={overview.can_manage === true
                ? 'You may set these as a workspace admin. A member whom GitHub calls admin of this repository may too.'
                : 'A workspace admin sets these, or a member whom GitHub calls admin of this repository.'}
            />
            <div className="sec-g2">
              <QaCard rows={of('qa_sign_in')} />
              <GitCard rows={of('github')} empty="No repository token. Agents for this repository use the workspace’s token (repository first, then workspace)." />
            </div>
          </>
        )}
      </>
    )
  }
  if (at.scope === 'personal') {
    return (
      <>
        <div className="sec-lead is-ok">
          <span className="sec-k">Yours</span>
          <b>Your secrets win over the workspace&rsquo;s and a repository&rsquo;s, for your own agents</b>
          <span>Nothing you set here is read by anyone else&rsquo;s agents.</span>
        </div>
        {personalSelected ? (
          <StoreCard title="For your agents" label="Your secrets" target={{ scope: 'workspace' }} overview={overview} catalogue={catalogue} onChange={onChange} go={go} at={at} />
        ) : (
          <YoursList rows={of('agents')} personal={overview.personal_tenant_id} />
        )}
        <BrowserCard rows={of('browser_sessions')} removable={personalSelected} onChange={onChange} />
        <KeysCard rows={of('keys')} scope="personal" go={go} onChange={onChange} onAdd={onAdd} />
        <GitCard rows={of('github')} empty="No GitHub user token of yours is registered." />
      </>
    )
  }
  return (
    <>
      <NeedLead rows={needed} manages={overview.can_manage === true} scope="workspace" go={go} onAdd={onAdd} />
      <StoreCard title="For agents" label="Workspace secrets" target={{ scope: 'workspace' }} overview={overview} catalogue={catalogue} onChange={onChange} go={go} at={at} empty={<FirstSecret tenant={overview.tenant_id} />} />
      <div className="sec-g2">
        <KeysCard rows={[...of('keys'), ...of('accounts')]} scope="workspace" go={go} onChange={onChange} onAdd={onAdd} />
        <GitCard rows={of('github')} empty="No workspace token is registered. Connect one on GitHub access." />
      </div>
    </>
  )
}

/** The page's next action (S4a, S4h): what is needed and not set, and who sets it. */
function NeedLead({ rows, manages, scope, go, onAdd }: { rows: SecretsRow[]; manages: boolean; scope: SecretsScope; go: (n: Partial<SecretsAt>) => void; onAdd: (c: string | null) => void }) {
  if (rows.length === 0) return null
  const name = (r: SecretsRow) => (r.section === 'qa_sign_in' ? `the QA sign-in for ${r.name} ${r.environment ?? ''}`.trim() : `MCP server ${r.name}’s credential`)
  const n = rows.length
  return (
    <section className="sec-lead is-warn" aria-label="Needed and not set">
      <span className="sec-k">{manages ? `${n} needed` : 'an admin’s step'}</span>
      <b>
        {n === 1 ? `${name(rows[0]!).replace(/^./, (c) => c.toUpperCase())} is not set` : `${n} secrets are needed and not set`}
      </b>
      <span>
        {n === 1 ? '' : `${rows.map(name).join('; ')}. `}
        {manages
          ? (n === 1 ? (rows[0]!.section === 'qa_sign_in' ? 'QA runs there stay off until it is set.' : `Every call to ${rows[0]!.name} fails until a secret is granted to ${rows[0]!.consumer ?? `mcp:${rows[0]!.name}`}.`) : 'Each stays off until it is set.')
          : 'A workspace admin sets them. You can set your own under Yours; yours win for your agents.'}
      </span>
      <span className="sec-acts">
        {manages ? rows.slice(0, 2).map((r) => r.section === 'qa_sign_in' ? (
          <AppLink key={`${r.name}-${r.environment}`} address="config/repositories?page=qa-targets">Set it on QA targets</AppLink>
        ) : (
          <Button key={r.name} kind="primary" size="sm" onClick={() => onAdd(r.consumer)}>Add a secret for {r.name}</Button>
        )) : scope !== 'personal' && (
          <Button kind="primary" size="sm" onClick={() => go({ scope: 'personal' })}>Go to Yours</Button>
        )}
      </span>
    </section>
  )
}

/** The empty workspace (S4i): nothing stored, and what people usually set first. */
function FirstSecret({ tenant }: { tenant: string }) {
  return (
    <>
      <p className="pk-lead"><b>No secret is stored for {tenant || 'this workspace'} yet.</b> Usually first:</p>
      <ul className="sec-first">
        <li><b>The TypeSafe key</b><span>Jev and browser agents read it. Set it under Keys for agents and MCP.</span></li>
        <li><b>A search API key</b><span>For agents that search the web. Add a secret, then grant it.</span></li>
        <li><b>A QA sign-in</b><span>Set on a repository, once a QA target is registered.</span></li>
      </ul>
    </>
  )
}

/**
 * ONE STORE'S SECRETS: the workspace's own, or one repository's. The list
 * reads the store (versions, last use and the grants the matrix needs); the
 * overview's rows say which scope wins each name.
 */
function StoreCard({
  title,
  label,
  target,
  overview,
  catalogue,
  onChange,
  go,
  at,
  hint,
  empty,
}: {
  title: string
  label: string
  target: SecretStoreTarget
  overview: SecretsOverview
  catalogue: UrRead<WorkspaceSecretCatalogue>
  onChange: () => void
  go: (n: Partial<SecretsAt>) => void
  at: SecretsAt
  hint?: string
  /** What a manager is shown in place of an empty list (S4i). */
  empty?: ReactNode
}) {
  const store = secretStore(target)
  const key = target.scope === 'workspace' ? 'store:workspace' : `store:${target.repoId}`
  const list = useUrRead(store.list, key)
  const [matrix, setMatrix] = useState(false)
  const rowOf = (name: string) => overview.rows.find((r) => r.section === 'agents' && r.name === name)
  const data = dataOf(list.state)
  const manages = data?.can_manage === true
  const reload = () => {
    list.reload()
    onChange()
  }
  const blank = data !== null && data.secrets.length === 0 && manages && empty !== undefined
  return (
    <Card
      title={title}
      className={blank ? 'sec-dash' : undefined}
      action={
        <span className="sec-acts">
          {data !== null && !manages && <span className="sec-chip">read only for members</span>}
          {!blank && (
            <Button kind="ghost" size="sm" aria-expanded={matrix} onClick={() => setMatrix((m) => !m)}>
              {matrix ? 'Hide the grant matrix' : 'Open the grant matrix'}
            </Button>
          )}
        </span>
      }
    >
      {blank ? empty : (
        <p className="pk-lead">
          Released to the profiles, tools and components in Granted to. A value is stored write-only: no page and no API
          route shows it, its length or any of its characters again.
        </p>
      )}
      {!blank && <UrRegion state={list.state} route={target.scope === 'workspace' ? 'GET /v1/workspace/secrets' : 'GET /v1/repositories/{repo_id}/secrets'} what={label} plural onRetry={list.reload}>
        {(d) => (
          <SecretList
            data={d}
            store={store}
            label={label}
            onChange={reload}
            onRotate={(name) => go({ scope: at.scope, repo: target.scope === 'repository' ? target.repoId : null, action: 'rotate', name })}
            note={(name) => <ShadowNotes row={rowOf(name)} />}
          />
        )}
      </UrRegion>}
      {hint !== undefined && <p className="sec-hint">{hint}</p>}
      {matrix && data !== null && (
        <UrRegion state={catalogue.state} route={CATALOGUE_ROUTE} what="The consumers a secret may be granted to" plural onRetry={catalogue.reload}>
          {(c) => <GrantMatrix data={data} store={store} catalogue={c} onChange={reload} />}
        </UrRegion>
      )}
    </Card>
  )
}

/** Yours, from a group workspace: read here, managed in the person's own workspace. */
function YoursList({ rows, personal }: { rows: SecretsRow[]; personal: string | null }) {
  const open = () => {
    if (personal === null) return
    chooseTenant(personal)
    goView('')
  }
  return (
    <Card title="For your agents">
      {rows.length === 0 ? (
        <p className="ur-small">You have no secret of your own yet.</p>
      ) : (
        <table className="mcp-servers" aria-label="Your secrets">
          <thead>
            <tr><th scope="col">Name</th><th scope="col">Kind</th><th scope="col">Set</th><th scope="col">Used here</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.name}>
                <th scope="row" data-label="Name"><code>{r.name}</code><span className="sec-notes"><ShadowNotes row={r} /></span></th>
                <td data-label="Kind">{r.kind === null ? 'not said' : (KIND_WORDS[r.kind] ?? r.kind)}</td>
                <td data-label="Set">{setWords(r)}</td>
                <td data-label="Used here">{r.opted_in === true ? 'yes, opted in' : r.opted_in === false ? 'no' : 'not said'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="sec-hint">
        Your secrets live in your personal workspace: add, rotate and opt them in to a workspace there.
      </p>
      {personal === null ? (
        <p className="ur-small">You have no personal workspace yet.</p>
      ) : (
        <span className="sec-acts">
          <Button kind="secondary" size="sm" onClick={open}>Open your personal workspace</Button>
        </span>
      )}
    </Card>
  )
}

function BrowserCard({ rows, removable, onChange }: { rows: SecretsRow[]; removable: boolean; onChange: () => void }) {
  const [removing, setRemoving] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  async function remove(name: string) {
    setBusy(true)
    setFailed(null)
    const r = await deleteBrowserLogin(name)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(`${name} was not removed: ${r.error.message}`)
      return
    }
    setRemoving(null)
    onChange()
  }
  return (
    <Card title="Browser sessions">
      {rows.length === 0 ? (
        <p className="ur-small">No browser session is saved. One is saved when a browser agent asks you to sign in.</p>
      ) : (
        <table className="mcp-servers" aria-label="Browser sessions">
          <thead>
            <tr><th scope="col">Session</th><th scope="col">Saved</th><th scope="col">Expires</th>{removable && <th scope="col" aria-label="Actions" />}</tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.name}>
                <th scope="row" data-label="Session"><code>{r.name}</code>{r.site !== null && <span className="sec-notes"><span className="sec-chip">{r.site}</span></span>}</th>
                <td data-label="Saved">{r.set_at === null ? 'not said' : timeAgo(r.set_at)}</td>
                <td data-label="Expires" title={r.expires_at ?? undefined}>{r.expires_at === null ? 'not said' : when(r.expires_at)}</td>
                {removable && (
                  <td data-label="Actions">
                    <Button kind="danger" size="sm" aria-label={`Remove ${r.name}`} onClick={() => setRemoving(r.name)}>Remove</Button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="sec-hint">Saved when a browser agent asks you to sign in. Only your agents reuse it.</p>
      {removing !== null && (
        <TypedConfirm title={`Remove ${removing}`} name={removing} verb="Remove" keep="Keep it" busy={busy ? 'Removing…' : false}
          error={failed} onConfirm={() => void remove(removing)} onClose={() => setRemoving(null)}>
          <p>Your agents will be asked to sign in again the next time they need this site.</p>
        </TypedConfirm>
      )}
    </Card>
  )
}

/** Keys for agents and MCP (Settings › API keys until SSC-UI; BGD 2026-10-23 23:57Z). */
function KeysCard({ rows, scope, go, onChange, onAdd }: { rows: SecretsRow[]; scope: SecretsScope; go: (n: Partial<SecretsAt>) => void; onChange: () => void; onAdd: (c: string | null) => void }) {
  const [deleting, setDeleting] = useState<SecretsRow | null>(null)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const keyScope = scope === 'personal' ? 'user' : 'tenant'
  async function remove(r: SecretsRow) {
    setBusy(true)
    setFailed(null)
    const res = await deleteProviderKey(r.name, keyScope)
    setBusy(false)
    if (res.status === 'error') {
      setFailed(`The key was not removed: ${res.error.message}`)
      return
    }
    setDeleting(null)
    onChange()
  }
  const line = (r: SecretsRow): { key: ReactNode; reads: string; state: ReactNode; act: ReactNode } => {
    if (r.store === 'provider_keys') {
      const label = r.name === 'typesafe' ? 'TypeSafe key' : `${r.name} key`
      return {
        key: <>{label}<small>{scope === 'personal' ? 'yours' : 'workspace'}</small></>,
        reads: scope === 'personal' ? 'your own submissions' : 'Jev and browser agents',
        state: r.status === 'set' ? <span className="sec-state is-ok">{setWords(r)}</span> : <span className="sec-chip is-warn">not set</span>,
        act: r.can_manage ? (
          <span className="sec-acts">
            <Button kind={r.status === 'set' ? 'secondary' : 'primary'} size="sm" aria-label={`${r.status === 'set' ? 'Replace' : 'Set'} the ${label}`}
              onClick={() => go({ scope, key: 'typesafe' })}>{r.status === 'set' ? 'Replace' : 'Set'}</Button>
            {r.status === 'set' && <Button kind="danger" size="sm" aria-label={`Delete the ${label}`} onClick={() => setDeleting(r)}>Delete</Button>}
          </span>
        ) : null,
      }
    }
    if (r.store === 'runner_provider_keys') {
      return {
        key: <>{r.name} runner key<small>workspace</small></>,
        reads: r.consumers.length === 0 ? 'no profile today' : r.consumers.map((c) => c.replace(/^profile:/, '')).join(', '),
        state: <span className="sec-state is-ok">set</span>,
        act: r.can_manage ? <Button kind="secondary" size="sm" aria-label={`Replace the ${r.name} runner key`} onClick={() => go({ scope: 'workspace', runnerKey: r.name })}>Replace</Button> : null,
      }
    }
    if (r.store === 'mcp_servers') {
      return {
        key: <>MCP server {r.name}</>,
        reads: r.consumer ?? `mcp:${r.name}`,
        state: r.status === 'set'
          ? <span className="sec-state is-ok">reads {r.secrets.join(', ')}</span>
          : <span className="sec-chip is-warn">not set</span>,
        act: r.can_manage && r.status !== 'set' ? <Button kind="primary" size="sm" onClick={() => onAdd(r.consumer)}>Add one</Button> : null,
      }
    }
    return {
      key: <>Subscription accounts</>,
      reads: 'the quota broker',
      state: <span className="sec-state">managed elsewhere</span>,
      act: <AppLink address="capacity/accounts">Capacity › Accounts</AppLink>,
    }
  }
  return (
    <Card title={scope === 'personal' ? 'Keys' : 'Keys for agents and MCP'}>
      {rows.length === 0 ? (
        <p className="ur-small">No key is listed here.</p>
      ) : (
        <table className="mcp-servers" aria-label="Keys for agents and MCP">
          <thead><tr><th scope="col">Key</th><th scope="col">Read by</th><th scope="col">State</th><th scope="col" aria-label="Actions" /></tr></thead>
          <tbody>
            {rows.map((r) => {
              const l = line(r)
              return (
                <tr key={`${r.store}-${r.name}`} className={r.status === 'unset' ? 'is-need' : undefined}>
                  <th scope="row" data-label="Key" className="sec-key">{l.key}</th>
                  <td data-label="Read by">{l.reads}</td>
                  <td data-label="State">{l.state}</td>
                  <td data-label="Actions">{l.act}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      <p className="sec-hint">Was Settings › API keys. Set and Replace open the one-time form; no value is shown again.</p>
      {deleting !== null && (
        <TypedConfirm title="Delete the TypeSafe key" name={deleting.name} verb="Delete" keep="Keep it" busy={busy ? 'Deleting…' : false}
          error={failed} onConfirm={() => void remove(deleting)} onClose={() => setDeleting(null)}>
          <p>Every version is destroyed. {scope === 'personal' ? 'Your submissions use the workspace’s key, if it has one.' : 'Jev and browser agents of this workspace lose it.'}</p>
        </TypedConfirm>
      )}
    </Card>
  )
}

/** GitHub token status (§4): kind, state, login, verified, expires. Never any character of it. */
function GitCard({ rows, empty }: { rows: SecretsRow[]; empty: string }) {
  return (
    <Card title="GitHub token">
      {rows.length === 0 ? <p className="ur-small">{empty}</p> : rows.map((r) => (
        <dl key={r.name} className="sec-kv">
          <dt>Kind</dt><dd>{r.kind === null ? 'not said' : (TOKEN_KINDS[r.kind] ?? r.kind)}{r.token_scope === null ? '' : ` · ${r.token_scope === 'tenant' ? 'workspace' : r.token_scope === 'user' ? 'yours' : r.token_scope}`}</dd>
          <dt>State</dt><dd><span className={`sec-state${r.status === 'active' ? ' is-ok' : ''}`}>{r.status}</span></dd>
          <dt>GitHub login</dt><dd>{r.forge_login === null ? 'not said' : `@${r.forge_login}`}</dd>
          <dt>Verified</dt><dd>{r.verified_at === null ? 'never' : timeAgo(r.verified_at)}</dd>
          <dt>Expires</dt><dd title={r.expires_at ?? undefined}>{r.expires_at === null ? 'not said' : when(r.expires_at)}</dd>
        </dl>
      ))}
      <p className="sec-hint">
        Status only: no part of a token is ever shown. Connect or disconnect on <AppLink address="config/github">GitHub access</AppLink>.
      </p>
    </Card>
  )
}

function QaCard({ rows }: { rows: SecretsRow[] }) {
  return (
    <Card title="Sign-in for QA">
      {rows.length === 0 ? (
        <p className="ur-small">No QA target of this repository holds a sign-in.</p>
      ) : (
        <table className="mcp-servers" aria-label="Sign-in for QA">
          <thead><tr><th scope="col">Target · environment</th><th scope="col">State</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.target_id}-${r.environment}-${r.field}`} className={r.status === 'unset' ? 'is-need' : undefined}>
                <th scope="row" data-label="Target" className="sec-key">
                  {r.name} · {r.environment ?? 'default'}
                  {r.field === 'challenge_bypass' && <small>challenge bypass</small>}
                </th>
                <td data-label="State">
                  {r.status === 'set' ? <span className="sec-state is-ok">{setWords(r)}</span> : <span className="sec-chip is-warn">needed, not set</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="sec-hint">Set and replace on <AppLink address="config/repositories?page=qa-targets">QA targets</AppLink>, from a one-time link.</p>
    </Card>
  )
}

/** SwarmCloud's record of the Actions secrets it wrote (§3.8). The pane itself is SSC-UI-ACTIONS'. */
function ActionsRecord({ rows, repository }: { rows: SecretsRow[]; repository: string }) {
  return (
    <Card title="Written by SwarmCloud">
      {rows.length === 0 ? (
        <p className="ur-small">SwarmCloud has written no GitHub Actions secret into {repository}.</p>
      ) : (
        <table className="mcp-servers" aria-label="GitHub Actions secrets SwarmCloud wrote">
          <thead><tr><th scope="col">Name</th><th scope="col">Environment</th><th scope="col">Updated</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.environment ?? ''}-${r.name}`}>
                <th scope="row" data-label="Name"><code>{r.name}</code></th>
                <td data-label="Environment">{r.environment ?? 'repository level'}</td>
                <td data-label="Updated">{setWords(r)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="sec-hint">
        Names and times only. GitHub&rsquo;s own list, environments and their branch rules, Set and Delete arrive with
        this repository&rsquo;s GitHub Actions pane.
      </p>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Add a secret: the four-step side sheet (S4c)
// ---------------------------------------------------------------------------

interface WizardStart {
  scope: SecretsScope
  repo: string | null
  /** A consumer to tick at step 3 (an MCP server that needs one). */
  consumer: string | null
}

/** What the wizard hands the one-time form: where, and the name, kind and grants chosen. */
export interface Draft extends AddFixed {
  scope: SecretsScope
  target: SecretStoreTarget
  consumers: string[]
}

const STEPS = ['Scope', 'Destination', 'Who reads it', 'One-time form'] as const
const NAME_RULE = /^[a-z0-9][a-z0-9-]{0,63}$/

function AddWizard({
  start,
  repos,
  tenant,
  personalSelected,
  catalogue,
  onClose,
  onOpen,
}: {
  start: WizardStart
  repos: RepoRecord[]
  tenant: string | null
  /** Whether the selected workspace is the person's own; null when not yet known. */
  personalSelected: boolean | null
  catalogue: WorkspaceSecretCatalogue | null
  onClose: () => void
  onOpen: (d: Draft) => void
}) {
  const id = useId()
  const [step, setStep] = useState(1)
  const [scope, setScope] = useState<SecretsScope>(start.scope)
  const [repo, setRepo] = useState<string | null>(start.repo ?? repos[0]?.repo_id ?? null)
  const [name, setName] = useState('')
  const providers = catalogue?.providers.length ? catalogue.providers : [DEFAULT_PROVIDER]
  const kinds = catalogue?.kinds.length ? catalogue.kinds : [DEFAULT_KIND]
  const [provider, setProvider] = useState(providers.includes(DEFAULT_PROVIDER) ? DEFAULT_PROVIDER : providers[0]!)
  const [kind, setKind] = useState(kinds.includes(DEFAULT_KIND) ? DEFAULT_KIND : kinds[0]!)
  const [picked, setPicked] = useState<readonly string[]>(start.consumer === null ? [] : [start.consumer])
  // A personal secret is written in the person's own workspace and nowhere
  // else: from a group workspace the form would write the GROUP's store.
  const [personalHere, setPersonalHere] = useState<boolean | null>(personalSelected)
  useEffect(() => {
    if (scope !== 'personal' || personalHere !== null) return
    let live = true
    void loadSecretsOverview('personal').then((r) => {
      const d = dataOf(r)
      if (live) setPersonalHere(d !== null && d.personal_tenant_id !== null && d.personal_tenant_id === d.tenant_id)
    })
    return () => {
      live = false
    }
  }, [scope, personalHere])
  const repoName = repos.find((r) => r.repo_id === repo)
  const where = scope === 'repository' ? (repoName ? `${repoName.owner}/${repoName.repo}` : 'a repository') : scope === 'personal' ? 'yours' : `the workspace${tenant === null ? '' : ` ${tenant}`}`
  const consumers: WorkspaceSecretConsumer[] = (catalogue?.consumers ?? []).filter((c) => GROUPS.some(([k]) => k === c.kind) || c.kind === 'mcp')
  const fits = consumers.map((c) => ({ c, cell: cellOf({ kind }, c) })).filter((x) => x.cell !== null)
  const ok1 = scope !== 'repository' || repo !== null
  const ok3 = NAME_RULE.test(name)
  const wrongScope = scope === 'personal' && personalHere !== true
  const open = () => {
    if (!ok1 || !ok3 || wrongScope) return
    const target: SecretStoreTarget = scope === 'repository' && repo !== null ? { scope: 'repository', repoId: repo } : { scope: 'workspace' }
    onOpen({ scope, target, name, provider, kind, consumers: picked.filter((p) => fits.some((f) => f.c.consumer === p && f.cell!.open)) })
  }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="sec-scrim" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="sec-sheet" role="dialog" aria-modal="true" aria-labelledby={`${id}-h`}>
        <div className="sec-sheet-h">
          <h2 id={`${id}-h`}>Add a secret</h2>
          <Button kind="ghost" size="sm" aria-label="Close" onClick={onClose}>Close</Button>
        </div>
        <ol className="sec-steps" aria-label="Steps">
          {STEPS.map((s, i) => (
            <li key={s} className={i + 1 < step ? 'is-done' : i + 1 === step ? 'is-on' : ''} aria-current={i + 1 === step ? 'step' : undefined}>
              <i aria-hidden="true">{i + 1 < step ? '✓' : i + 1}</i>{s}
            </li>
          ))}
        </ol>
        {step === 1 && (
          <fieldset className="sec-opts">
            <legend>Where does it live?</legend>
            <Opt name={`${id}-scope`} on={scope === 'workspace'} title={tenant === null ? 'Workspace' : `Workspace · ${tenant}`} why="Any member’s agents may be granted it. Set by a workspace admin." onPick={() => setScope('workspace')} />
            <Opt name={`${id}-scope`} on={scope === 'personal'} title="Yours" why="Only your own submissions read it, and it wins over the others." onPick={() => setScope('personal')} />
            <Opt name={`${id}-scope`} on={scope === 'repository'} title="Repository" why="Only for one repository, and released only to agents that clone it." onPick={() => setScope('repository')} disabled={repos.length === 0 ? 'No repository is registered in this workspace yet.' : undefined} />
            {scope === 'repository' && repos.length > 0 && (
              <span className="sec-field">
                <label htmlFor={`${id}-repo`}>Repository</label>
                <select id={`${id}-repo`} value={repo ?? ''} onChange={(e) => setRepo(e.target.value)}>
                  {repos.map((r) => <option key={r.repo_id} value={r.repo_id}>{r.owner}/{r.repo}</option>)}
                </select>
              </span>
            )}
          </fieldset>
        )}
        {step === 2 && (
          <fieldset className="sec-opts">
            <legend>Who is it for?</legend>
            <Opt name={`${id}-dest`} on title="Agents" why={scope === 'repository' ? `Agents that clone ${where}.` : 'Agents, tools and components it is granted to.'} onPick={() => undefined} />
            {scope === 'repository' && (
              <Opt name={`${id}-dest`} on={false} title="GitHub Actions" why="Workflows in the repository, on GitHub."
                disabled="Written from this repository’s GitHub Actions pane, which this console does not draw yet." onPick={() => undefined} />
            )}
            {scope !== 'repository' && <p className="sec-hint">A GitHub Actions secret lives on one repository: pick Repository at step 1 for one.</p>}
          </fieldset>
        )}
        {step === 3 && (
          <div className="sec-opts">
            <span className="sec-field">
              <label htmlFor={`${id}-name`}>Name</label>
              <input id={`${id}-name`} className="ur-search" value={name} maxLength={64} spellCheck={false} autoCapitalize="off" autoCorrect="off"
                placeholder="search-api" aria-describedby={`${id}-name-why`} onChange={(e) => setName(e.target.value.trim())} />
              <span id={`${id}-name-why`} className="sec-hint">Lower-case letters, digits and hyphens, starting with a letter or a digit. Never the value.</span>
            </span>
            <span className="sec-field-row">
              <span className="sec-field">
                <label htmlFor={`${id}-provider`}>Provider</label>
                <select id={`${id}-provider`} value={provider} onChange={(e) => setProvider(e.target.value)}>
                  {providers.map((p) => <option key={p} value={p}>{p}</option>)}
                </select>
              </span>
              <span className="sec-field">
                <label htmlFor={`${id}-kind`}>Kind</label>
                <select id={`${id}-kind`} value={kind} onChange={(e) => setKind(e.target.value)}>
                  {kinds.map((k) => <option key={k} value={k}>{KIND_WORDS[k] ?? k}</option>)}
                </select>
              </span>
            </span>
            <fieldset className="sec-grants">
              <legend>Granted to</legend>
              {fits.length === 0 ? <p className="sec-hint">The catalogue was not read, so grant it from the matrix after it is set.</p> : fits.map(({ c, cell }) => (
                <label key={c.consumer} className={`sec-grant${cell!.open ? '' : ' is-off'}`}>
                  <input type="checkbox" disabled={!cell!.open} checked={picked.includes(c.consumer)}
                    onChange={(e) => setPicked((p) => (e.target.checked ? [...p, c.consumer] : p.filter((x) => x !== c.consumer)))} />
                  <code>{c.consumer}</code>
                  {cell!.reason !== null && <span className="sec-hint">{cell!.reason}</span>}
                </label>
              ))}
            </fieldset>
          </div>
        )}
        {step === 4 && (
          <div className="sec-opts">
            <p className="sec-big">Set the value in a one-time form</p>
            {wrongScope ? (
              <Banner tone="warn" title="Yours is set in your personal workspace">
                {personalHere === null
                  ? 'Reading which workspace is yours…'
                  : 'A form opened here would write this workspace’s store, not yours. Switch to your personal workspace and add it there.'}
              </Banner>
            ) : (
              <p className="sec-hint">
                Opens a form for <b>{name}</b> in {where}. It works once, for ten minutes. The value is stored write-only and
                never shown again{picked.length > 0 ? `; then ${picked.length === 1 ? picked[0] : `${picked.length} consumers`} ${picked.length === 1 ? 'is' : 'are'} granted it` : ''}.
              </p>
            )}
            <div className="c-banner is-info sec-term">
              <span>From a terminal instead: <code>/sc secrets link {scope === 'personal' ? 'personal' : scope}{name === '' ? '' : ` ${name}`}{scope === 'repository' && repoName ? ` --repo ${repoName.owner}/${repoName.repo}` : ''}</code></span>
            </div>
          </div>
        )}
        <div className="sec-sheet-f">
          {step > 1 ? <Button kind="ghost" size="sm" onClick={() => setStep(step - 1)}>Back</Button> : <Button kind="ghost" size="sm" onClick={onClose}>Cancel</Button>}
          {step < 4 ? (
            <Button kind="primary" size="sm" disabled={(step === 1 && !ok1) || (step === 3 && !ok3)} onClick={() => setStep(step + 1)}>Next</Button>
          ) : (
            <Button kind="primary" size="sm" disabled={!ok1 || !ok3 || wrongScope} onClick={open}>Open the one-time form</Button>
          )}
        </div>
      </div>
    </div>
  )
}

function Opt({ name, on, title, why, onPick, disabled }: { name: string; on: boolean; title: string; why: string; onPick: () => void; disabled?: string }) {
  return (
    <label className={`sec-opt${on ? ' is-on' : ''}${disabled === undefined ? '' : ' is-off'}`}>
      <input type="radio" name={name} checked={on} disabled={disabled !== undefined} onChange={onPick} />
      <span><b>{title}</b><small>{disabled ?? why}</small></span>
    </label>
  )
}

// ---------------------------------------------------------------------------
// The one-time form (S4d, S4g)
// ---------------------------------------------------------------------------

type Minted = { status: 'opening' } | { status: 'open' } | { status: 'refused'; why: string }

/**
 * THE FORM HOLDS ONE TOKEN. A tool's link brings one (`takeLink`); otherwise
 * the form mints its own as it opens, from the store its view names, and the
 * write it opens is authorised again when it is submitted. Never drawn.
 */
function useFormLink(takeLink: () => string | null, mint: () => Promise<Result<MintedLink>>): [Minted, () => string | null] {
  const held = useRef<string | null>(null)
  const [state, setState] = useState<Minted>({ status: 'opening' })
  useEffect(() => {
    let live = true
    const given = takeLink()
    if (given !== null) {
      held.current = given
      setState({ status: 'open' })
      return
    }
    void mint().then((r) => {
      if (!live) return
      if (r.status === 'ok' || r.status === 'stale') {
        held.current = r.data.token
        setState({ status: 'open' })
      } else {
        setState({ status: 'refused', why: r.status === 'error' ? r.error.message : 'the link was not minted' })
      }
    })
    return () => {
      live = false
    }
    // Once per form: a form is keyed by its view.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const take = () => {
    const t = held.current
    held.current = null
    return t
  }
  return [state, take]
}

function OneTimeForm({
  at,
  repoId,
  known,
  overview,
  personalSelected,
  catalogue,
  draft,
  takeLink,
  fromLink,
  onBack,
  onDone,
  onAnother,
}: {
  at: SecretsAt
  repoId: string | null
  known: boolean | null
  overview: SecretsOverview | null
  personalSelected: boolean
  catalogue: WorkspaceSecretCatalogue | null
  draft: Draft | null
  takeLink: () => string | null
  /** Opened from a link a tool minted, rather than from a button here. */
  fromLink: boolean
  onBack: () => void
  onDone: (words: string) => void
  onAnother: () => void
}) {
  const [written, setWritten] = useState<string | null>(null)
  const done = (words: string) => {
    setWritten(words)
    onDone(words)
  }
  const after = (
    <Card title={written === null ? 'After you press Set' : 'Written'}>
      {written === null ? (
        <>
          <p className="pk-lead">The value is written write-only, the link is used up, and the value is not shown again, here or anywhere.</p>
          <p className="pk-lead"><b>If the link was used or is older than ten minutes</b>, the form says so and nothing is written.</p>
        </>
      ) : (
        <>
          <p className="pk-saved" role="status">{written}</p>
          <p className="pk-lead">The link is used up. The value is not shown again, here or anywhere.</p>
          <Button kind="secondary" size="sm" onClick={onAnother}>Back to Secrets</Button>
        </>
      )}
    </Card>
  )
  let body: ReactNode
  if (at.runnerKey !== null) {
    body = <RunnerKeyForm provider={at.runnerKey} takeLink={takeLink} onDone={done} written={written !== null} />
  } else if (at.key === 'typesafe') {
    body = <TypeSafeForm scope={at.scope === 'personal' ? 'user' : 'tenant'} takeLink={takeLink} onDone={done} written={written !== null} />
  } else if (at.scope === 'personal' && overview !== null && !personalSelected) {
    body = (
      <Banner tone="warn" title="Yours is set in your personal workspace">
        This form would write the selected workspace&rsquo;s store, not yours, so it is not offered here. Switch to your
        personal workspace and open it there.
      </Banner>
    )
  } else if (at.scope === 'personal' && overview === null) {
    body = <p className="ur-small">Reading which workspace is yours…</p>
  } else if (at.scope === 'repository' && (repoId === null || known !== true)) {
    body = known === false
      ? <Banner tone="warn" title="That repository is not registered in this workspace">Nothing can be set for it here.</Banner>
      : <p className="ur-small">Reading this workspace&rsquo;s repositories…</p>
  } else {
    const target: SecretStoreTarget = at.scope === 'repository' && repoId !== null ? { scope: 'repository', repoId } : { scope: 'workspace' }
    body = (
      <StoreForm
        at={at}
        target={target}
        catalogue={catalogue}
        draft={draft !== null && sameTarget(draft.target, target) ? draft : null}
        repository={at.scope === 'repository' ? overview?.repository ?? null : null}
        takeLink={takeLink}
        onBack={onBack}
        onDone={done}
        written={written !== null}
      />
    )
  }
  return (
    <>
      <Banner tone="info" title={fromLink ? 'Opened from a one-time link' : 'A one-time form'}>
        Paste the value into the form below: the /sc command and this page&rsquo;s address never see it. Its link works
        once, for ten minutes.
      </Banner>
      <div className="sec-g21">
        <div className="sec-col">{body}</div>
        <div className="sec-col">{after}</div>
      </div>
    </>
  )
}

function sameTarget(a: SecretStoreTarget, b: SecretStoreTarget): boolean {
  return a.scope === b.scope && (a.scope === 'workspace' || (b.scope === 'repository' && a.repoId === b.repoId))
}

function Opening({ state, onBack }: { state: Minted; onBack?: () => void }) {
  if (state.status === 'opening') return <p className="ur-small">Opening the one-time form…</p>
  if (state.status === 'refused') {
    return (
      <Banner tone="bad" title={`The one-time form could not be opened: ${state.why}`} role="alert"
        actions={onBack === undefined ? undefined : <Button kind="secondary" size="sm" onClick={onBack}>Back</Button>}>
        Nothing was written.
      </Banner>
    )
  }
  return null
}

function whereWords(at: SecretsAt, target: SecretStoreTarget, repository: string | null): string {
  return target.scope === 'repository' ? `${repository ?? 'this repository'}, for agents that clone it` : at.scope === 'personal' ? 'yours' : 'the workspace'
}

function StoreForm({
  at,
  target,
  catalogue,
  draft,
  takeLink,
  onBack,
  onDone,
  written,
  repository,
}: {
  at: SecretsAt
  target: SecretStoreTarget
  catalogue: WorkspaceSecretCatalogue | null
  draft: Draft | null
  repository: string | null
  takeLink: () => string | null
  onBack: () => void
  onDone: (words: string) => void
  written: boolean
}) {
  const store: SecretStoreApi = secretStore(target)
  const action = at.action ?? 'add'
  const [state, take] = useFormLink(takeLink, () => store.mintLink(action, action === 'rotate' ? at.name : null))
  const list = useUrRead(store.list, `form:${target.scope === 'workspace' ? 'ws' : target.repoId}`)
  if (state.status !== 'open') return <Card title={at.name ?? 'Add a secret'}><Opening state={state} onBack={onBack} /></Card>
  if (written) return <Card title={at.name ?? draft?.name ?? 'Added'}><p className="pk-lead">This form is used up.</p></Card>
  if (action === 'rotate') {
    const secret = dataOf(list.state)?.secrets.find((s) => s.name === at.name)
    return (
      <Card title={at.name ?? 'Rotate'}>
        <dl className="sec-kv"><dt>Where</dt><dd>{whereWords(at, target, repository)}</dd><dt>Name</dt><dd><code>{at.name}</code> <span className="sec-hint">(fixed by the link)</span></dd></dl>
        {secret === undefined
          ? <UrRegion state={list.state} route="GET secrets" what="The secret to rotate" onRetry={list.reload}>{() => <p className="ur-small">No secret of that name is stored here.</p>}</UrRegion>
          : <RotateSecret secret={secret} store={store} takeToken={take} onCancel={onBack} onDone={onDone} />}
      </Card>
    )
  }
  const add = async (name: string) => {
    const grants = draft?.consumers ?? []
    const failed: string[] = []
    for (const consumer of grants) {
      const c = catalogue?.consumers.find((x) => x.consumer === consumer)
      const slot = c === undefined ? null : cellOf({ kind: draft!.kind }, c)?.slot ?? null
      const r = await store.grant(name, consumer, true, slot)
      if (r.status === 'error') failed.push(`${consumer} (${r.error.message})`)
    }
    const granted = grants.length - failed.length
    onDone(`${name} was written at ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}${granted > 0 ? `, and granted to ${granted === 1 ? grants.find((g) => !failed.some((f) => f.startsWith(g))) : `${granted} consumers`}` : ''}.${failed.length > 0 ? ` Not granted: ${failed.join(', ')}; tick it in the grant matrix.` : grants.length === 0 ? ' Grant it to a profile, tool or component before an agent can use it.' : ''}`)
  }
  return (
    <Card title={draft?.name ?? 'Add a secret'}>
      <dl className="sec-kv">
        <dt>Where</dt><dd>{whereWords(at, target, repository)}</dd>
        {draft !== null && <><dt>Granted to</dt><dd>{draft.consumers.length === 0 ? 'nothing yet: grant it from the matrix' : draft.consumers.join(', ')}</dd></>}
      </dl>
      {catalogue === null ? (
        <p className="ur-small">Reading the kinds a secret may be stored as…</p>
      ) : (
        <AddSecret catalogue={catalogue} store={store} fixed={draft} takeToken={take} onAdded={(name) => void add(name)} />
      )}
      <p className="sec-hint">The form is cleared whatever the answer.</p>
    </Card>
  )
}

/** A key form's one field: uncontrolled, never prefilled, read once and emptied before the request leaves. */
function KeyForm({
  title,
  where,
  label,
  takeLink,
  mint,
  save,
  onDone,
  written,
}: {
  title: string
  where: string
  label: string
  takeLink: () => string | null
  mint: () => Promise<Result<MintedLink>>
  save: (value: string, token: string | null) => Promise<Result<unknown>>
  onDone: (words: string) => void
  written: boolean
}) {
  const id = useId()
  const [state, take] = useFormLink(takeLink, mint)
  const field = useRef<HTMLInputElement>(null)
  const [filled, setFilled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  if (state.status !== 'open') return <Card title={title}><Opening state={state} /></Card>
  if (written) return <Card title={title}><p className="pk-lead">This form is used up.</p></Card>
  async function submit(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    const v = field.current?.value ?? ''
    if (field.current !== null) field.current.value = ''
    setFilled(false)
    if (v === '') return
    setBusy(true)
    setFailed(null)
    const r = await save(v, take())
    setBusy(false)
    if (r.status === 'error') {
      setFailed(r.error.code === 'KEY_LINK_INVALID'
        ? `${title} was not set: the one-time link is used, expired or not yours. Nothing was written.`
        : `${title} was not set: ${r.error.message}`)
      return
    }
    onDone(`${title} was written at ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}.`)
  }
  return (
    <Card title={title}>
      <dl className="sec-kv"><dt>Where</dt><dd>{where}</dd></dl>
      <form className="pk-form" autoComplete="off" aria-label={`Set ${title}`} onSubmit={(e) => void submit(e)}>
        <label htmlFor={`${id}-v`}>{label}</label>
        <input id={`${id}-v`} ref={field} className="ur-search pk-input" type="password" autoComplete="off" autoCapitalize="off"
          autoCorrect="off" spellCheck={false} placeholder="Paste the value" onChange={() => setFilled((field.current?.value ?? '') !== '')} />
        <p className="sec-hint">8 bytes to 8 KiB. The form is cleared whatever the answer.</p>
        <div className="pk-form-row">
          <Button kind="primary" size="sm" type="submit" busy={busy ? 'Setting…' : false} disabled={!filled}>Set secret</Button>
        </div>
      </form>
      {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
    </Card>
  )
}

function TypeSafeForm({ scope, takeLink, onDone, written }: { scope: 'user' | 'tenant'; takeLink: () => string | null; onDone: (w: string) => void; written: boolean }) {
  return (
    <KeyForm
      title={scope === 'user' ? 'Your TypeSafe key' : 'The workspace’s TypeSafe key'}
      where={scope === 'user' ? 'yours: your own submissions read it, before the workspace’s' : 'the workspace: Jev and browser agents read it'}
      label="TypeSafe key"
      takeLink={takeLink}
      mint={() => mintProviderKeyLink('typesafe', scope)}
      save={(v, t) => putProviderKey('typesafe', scope, v, t)}
      onDone={onDone}
      written={written}
    />
  )
}

function RunnerKeyForm({ provider, takeLink, onDone, written }: { provider: string; takeLink: () => string | null; onDone: (w: string) => void; written: boolean }) {
  return (
    <KeyForm
      title={`The ${provider} runner key`}
      where={`the workspace: the runner profiles that call ${provider} read it`}
      label={`${provider} API key`}
      takeLink={takeLink}
      mint={() => mintRunnerKeyLink(provider)}
      save={(v, t) => putRunnerKey(provider, v, t)}
      onDone={onDone}
      written={written}
    />
  )
}
