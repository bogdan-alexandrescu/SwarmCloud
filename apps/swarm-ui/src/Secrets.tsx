import { useEffect, useId, useLayoutEffect, useRef, useState, type FormEvent, type MouseEvent, type ReactNode, type RefObject } from 'react'
import {
  createWorkspaceSecret,
  loadRepositories,
  loadSecretsOverview,
  loadWorkspaceSecretCatalogue,
  mintProviderKeyLink,
  mintRunnerKeyLink,
  mintWorkspaceSecretLink,
  putRunnerKey,
  setWorkspaceSecretGrant,
  type ProviderKeyScope,
  type SecretsOverview,
  type SecretsRow,
  type SecretsScope,
  type SecretsShadow,
  type WorkspaceSecretCatalogue,
  type WorkspaceSecretConsumer,
} from './api'
import { Banner, Button, Card, Chip, Segmented, routedClick } from './components'
import { chooseTenant } from './fetch'
import { JevBackendCard } from './JudgmentBackend'
import { CONFIG_SECRETS_ADDRESS, addressToPath, keyLinkToken, secretsLinkToken, secretsLinkView } from './paths'
import { ProviderKeysScreen } from './ProviderKeys'
import type { RepoRecord } from './RepositoriesData'
import { UrRefresh, UrRegion, useUrRead, type UrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import { timeAgo } from './types'
import { KIND_WORDS, STRUCTURED, SecretField, WorkspaceSecretsScreen, type SecretsLink } from './WorkspaceSecrets'
import './styles/keys.css'
import './styles/workspace-settings.css'
import './styles/secrets.css'

/**
 * CONFIG › SECRETS (lane SSC-UI, part of #2158; docs/design/secrets-scopes.md
 * §6.1, with BGD's answers in its §11, drawn as Config proposal E's S4,
 * docs/web-ui/mockups/config-e.html, BGD's pick of 2026-10-24).
 *
 * ONE PAGE, THREE SCOPES. The picker is Workspace · Yours · Repository, and
 * the address carries it: `/config/secrets?scope=workspace|personal|
 * repository&repo=<repo_id>&dest=agents|actions` (paths.ts
 * `secretsLinkView`). Each scope draws its sections in §6.1's order:
 *
 *   Workspace   For agents (today's store and its grant matrix,
 *               WorkspaceSecrets.tsx), Keys for agents and MCP (what was
 *               Settings › API keys, BGD 2026-10-23T23:57Z, and the runner
 *               keys), GitHub (token status)
 *   Yours       For your agents, Browser sessions, Keys, GitHub
 *   Repository  a destination switch: Agents (that repository's store,
 *               "released only to agents that clone <owner/repo>", Sign-in
 *               for QA, its GitHub token) or GitHub Actions, whose pane is
 *               lane SSC-UI-ACTIONS' and is not drawn here
 *
 * Every row is the overview's (`GET /v1/secrets/overview`, SSC-OVERVIEW):
 * names, status, who set it and when, never a value, a length or any
 * character of one. THE REPOSITORY SCOPE LISTS ONLY REGISTERED REPOSITORIES:
 * its picker is `GET /v1/repositories`, and a `repo` the address names that
 * is not one of them is said to be so and read for nothing.
 *
 * SHADOWING (Q1: the person's, then the repository's, then the
 * workspace's) is drawn as the overview reports it, beside the name: the
 * secret that wins it and on whose agents, or what it hides.
 *
 * EVERY SET OPENS A ONE-TIME FORM, never prefilled: the link a tool minted
 * (its token App read off the bar), or one this page mints for itself on
 * click (§6.1). The token is held in a ref, never drawn or written into the
 * address; the page drops `t` from the bar in a layout effect, before
 * anything paints. A token belongs to the view it arrived with: it is sent
 * only by the form of that scope, repository and link, so a secret cannot
 * be written into another scope with it, and moving to another scope drops
 * the form.
 *
 * ADD A SECRET is one side sheet of four steps, in proposal E's order: scope
 * → destination → who reads it → the one-time form. Its last step mints the
 * store's add link for the scope step 1 chose, and that link's form is the
 * only place the value is typed.
 *
 * WHO MAY is each row's `can_manage`; the API is still the gate.
 */

/** App's capture of the one-time links' tokens, before its normalise rewrote the bar. */
export interface SecretsTokens {
  /** The `t` on `/config/secrets` or `/settings/workspace/secrets`: a store, runner-key or Actions link. */
  link: string | null
  /** The `t` on `/settings/keys/typesafe`: the provider-key link. */
  key: string | null
}

/** What the page's address says (`secretsLinkView`). */
export interface SecretsView {
  scope: SecretsScope
  repo: string | null
  dest: 'agents' | 'actions'
  env: string | null
  action: 'add' | 'rotate' | null
  name: string | null
  runnerKey: string | null
  key: 'typesafe' | null
}

/** The view a route carries; anything it does not keep is dropped first. */
export function secretsViewOf(view: string | null): SecretsView {
  const q = new URLSearchParams(secretsLinkView(view ?? ''))
  const scope = q.get('scope')
  const action = q.get('action')
  return {
    scope: scope === 'personal' || scope === 'repository' ? scope : 'workspace',
    repo: q.get('repo'),
    dest: q.get('dest') === 'actions' ? 'actions' : 'agents',
    env: q.get('env'),
    action: action === 'add' || action === 'rotate' ? action : null,
    name: q.get('name'),
    runnerKey: q.get('runner_key'),
    key: q.get('key') === 'typesafe' ? 'typesafe' : null,
  }
}

/** The router address of a scope (and a repository, a destination). Never a link's fields. */
export function secretsAddress(scope: SecretsScope, repo: string | null = null, dest: 'agents' | 'actions' = 'agents'): string {
  const q = new URLSearchParams({ scope })
  if (scope === 'repository' && repo !== null) {
    q.set('repo', repo)
    if (dest === 'actions') q.set('dest', dest)
  }
  return `${CONFIG_SECRETS_ADDRESS}?${q.toString()}`
}

/** Drop `t` from the address bar, keeping everything else. Idempotent. */
function dropLinkToken(): void {
  const url = new URL(window.location.href)
  if (!url.searchParams.has('t')) return
  url.searchParams.delete('t')
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`)
}

const SCOPE_WORD: Readonly<Record<SecretsScope, string>> = { workspace: 'Workspace', personal: 'Yours', repository: 'Repository' }
const OVERVIEW_ROUTE = 'GET /v1/secrets/overview'
const REPOS_ROUTE = 'GET /v1/repositories'
/** The QA targets page, where a target's sign-in form is (QaTargets.tsx). */
const QA_TARGETS_ADDRESS = 'config/repositories?page=qa-targets'
/** GitHub access, where a token is connected; its Saved logins view (BrowserLogins.tsx). */
const GITHUB_ADDRESS = 'config/github'
const LOGINS_ADDRESS = 'config/github?view=logins'
const MCP_ADDRESS = 'config/mcp-servers'

/** An in-app link, routed like the panel's own. */
function Go({ to, go, children }: { to: string; go: (to: string) => void; children: ReactNode }) {
  const click = (e: MouseEvent<HTMLAnchorElement>) => {
    if (!routedClick(e)) return
    e.preventDefault()
    go(to)
  }
  return <a href={addressToPath(to)} onClick={click}>{children}</a>
}

function repoName(r: RepoRecord): string {
  return `${r.owner}/${r.repo}`
}

/** "2d ago by alice@…", or what is known of it. */
function setWords(at: string | null, by: string | null): string {
  if (at === null) return by === null ? 'not said' : `by ${by}`
  return by === null ? timeAgo(at) : `${timeAgo(at)} by ${by}`
}

/**
 * WHAT THE OVERVIEW SAYS ABOUT ONE NAME (Q1), as chips: the secret that wins
 * it and where, or what it hides. One chip per (scope, where): a name
 * delivered into two slots is one fact to the reader, not two.
 */
export function ShadowChips({ row }: { row: SecretsRow | undefined }) {
  if (row === undefined || (row.shadowed_by.length === 0 && row.shadows.length === 0)) return null
  const once = (list: readonly SecretsShadow[]) => list.filter((e, i) => list.findIndex((o) => o.scope === e.scope && o.where === e.where) === i)
  return (
    <span className="sec-shadow">
      {once(row.shadowed_by).map((w) => (
        <Chip key={`by-${w.scope}-${w.where}`} tone="park" title={`On ${w.where}, the ${winnerWord(w)} secret of this name is the one released; this one is withheld there.`}>
          {shadowWords(w)}
        </Chip>
      ))}
      {once(row.shadows).map((h) => (
        <Chip key={`hides-${h.scope}-${h.where}`} title={`On ${winnerPlace(row, h)}, this secret is released and the ${hiddenWord(h)} one of the same name is withheld.`}>
          {`overrides the ${hiddenWord(h)}`}
        </Chip>
      ))}
    </span>
  )
}

/** A shadowed row's chip: who wins it, and where (Q1). */
function shadowWords(w: SecretsShadow): string {
  if (w.scope === 'personal') return `${w.person ?? 'a person'}’s own wins on their agents`
  if (w.scope === 'repository') return `${w.repository ?? 'a repository'}’s wins on its agents`
  return 'the workspace’s wins'
}

/** The winner, in the reader's words: "alice@example.com’s", "example-org/example-api’s". */
function winnerWord(w: SecretsShadow): string {
  if (w.scope === 'personal') return `${w.person ?? 'the person'}’s`
  if (w.scope === 'repository') return `${w.repository ?? 'the repository'}’s`
  return 'the workspace’s'
}

/** What a winner hides: "workspace’s", "repository’s (example-org/example-api)". */
function hiddenWord(h: SecretsShadow): string {
  if (h.scope === 'workspace') return 'workspace’s'
  if (h.scope === 'repository') return `repository’s (${h.repository ?? 'a repository'})`
  return `${h.person ?? 'a person'}’s`
}

function winnerPlace(row: SecretsRow, h: SecretsShadow): string {
  if (row.scope === 'personal') return 'your own agents'
  return h.where
}

/** The overview's rows of one section. */
function rowsOf(o: SecretsOverview, section: string): SecretsRow[] {
  return o.rows.filter((r) => r.section === section)
}

/** Rows a person could act on that are not set: what the lead counts. */
function unsetOf(o: SecretsOverview): SecretsRow[] {
  return o.rows.filter((r) => r.status === 'unset')
}

export function SecretsScreen({ view, tokens = null, go }: { view: string | null; tokens?: SecretsTokens | null; go: (to: string) => void }) {
  const v = secretsViewOf(view)
  // THE LINKS' TOKENS, held once, with the view they arrived on. App hands
  // them on the first render only; rendered alone, the page reads the bar.
  const held = useRef<{ view: SecretsView; link: string | null; key: string | null } | null>(null)
  if (held.current === null) {
    const loc = typeof window === 'undefined' ? null : window.location
    held.current = {
      view: v,
      link: tokens !== null ? tokens.link : loc === null ? null : secretsLinkToken(loc.pathname, loc.search),
      key: tokens !== null ? tokens.key : loc === null ? null : keyLinkToken(loc.pathname, loc.search),
    }
  }
  useLayoutEffect(() => {
    dropLinkToken()
  }, [])
  // A token is offered only while the page is still on the view it came with.
  const sameScope = held.current.view.scope === v.scope && held.current.view.repo === v.repo
  const linkView = sameScope ? held.current.view : null
  const [adding, setAdding] = useState(false)

  const repos = useUrRead(loadRepositories, 'secrets-repositories')
  const registered = repos.state.status === 'ok' || repos.state.status === 'stale' ? repos.state.data : repos.state.status === 'empty' ? [] : null
  const repo = v.scope === 'repository' && v.repo !== null && registered !== null ? registered.find((r) => r.repo_id === v.repo) ?? null : null
  // Read a scope only once it can be answered: a repository only when it is registered here.
  const readable = v.scope !== 'repository' || repo !== null
  const overview = useUrRead<SecretsOverview | null>(
    () => (readable ? loadSecretsOverview(v.scope, v.scope === 'repository' ? v.repo : null) : Promise.resolve({ status: 'ok', fetchedAt: Date.now(), data: null })),
    `secrets-overview:${v.scope}:${readable ? v.repo ?? '' : 'none'}`,
  )
  const data = overview.state.status === 'ok' || overview.state.status === 'stale' ? overview.state.data : null
  const empty = data !== null && v.scope === 'workspace' && rowsOf(data, 'agents').length === 0 && unsetOf(data).length === 0

  const link = (kind: 'store' | 'runner' | 'key'): string | null => {
    const h = held.current
    if (h === null || linkView === null) return null
    if (kind === 'key') return h.view.key !== null && v.key === h.view.key ? h.key : null
    if (kind === 'runner') return h.view.runnerKey !== null && v.runnerKey === h.view.runnerKey ? h.link : null
    return h.view.action !== null && v.action === h.view.action && v.name === h.view.name ? h.link : null
  }
  const storeLink: SecretsLink | null = linkView !== null && v.action !== null && held.current.view.action === v.action
    ? { action: v.action, name: v.name, token: link('store') }
    : null

  return (
    <div className="pk-page sec-page">
      <PageHead title="Secrets">
        <UrRefresh reads={[overview, repos]} ageOf={[overview]} />
        {!empty && (
          <Button kind="primary" size="sm" onClick={() => setAdding(true)}>
            Add a secret
          </Button>
        )}
      </PageHead>
      <ScopeBar view={v} repos={repos} registered={registered} go={go} />
      {v.scope === 'repository' && v.repo === null ? (
        <Card title="Pick a repository">
          <p className="pk-lead">
            A repository&rsquo;s secrets are released only to agents that clone it. Pick one of this workspace&rsquo;s
            registered repositories above.
          </p>
        </Card>
      ) : v.scope === 'repository' && registered !== null && repo === null ? (
        <Banner tone="bad" title="That repository is not registered in this workspace" role="alert">
          Only a registered repository has secrets here. Register it on <Go to="config/repositories" go={go}>Config › Repositories</Go>, or pick one above.
        </Banner>
      ) : (
        <ScopeBody
          overview={overview}
          view={v}
          repo={repo}
          go={go}
          storeLink={storeLink}
          runnerToken={link('runner')}
          keyToken={link('key')}
          onAdd={() => setAdding(true)}
        />
      )}
      {adding && (
        <AddSecretSheet
          start={v}
          registered={registered ?? []}
          personalTenant={v.scope === 'personal' ? data?.personal_tenant_id ?? null : null}
          workspace={data?.tenant_id ?? ''}
          go={go}
          onClose={() => setAdding(false)}
          onAdded={overview.reload}
        />
      )}
    </div>
  )
}

/** Workspace · Yours · Repository, and a Repository scope's registered repositories. */
function ScopeBar({ view, repos, registered, go }: { view: SecretsView; repos: UrRead<RepoRecord[]>; registered: RepoRecord[] | null; go: (to: string) => void }) {
  const id = useId()
  return (
    <div className="sec-scope">
      <div className="sec-scope-row">
        <span className="sec-scope-l" id={`${id}-l`}>Scope</span>
        <Segmented<SecretsScope>
          label="Scope"
          value={view.scope}
          options={(['workspace', 'personal', 'repository'] as const).map((k) => ({ key: k, label: SCOPE_WORD[k] }))}
          onChange={(k) => go(secretsAddress(k, k === 'repository' ? view.repo ?? registered?.[0]?.repo_id ?? null : null))}
        />
        {view.scope === 'repository' && (
          <span className="sec-repo">
            <label htmlFor={`${id}-repo`}>Repository</label>
            {registered === null ? (
              <span className="ur-small">{repos.state.status === 'error' ? `${REPOS_ROUTE} did not answer` : 'Reading the registered repositories…'}</span>
            ) : registered.length === 0 ? (
              <span className="ur-small">No repository is registered in this workspace yet.</span>
            ) : (
              <select id={`${id}-repo`} value={view.repo ?? ''} onChange={(e) => go(secretsAddress('repository', e.target.value, view.dest))}>
                {view.repo === null || !registered.some((r) => r.repo_id === view.repo) ? <option value="" disabled>Pick a repository</option> : null}
                {registered.map((r) => <option key={r.repo_id} value={r.repo_id}>{repoName(r)}</option>)}
              </select>
            )}
          </span>
        )}
      </div>
      <p className="sec-prec">Same name in several scopes: yours wins, then the repository&rsquo;s, then the workspace&rsquo;s.</p>
    </div>
  )
}

/** The page's lead: what this scope still lacks, or that nothing is listed yet, from the one read. */
function Lead({ o, view, onAdd, go }: { o: SecretsOverview; view: SecretsView; onAdd: () => void; go: (to: string) => void }) {
  const unset = unsetOf(o)
  const names = unset.map(unsetName)
  const listed = names.length > 3 ? `${names.slice(0, 3).join(', ')} and ${names.length - 3} more` : names.join(', ')
  if (view.scope === 'workspace' && rowsOf(o, 'agents').length === 0 && unset.length === 0) {
    return (
      <LeadBox tone="next" kicker="Nothing stored yet" title={`Add the first secret for ${o.tenant_id || 'this workspace'}`}
        body="Nothing is stored for this workspace yet. Usually first: a TypeSafe key, a search API key, or a QA sign-in on a repository."
        action={<Button kind="primary" size="sm" onClick={onAdd}>Add a secret</Button>} />
    )
  }
  if (unset.length === 0) {
    return <LeadBox tone="ok" kicker={SCOPE_WORD[view.scope]} title="Everything this scope lists is set" body="Set and Replace open a one-time form; no value is shown again." />
  }
  const may = unset.some((r) => r.can_manage)
  if (!may && view.scope !== 'personal') {
    return (
      <LeadBox tone="next" kicker="An admin’s step" title={`${unset.length} not set here: a workspace admin sets ${unset.length === 1 ? 'it' : 'them'}`}
        body={`${listed}. You can set your own under Yours; yours win for your own agents.`}
        action={<Button kind="primary" size="sm" onClick={() => go(secretsAddress('personal'))}>Go to Yours</Button>} />
    )
  }
  return <LeadBox tone="next" kicker={`${SCOPE_WORD[view.scope]} · ${unset.length} not set`} title={`Not set: ${listed}`} body="Set opens a one-time form; the value is never shown again." />
}

function unsetName(r: SecretsRow): string {
  if (r.section === 'qa_sign_in') return `${r.name} · ${r.environment ?? 'environment'} ${r.field === 'challenge_bypass' ? 'challenge bypass' : 'sign-in'}`
  if (r.store === 'provider_keys') return r.name === 'typesafe' ? 'the TypeSafe key' : `the ${r.name} key`
  if (r.store === 'mcp_servers') return `MCP server ${r.name}`
  return r.name
}

function LeadBox({ kicker, title, body, tone, action }: { kicker: string; title: string; body?: string; tone: 'next' | 'ok'; action?: ReactNode }) {
  return (
    <section className={`ws-lead is-${tone}`} aria-label={kicker}>
      <div className="ws-lead-t">
        <span className="ws-lead-k">{kicker}</span>
        <b>{title}</b>
        {body !== undefined && <span className="ws-lead-s">{body}</span>}
      </div>
      {action !== undefined && <div className="ws-lead-a">{action}</div>}
    </section>
  )
}

/**
 * ONE SCOPE'S SECTIONS. The store (For agents) stands on its own read, so a
 * failed overview never takes the store or a link's form with it; what only
 * the overview serves -- the lead, shadowing, keys, QA sign-in, GitHub
 * status -- is drawn from it, and its failure is said where those sections
 * would be.
 */
function ScopeBody({
  overview,
  view,
  repo,
  go,
  storeLink,
  runnerToken,
  keyToken,
  onAdd,
}: {
  overview: UrRead<SecretsOverview | null>
  view: SecretsView
  repo: RepoRecord | null
  go: (to: string) => void
  storeLink: SecretsLink | null
  runnerToken: string | null
  keyToken: string | null
  onAdd: () => void
}) {
  const o = overview.state.status === 'ok' || overview.state.status === 'stale' ? overview.state.data : null
  const agents = o === null ? [] : rowsOf(o, 'agents')
  const shadow = (name: string) => <ShadowChips row={agents.find((r) => r.name === name)} />
  const fromOverview = (draw: (o: SecretsOverview) => ReactNode) => (
    <UrRegion state={overview.state} route={OVERVIEW_ROUTE} what="This scope's secrets" plural onRetry={overview.reload}>
      {(d) => (d === null ? null : draw(d))}
    </UrRegion>
  )
  return (
    <>
      {o !== null && <Lead o={o} view={view} onAdd={onAdd} go={go} />}
      {view.scope === 'workspace' && (
        <>
          <WorkspaceSecretsScreen embedded link={storeLink} shadow={shadow} />
          {fromOverview((d) => (
            <div className="sec-g2">
              <KeysCard o={d} scope="workspace" go={go} runnerKey={view.runnerKey} runnerToken={runnerToken} keyOpen={view.key !== null} keyToken={keyToken} onChange={overview.reload} />
              <GitHubCard rows={rowsOf(d, 'github')} scope="workspace" go={go} />
            </div>
          ))}
          {/* Which backend Jev judges with (JUDGE-SELECT), beside the key it
              may need, as on Settings › API keys. */}
          <JevBackendCard />
        </>
      )}
      {view.scope === 'personal' && fromOverview((d) => <YoursBody o={d} go={go} keyOpen={view.key !== null} keyToken={keyToken} onChange={overview.reload} />)}
      {view.scope === 'repository' && repo !== null && (
        <RepositoryBody fromOverview={fromOverview} view={view} repo={repo} go={go} storeLink={storeLink} shadow={shadow} />
      )}
    </>
  )
}

/** The Repository scope: its destination switch, then the destination's sections. */
function RepositoryBody({
  fromOverview,
  view,
  repo,
  go,
  storeLink,
  shadow,
}: {
  fromOverview: (draw: (o: SecretsOverview) => ReactNode) => ReactNode
  view: SecretsView
  repo: RepoRecord
  go: (to: string) => void
  storeLink: SecretsLink | null
  shadow: (name: string) => ReactNode
}) {
  const name = repoName(repo)
  return (
    <>
      <div className="sec-dest">
        <Segmented<'agents' | 'actions'>
          label="Destination"
          value={view.dest}
          options={[{ key: 'agents', label: 'Agents' }, { key: 'actions', label: 'GitHub Actions' }]}
          onChange={(d) => go(secretsAddress('repository', repo.repo_id, d))}
        />
        <span className="sec-dest-s">
          {view.dest === 'actions'
            ? 'Written into the repository on GitHub. SwarmCloud reads names and update times, never values.'
            : `Released only to agents that clone ${name}.`}
        </span>
      </div>
      {view.dest === 'agents' ? (
        <>
          <p className="ur-small sec-who">
            A workspace admin may set these, and so may a member whom GitHub calls admin of {name}, asked live with their
            own token at the write.
          </p>
          <WorkspaceSecretsScreen key={repo.repo_id} embedded repoId={repo.repo_id} link={storeLink} shadow={shadow} />
          {fromOverview((o) => (
            <div className="sec-g2">
              <QaCard rows={rowsOf(o, 'qa_sign_in')} go={go} />
              <GitHubCard rows={rowsOf(o, 'github')} scope="repository" go={go} />
            </div>
          ))}
        </>
      ) : (
        fromOverview((o) => <ActionsPlace o={o} repo={repo} view={view} />)
      )}
    </>
  )
}

/**
 * THE GITHUB ACTIONS DESTINATION'S PLACE. Its pane -- GitHub's live list,
 * Set a secret, environments, Check rule and Create on GitHub -- is lane
 * SSC-UI-ACTIONS' (secrets-scopes.md §9), so this draws only what this
 * page already reads: SwarmCloud's own record of what it wrote here
 * (the overview's `actions` rows), and the terminal's way to the same form.
 */
function ActionsPlace({ o, repo, view }: { o: SecretsOverview; repo: RepoRecord; view: SecretsView }) {
  const rows = rowsOf(o, 'actions')
  const sc = `/sc secrets link repository ${view.name ?? '<NAME>'} --repo ${repoName(repo)} --actions${view.env === null ? '' : ` --env ${view.env}`}`
  return (
    <Card title="Written by SwarmCloud">
      <p className="pk-lead">
        The names SwarmCloud has written into {repoName(repo)}&rsquo;s GitHub Actions, and by whom. GitHub&rsquo;s own list,
        Set a secret and the environments are drawn here by their own lane (SSC-UI-ACTIONS); until then a one-time form
        for this destination is minted from a terminal:
      </p>
      <p className="sec-sc"><code>{sc}</code></p>
      {rows.length === 0 ? (
        <p className="ur-small">SwarmCloud has written no GitHub Actions secret here.</p>
      ) : (
        <table className="mcp-servers" aria-label="GitHub Actions secrets SwarmCloud wrote">
          <thead>
            <tr><th scope="col">Name</th><th scope="col">Where</th><th scope="col">Set</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.environment ?? ''}/${r.name}`}>
                <th scope="row" data-label="Name"><code>{r.name}</code></th>
                <td data-label="Where">{r.environment === null ? 'repository level' : `environment ${r.environment}`}</td>
                <td data-label="Set" title={r.set_at ?? undefined}>{setWords(r.set_at, r.set_by)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}

/** Yours: your own store secrets, browser sessions, your TypeSafe key and your GitHub slots. */
function YoursBody({ o, go, keyOpen, keyToken, onChange }: { o: SecretsOverview; go: (to: string) => void; keyOpen: boolean; keyToken: string | null; onChange: () => void }) {
  const mine = rowsOf(o, 'agents')
  const sessions = rowsOf(o, 'browser_sessions')
  const own = o.personal_tenant_id
  const here = own !== null && own === o.tenant_id
  if (own === null) {
    return (
      <Card title="For your agents">
        <p className="ur-small">You have no personal workspace yet, so nothing of yours is stored. Request one from Workspace setup.</p>
      </Card>
    )
  }
  return (
    <>
      <Card title="For your agents">
        <p className="pk-lead">
          Read only by your own submissions, and they win over the workspace&rsquo;s and a repository&rsquo;s of the same
          name. Nothing you set here is read by anyone else&rsquo;s agents.
        </p>
        {mine.length === 0 ? (
          <p className="ur-small">You store no secret of your own yet.</p>
        ) : (
          <table className="mcp-servers" aria-label="Your secrets">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Kind</th>
                <th scope="col">Set</th>
                <th scope="col">Granted to</th>
                {!here && <th scope="col">Used in {o.tenant_id}</th>}
              </tr>
            </thead>
            <tbody>
              {mine.map((r) => (
                <tr key={r.name}>
                  <th scope="row" data-label="Name"><code>{r.name}</code><ShadowChips row={r} /></th>
                  <td data-label="Kind">{KIND_WORDS[r.kind ?? ''] ?? (r.kind || 'not said')}</td>
                  <td data-label="Set" title={r.set_at ?? undefined}>{setWords(r.set_at, r.set_by)}</td>
                  <td data-label="Granted to">{r.consumers.length === 0 ? 'nothing yet' : r.consumers.join(', ')}</td>
                  {!here && <td data-label={`Used in ${o.tenant_id}`}>{r.opted_in === true ? 'yes' : 'no'}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {!here && (
          <div className="sec-switch">
            <span className="ur-small">They are added, rotated and granted in your own workspace, {own}.</span>
            <Button kind="secondary" size="sm" onClick={() => {
              chooseTenant(own)
              go(secretsAddress('workspace'))
            }}>
              Open your workspace&rsquo;s secrets
            </Button>
          </div>
        )}
      </Card>
      <div className="sec-g2">
        <Card title="Browser sessions">
          {sessions.length === 0 ? (
            <p className="ur-small">No saved browser session. A browser agent that asks you to sign in offers to save one.</p>
          ) : (
            <table className="mcp-servers" aria-label="Browser sessions">
              <thead>
                <tr><th scope="col">Session</th><th scope="col">Saved</th><th scope="col">Expires</th></tr>
              </thead>
              <tbody>
                {sessions.map((r) => (
                  <tr key={r.name}>
                    <th scope="row" data-label="Session"><code>{r.name}</code></th>
                    <td data-label="Saved" title={r.set_at ?? undefined}>{r.set_at === null ? 'not said' : timeAgo(r.set_at)}</td>
                    <td data-label="Expires" title={r.expires_at ?? undefined}>{r.expires_at === null ? 'not said' : untilWords(r.expires_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="ur-small">Only your agents reuse them. Remove one on <Go to={LOGINS_ADDRESS} go={go}>GitHub access › Saved logins</Go>.</p>
        </Card>
        <KeysCard o={o} scope="personal" go={go} runnerKey={null} runnerToken={null} keyOpen={keyOpen} keyToken={keyToken} onChange={onChange} />
      </div>
      <GitHubCard rows={rowsOf(o, 'github')} scope="personal" go={go} />
    </>
  )
}

/** "in 5 d", or "expired 2 h ago". */
function untilWords(at: string): string {
  const t = Date.parse(at)
  if (Number.isNaN(t)) return at
  if (t <= Date.now()) return `expired ${timeAgo(at)}`
  const h = Math.round((t - Date.now()) / 3_600_000)
  return h < 48 ? `in ${Math.max(h, 1)} h` : `in ${Math.round(h / 24)} d`
}

/**
 * KEYS FOR AGENTS AND MCP (§4; Settings › API keys, moved). TypeSafe opens
 * the provider-key form (ProviderKeys.tsx) through its one-time link; a
 * runner key opens the runner-key form through its own (SSC-LINKS); an MCP
 * server's credential is a store secret, granted in the matrix.
 */
function KeysCard({
  o,
  scope,
  go,
  runnerKey,
  runnerToken,
  keyOpen,
  keyToken,
  onChange,
}: {
  o: SecretsOverview
  scope: 'workspace' | 'personal'
  go: (to: string) => void
  runnerKey: string | null
  runnerToken: string | null
  keyOpen: boolean
  keyToken: string | null
  onChange: () => void
}) {
  const rows = rowsOf(o, 'keys')
  const accounts = rowsOf(o, 'accounts')
  const keyScope: ProviderKeyScope = scope === 'personal' ? 'user' : 'tenant'
  // The provider-key form: opened by the link App read, or by one minted on
  // Set. An address naming the key without a token opens nothing: every Set
  // goes through a one-time link.
  const [typesafe, setTypesafe] = useState<{ token: string | null } | null>(keyOpen && keyToken !== null ? { token: keyToken } : null)
  // The runner-key form, for one provider, likewise.
  const [runner, setRunner] = useState<{ provider: string; token: string | null } | null>(runnerKey !== null && runnerToken !== null ? { provider: runnerKey, token: runnerToken } : null)
  const [busy, setBusy] = useState<string | null>(null)
  const [refused, setRefused] = useState<string | null>(null)

  async function openTypesafe() {
    setBusy('typesafe')
    setRefused(null)
    const r = await mintProviderKeyLink('typesafe', keyScope)
    setBusy(null)
    if (r.status === 'error') return setRefused(`No one-time form was opened for the TypeSafe key: ${r.error.message}`)
    const t = r.status === 'ok' ? r.data.token : null
    if (t === null) return setRefused('No one-time form was opened for the TypeSafe key: the link came back without a token.')
    setTypesafe({ token: t })
  }
  async function openRunner(provider: string) {
    setBusy(provider)
    setRefused(null)
    const r = await mintRunnerKeyLink(provider)
    setBusy(null)
    if (r.status === 'error') return setRefused(`No one-time form was opened for the ${provider} key: ${r.error.message}`)
    const t = r.status === 'ok' ? r.data.token : null
    if (t === null) return setRefused(`No one-time form was opened for the ${provider} key: the link came back without a token.`)
    setRunner({ provider, token: t })
  }

  return (
    <Card title={scope === 'personal' ? 'Keys' : 'Keys for agents and MCP'}>
      {rows.length === 0 ? (
        <p className="ur-small">No key is listed in this scope.</p>
      ) : (
        <table className="mcp-servers" aria-label={scope === 'personal' ? 'Your keys' : 'Keys for agents and MCP'}>
          <thead>
            <tr><th scope="col">Key</th><th scope="col">Read by</th><th scope="col">State</th><th scope="col" aria-label="Actions" /></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.store}:${r.name}`}>
                <th scope="row" data-label="Key">{keyWords(r, scope)}</th>
                <td data-label="Read by">{readBy(r)}</td>
                <td data-label="State" title={r.set_at ?? undefined}>{keyState(r)}</td>
                <td data-label="Actions">
                  {r.store === 'provider_keys' && r.can_manage && (
                    <Button kind={r.status === 'set' ? 'secondary' : 'primary'} size="sm" busy={busy === 'typesafe' ? 'Opening…' : false}
                      aria-label={`${r.status === 'set' ? 'Replace' : 'Set'} the ${r.name} key`} onClick={() => void openTypesafe()}>
                      {r.status === 'set' ? 'Replace' : 'Set'}
                    </Button>
                  )}
                  {r.store === 'runner_provider_keys' && r.can_manage && (
                    <Button kind="secondary" size="sm" busy={busy === r.name ? 'Opening…' : false} aria-label={`Replace the ${r.name} key`} onClick={() => void openRunner(r.name)}>
                      Replace
                    </Button>
                  )}
                  {r.store === 'mcp_servers' && <Go to={MCP_ADDRESS} go={go}>MCP servers</Go>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {accounts.length > 0 && (
        <p className="ur-small">Subscription accounts are managed on <Go to="capacity/accounts" go={go}>Capacity › Accounts</Go>.</p>
      )}
      <p className="ur-small">Was Settings › API keys. Set and Replace open a one-time form; no value is shown again.</p>
      {refused !== null && <Banner tone="bad" title={refused} role="alert" />}
      {typesafe !== null && (
        <div className="sec-form">
          <ProviderKeysScreen embedded link={{ scope: keyScope, token: typesafe.token }} />
          <Button kind="ghost" size="sm" onClick={() => {
            setTypesafe(null)
            onChange()
          }}>
            Close the TypeSafe form
          </Button>
        </div>
      )}
      {runner !== null && (
        <RunnerKeyForm key={runner.provider} provider={runner.provider} token={runner.token} fromLink={runnerKey === runner.provider && runnerToken !== null}
          onClose={() => {
            setRunner(null)
            onChange()
          }} />
      )}
    </Card>
  )
}

function keyWords(r: SecretsRow, scope: 'workspace' | 'personal'): ReactNode {
  if (r.store === 'provider_keys') return <>{r.name === 'typesafe' ? 'TypeSafe key' : `${r.name} key`}<small className="sec-sub">{scope === 'personal' ? 'yours' : 'workspace'}</small></>
  if (r.store === 'runner_provider_keys') return <>{`${r.name} key`}<small className="sec-sub">runner LLM key</small></>
  if (r.store === 'mcp_servers') return <>{`MCP server ${r.name}`}<small className="sec-sub">credential from the store</small></>
  return r.name
}

function readBy(r: SecretsRow): string {
  if (r.store === 'provider_keys') return r.name === 'typesafe' ? 'Jev, the judgment models' : 'its provider’s tools'
  if (r.store === 'mcp_servers') return r.secrets.length === 0 ? 'no store secret is granted to it' : r.secrets.join(', ')
  return r.consumers.length === 0 ? 'nothing yet' : r.consumers.map((c) => c.replace(/^profile:/, '')).join(', ')
}

function keyState(r: SecretsRow): ReactNode {
  if (r.status === 'set') return r.set_at === null ? 'set' : `set ${timeAgo(r.set_at)}`
  if (r.status === 'unset') return <Chip tone="park">not set</Chip>
  return r.status || 'not said'
}

/** `gittokens.TOKEN_KINDS` in the reader's words; an unknown kind is shown as served. */
const TOKEN_KIND: Readonly<Record<string, string>> = {
  app_user: 'GitHub App user token',
  app_installation: 'GitHub App installation token',
  fine_grained_pat: 'fine-grained personal access token',
  classic_pat: 'classic personal access token',
}

/** GitHub token status (§4): kind, state, login, verified, expires. Never any character of a token. */
function GitHubCard({ rows, scope, go }: { rows: SecretsRow[]; scope: SecretsScope; go: (to: string) => void }) {
  return (
    <Card title="GitHub token">
      {rows.length === 0 ? (
        <p className="ur-small">
          {scope === 'repository'
            ? 'No repository token. Agents for this repository use the workspace’s token.'
            : scope === 'personal' ? 'You have no GitHub user token in this workspace.' : 'No workspace token is registered.'}
        </p>
      ) : (
        rows.map((r) => (
          <dl key={r.name} className="sec-kv">
            <dt>Kind</dt><dd>{r.kind === null ? 'not said' : TOKEN_KIND[r.kind] ?? r.kind}</dd>
            <dt>State</dt><dd>{r.status || 'not said'}</dd>
            <dt>GitHub login</dt><dd>{r.forge_login === null ? 'not said' : `@${r.forge_login}`}</dd>
            <dt>Verified</dt><dd>{r.verified_at === null ? 'never' : timeAgo(r.verified_at)}</dd>
            <dt>Expires</dt><dd>{r.expires_at === null ? 'not said' : untilWords(r.expires_at)}</dd>
          </dl>
        ))
      )}
      <p className="ur-small">Status only: no part of a token is ever shown. Connect or disconnect on <Go to={GITHUB_ADDRESS} go={go}>GitHub access</Go>.</p>
    </Card>
  )
}

/** Sign-in for QA: each QA target of the repository, per environment. Set opens the target's own form. */
function QaCard({ rows, go }: { rows: SecretsRow[]; go: (to: string) => void }) {
  return (
    <Card title="Sign-in for QA">
      {rows.length === 0 ? (
        <p className="ur-small">No QA target is registered for this repository.</p>
      ) : (
        <table className="mcp-servers" aria-label="Sign-in for QA">
          <thead>
            <tr><th scope="col">Target · environment</th><th scope="col">State</th><th scope="col" aria-label="Actions" /></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.target_id}/${r.environment}/${r.field}`}>
                <th scope="row" data-label="Target">
                  {`${r.name} · ${r.environment ?? 'environment'}`}
                  {r.field === 'challenge_bypass' && <small className="sec-sub">challenge bypass</small>}
                </th>
                <td data-label="State" title={r.set_at ?? undefined}>{r.status === 'set' ? `set ${setWords(r.set_at, r.set_by)}` : <Chip tone="park">not set</Chip>}</td>
                <td data-label="Actions">
                  {r.can_manage && (
                    <Button kind={r.status === 'set' ? 'secondary' : 'primary'} size="sm" onClick={() => go(QA_TARGETS_ADDRESS)}>
                      {r.status === 'set' ? 'Replace' : 'Set'}
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="ur-small">Set opens the target&rsquo;s one-time form on Config › Repositories › QA targets.</p>
    </Card>
  )
}

/**
 * THE RUNNER-KEY ONE-TIME FORM (SSC-LINKS; `POST /v1/tenants/me/credentials`).
 * The field is uncontrolled, never prefilled, read once and emptied before
 * the request leaves; the token goes with the first write and is forgotten.
 */
function RunnerKeyForm({ provider, token, fromLink, onClose }: { provider: string; token: string | null; fromLink: boolean; onClose: () => void }) {
  const id = useId()
  const input = useRef<HTMLInputElement>(null)
  const held = useRef<string | null>(token)
  const [filled, setFilled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState<{ ok: boolean; words: string } | null>(null)

  async function save(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    const value = input.current?.value ?? ''
    if (input.current !== null) input.current.value = ''
    setFilled(false)
    if (value === '') return
    const t = held.current
    held.current = null
    setBusy(true)
    setSaid(null)
    const r = await putRunnerKey(provider, value, t)
    setBusy(false)
    if (r.status === 'error') {
      setSaid({ ok: false, words: r.error.code === 'KEY_LINK_INVALID'
        ? `The ${provider} key was not set: the one-time link is used, expired or not yours. Replace it again to mint a new one.`
        : `The ${provider} key was not set: ${r.error.message}` })
      return
    }
    setSaid({ ok: true, words: `The ${provider} key was set. The link is used up; the value is not shown again, here or anywhere.` })
  }

  return (
    <div className="sec-form">
      {fromLink && (
        <Banner tone="info" title="Opened from a one-time link">
          Paste the key into the form below: the /sc command and this page&rsquo;s address never see it. The link works
          once, for ten minutes.
        </Banner>
      )}
      <form className="pk-form" autoComplete="off" aria-label={`Set the ${provider} key`} onSubmit={(e) => void save(e)}>
        <p className="pk-lead">The workspace&rsquo;s {provider} key for its runners. Write-only: no page or route shows it again.</p>
        <SecretField id={`${id}-value`} label="Value" masked input={input} onInput={() => setFilled((input.current?.value ?? '') !== '')} />
        <div className="pk-form-row">
          <Button kind="primary" size="sm" type="submit" busy={busy ? 'Setting…' : false} disabled={!filled}>Set key</Button>
          <Button kind="ghost" size="sm" onClick={onClose}>Close</Button>
        </div>
      </form>
      {said !== null && (said.ok ? <p className="pk-saved" role="status">{said.words}</p> : <Banner tone="bad" title={said.words} role="alert" />)}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Add a secret: one side sheet, four steps (proposal E S4c)
// ---------------------------------------------------------------------------

const STEPS = ['Scope', 'Destination', 'Who reads it', 'One-time form'] as const

/** A store name (swarm_api `check_name`): lower-case letters, digits and hyphens, from a letter or a digit. */
const STORE_NAME = /^[a-z0-9][a-z0-9-]{0,63}$/

interface Choice {
  scope: SecretsScope
  repo: string | null
  dest: 'agents' | 'actions'
}

function AddSecretSheet({
  start,
  registered,
  personalTenant,
  workspace,
  go,
  onClose,
  onAdded,
}: {
  start: SecretsView
  registered: RepoRecord[]
  /** The caller's own workspace, when the page has read it (the Yours scope). */
  personalTenant: string | null
  /** The selected workspace's id, as the overview names it. */
  workspace: string
  go: (to: string) => void
  onClose: () => void
  onAdded: () => void
}) {
  const titleId = useId()
  const [step, setStep] = useState(1)
  const [choice, setChoice] = useState<Choice>({
    scope: start.scope,
    repo: start.scope === 'repository' ? start.repo ?? registered[0]?.repo_id ?? null : null,
    dest: 'agents',
  })
  const [name, setName] = useState('')
  const [provider, setProvider] = useState('other')
  const [kind, setKind] = useState('api_key')
  const [grants, setGrants] = useState<readonly string[]>([])
  const catalogue = useUrRead(loadWorkspaceSecretCatalogue, 'workspace-secret-catalogue')
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeRef.current()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  const repo = choice.repo === null ? null : registered.find((r) => r.repo_id === choice.repo) ?? null
  // Yours is written in the person's own workspace; from a group one it is a switch, not a write.
  const yoursElsewhere = choice.scope === 'personal' && (personalTenant === null || personalTenant !== workspace)
  const can = [
    // 1 scope
    (choice.scope !== 'repository' || repo !== null) && !yoursElsewhere,
    // 2 destination: GitHub Actions' form is SSC-UI-ACTIONS'
    choice.dest === 'agents',
    // 3 who reads it
    STORE_NAME.test(name.trim()),
    true,
  ]
  const pick = (next: Partial<Choice>) => {
    setChoice((c) => ({ ...c, ...next }))
  }

  const sheet = (
    <div className="sec-scrim" onMouseDown={(e) => {
      if (e.target === e.currentTarget) onClose()
    }}>
      <aside className="sec-sheet" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <div className="sec-sheet-h">
          <h2 id={titleId}>Add a secret</h2>
          <Button kind="ghost" size="sm" aria-label="Close Add a secret" onClick={onClose}>Close</Button>
        </div>
        <ol className="sec-steps" aria-label="Steps">
          {STEPS.map((s, i) => (
            <li key={s} className={i + 1 < step ? 'is-done' : i + 1 === step ? 'is-on' : ''} aria-current={i + 1 === step ? 'step' : undefined}>
              <i aria-hidden="true">{i + 1 < step ? '✓' : i + 1}</i>{s}
            </li>
          ))}
        </ol>
        {step === 1 && (
          <section className="sec-step" aria-label="Where does it live?">
            <h3>Where does it live?</h3>
            <Opt name="scope" on={choice.scope === 'workspace'} title={`Workspace${workspace ? ` · ${workspace}` : ''}`}
              sub="Any member’s agents may be granted it. Set by a workspace admin." onPick={() => pick({ scope: 'workspace', repo: null, dest: 'agents' })} />
            <Opt name="scope" on={choice.scope === 'personal'} title="Yours"
              sub="Only your own submissions read it, and it wins over the others." onPick={() => pick({ scope: 'personal', repo: null, dest: 'agents' })} />
            <Opt name="scope" on={choice.scope === 'repository'} title="Repository"
              sub="Only for one repository, and released only to agents that clone it." onPick={() => pick({ scope: 'repository', repo: choice.repo ?? registered[0]?.repo_id ?? null })} />
            {choice.scope === 'repository' && (
              registered.length === 0 ? (
                <p className="ur-small">No repository is registered in this workspace yet.</p>
              ) : (
                <label className="sec-fld">
                  <span>Repository</span>
                  <select value={choice.repo ?? ''} onChange={(e) => pick({ repo: e.target.value })}>
                    {registered.map((r) => <option key={r.repo_id} value={r.repo_id}>{repoName(r)}</option>)}
                  </select>
                </label>
              )
            )}
            {yoursElsewhere && (
              <div className="sec-switch">
                <span className="ur-small">
                  Your own secrets are added in your personal workspace{personalTenant === null ? '' : `, ${personalTenant}`}: switch to it, then add one there.
                </span>
                {personalTenant !== null && (
                  <Button kind="secondary" size="sm" onClick={() => {
                    chooseTenant(personalTenant)
                    onClose()
                    go(secretsAddress('workspace'))
                  }}>
                    Switch to your workspace
                  </Button>
                )}
              </div>
            )}
          </section>
        )}
        {step === 2 && (
          <section className="sec-step" aria-label="Who is it for?">
            <h3>Who is it for?</h3>
            <Opt name="dest" on={choice.dest === 'agents'} title="Agents"
              sub={repo === null ? 'Agents of this workspace, through the grants you tick next.' : `Agents that clone ${repoName(repo)}.`} onPick={() => pick({ dest: 'agents' })} />
            {choice.scope === 'repository' && repo !== null && (
              <Opt name="dest" on={choice.dest === 'actions'} title="GitHub Actions"
                sub="Workflows in the repository, on GitHub." onPick={() => pick({ dest: 'actions' })} />
            )}
            {choice.dest === 'actions' && repo !== null && (
              <div className="sec-note">
                <p className="ur-small">
                  GitHub Actions&rsquo; form is drawn by its own lane (SSC-UI-ACTIONS) and is not here yet. From a terminal,
                  the same one-time form:
                </p>
                <p className="sec-sc"><code>{`/sc secrets link repository <NAME> --repo ${repoName(repo)} --actions [--env <environment>]`}</code></p>
              </div>
            )}
          </section>
        )}
        {step === 3 && (
          <section className="sec-step" aria-label="Who reads it, and its name">
            <h3>Who reads it, and its name</h3>
            <label className="sec-fld">
              <span>Name</span>
              <input className="ur-search pk-input" value={name} maxLength={64} spellCheck={false} autoCapitalize="off" autoCorrect="off"
                placeholder="search-api" aria-describedby={`${titleId}-name`} onChange={(e) => setName(e.target.value)} />
            </label>
            <span id={`${titleId}-name`} className="jb-note">Lower-case letters, digits and hyphens, starting with a letter or a digit. Never the value.</span>
            <UrRegion state={catalogue.state} route="GET /v1/workspace/secrets/consumers" what="The kinds and consumers a secret may have" plural onRetry={catalogue.reload}>
              {(c) => <WhoReads catalogue={c} provider={provider} kind={kind} grants={grants} onProvider={setProvider} onKind={(k) => {
                setKind(k)
                setGrants([])
              }} onGrants={setGrants} />}
            </UrRegion>
          </section>
        )}
        {step === 4 && (
          <OneTimeAdd
            choice={choice}
            repoLabel={repo === null ? null : repoName(repo)}
            name={name.trim()}
            provider={provider}
            kind={kind}
            grants={grants}
            catalogue={catalogue.state.status === 'ok' || catalogue.state.status === 'stale' ? catalogue.state.data : null}
            onAdded={onAdded}
          />
        )}
        <div className="sec-sheet-f">
          {step === 1 ? <Button kind="ghost" size="sm" onClick={onClose}>Cancel</Button> : <Button kind="ghost" size="sm" onClick={() => setStep(step - 1)}>Back</Button>}
          {step < 4 && (
            <Button kind="primary" size="sm" disabled={!can[step - 1]} onClick={() => setStep(step + 1)}>Next</Button>
          )}
        </div>
      </aside>
    </div>
  )
  // Drawn in place, not portalled to <body>: the console's form controls are
  // styled under `.app`, and a sheet outside it drew bare native selects.
  return sheet
}

function Opt({ name, on, title, sub, onPick }: { name: string; on: boolean; title: string; sub: string; onPick: () => void }) {
  return (
    <label className={`sec-opt${on ? ' is-on' : ''}`}>
      <input type="radio" name={name} checked={on} onChange={onPick} />
      <span><b>{title}</b><small>{sub}</small></span>
    </label>
  )
}

/** Step 3 on the Agents path: the kind and provider, and which consumers are granted it after the value is set. */
function WhoReads({
  catalogue,
  provider,
  kind,
  grants,
  onProvider,
  onKind,
  onGrants,
}: {
  catalogue: WorkspaceSecretCatalogue
  provider: string
  kind: string
  grants: readonly string[]
  onProvider: (p: string) => void
  onKind: (k: string) => void
  onGrants: (g: readonly string[]) => void
}) {
  const fits = catalogue.consumers.filter((c) => c.grantable && c.slots.some((s) => s.kinds.includes(kind)))
  const providers = catalogue.providers.length > 0 ? catalogue.providers : ['other']
  const kinds = catalogue.kinds.length > 0 ? catalogue.kinds : ['api_key']
  return (
    <>
      <div className="pk-form-row">
        <label className="sec-fld">
          <span>Provider</span>
          <select value={providers.includes(provider) ? provider : providers[0]} onChange={(e) => onProvider(e.target.value)}>
            {providers.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        <label className="sec-fld">
          <span>Kind</span>
          <select value={kinds.includes(kind) ? kind : kinds[0]} onChange={(e) => onKind(e.target.value)}>
            {kinds.map((k) => <option key={k} value={k}>{KIND_WORDS[k] ?? k}</option>)}
          </select>
        </label>
      </div>
      <fieldset className="sec-grants">
        <legend>Granted to, once the value is set</legend>
        {fits.length === 0 ? (
          <p className="ur-small">No consumer reads {KIND_WORDS[kind] ?? kind} yet; grant it later in the matrix.</p>
        ) : (
          fits.map((c) => (
            <label key={c.consumer} className="sec-tick">
              <input type="checkbox" checked={grants.includes(c.consumer)} onChange={(e) => onGrants(e.target.checked ? [...grants, c.consumer] : grants.filter((g) => g !== c.consumer))} />
              <code>{c.consumer}</code>
            </label>
          ))
        )}
      </fieldset>
    </>
  )
}

/** A consumer's slot for this kind, when it reads several; null for its only one (the API's default). */
function slotFor(c: WorkspaceSecretConsumer | undefined, kind: string): string | null {
  if (c === undefined || c.slots.length <= 1) return null
  return c.slots.find((s) => s.kinds.includes(kind))?.slot ?? null
}

/**
 * STEP 4: THE ONE-TIME FORM. "Open the one-time form" mints the store's add
 * link for the scope step 1 chose (`POST .../links`); the form it opens is
 * the only place the value is typed. Its fields are uncontrolled and never
 * prefilled, read once and emptied before the request leaves, and the
 * token goes with that one write to the route of that same scope. The
 * grants ticked in step 3 are written after the value is stored.
 */
function OneTimeAdd({
  choice,
  repoLabel,
  name,
  provider,
  kind,
  grants,
  catalogue,
  onAdded,
}: {
  choice: Choice
  repoLabel: string | null
  name: string
  provider: string
  kind: string
  grants: readonly string[]
  catalogue: WorkspaceSecretCatalogue | null
  onAdded: () => void
}) {
  const id = useId()
  const repoId = choice.scope === 'repository' ? choice.repo : null
  const token = useRef<string | null>(null)
  const [opened, setOpened] = useState(false)
  const [busy, setBusy] = useState<string | false>(false)
  const [filled, setFilled] = useState(false)
  const [said, setSaid] = useState<{ ok: boolean; words: string } | null>(null)
  const value = useRef<HTMLInputElement>(null)
  const first = useRef<HTMLInputElement>(null)
  const second = useRef<HTMLInputElement>(null)
  const structured = STRUCTURED[kind]
  const fields = (): RefObject<HTMLInputElement>[] => (structured === undefined ? [value] : [first, second])
  const where = repoLabel === null ? (choice.scope === 'personal' ? 'your own workspace' : 'this workspace') : repoLabel
  const scopeArg = choice.scope === 'repository' ? `repository ${name} --repo ${repoLabel ?? '<owner/repo>'}` : `${choice.scope} ${name}`

  async function open() {
    setBusy('Opening…')
    setSaid(null)
    const r = await mintWorkspaceSecretLink('add', null, repoId)
    setBusy(false)
    if (r.status === 'error') return setSaid({ ok: false, words: `No one-time form was opened: ${r.error.message}` })
    const t = r.status === 'ok' ? r.data.token : null
    if (t === null) return setSaid({ ok: false, words: 'No one-time form was opened: the link came back without a token.' })
    token.current = t
    setOpened(true)
  }

  async function save(e: FormEvent) {
    e.preventDefault()
    if (busy !== false) return
    const read = fields().map((f) => {
      const v = f.current?.value ?? ''
      if (f.current !== null) f.current.value = ''
      return v
    })
    setFilled(false)
    if (read.some((v) => v === '')) return
    const t = token.current
    token.current = null
    setBusy('Setting…')
    setSaid(null)
    const body = structured === undefined
      ? { name, provider, kind, value: read[0]! }
      : { name, provider, kind, fields: { [structured[0].key]: read[0]!, [structured[1].key]: read[1]! } }
    const r = await createWorkspaceSecret(body, t, repoId)
    if (r.status === 'error') {
      setBusy(false)
      setOpened(false)
      setSaid({ ok: false, words: r.error.code === 'SECRET_LINK_INVALID'
        ? `${name} was not added: the one-time link is used, expired or not yours. Open the form again for a new one.`
        : `${name} was not added: ${r.error.message}` })
      return
    }
    const refused: string[] = []
    for (const consumer of grants) {
      const g = await setWorkspaceSecretGrant(name, consumer, true, slotFor(catalogue?.consumers.find((c) => c.consumer === consumer), kind), repoId)
      if (g.status === 'error') refused.push(`${consumer} (${g.error.message})`)
    }
    setBusy(false)
    setOpened(false)
    onAdded()
    const granted = grants.filter((g) => !refused.some((x) => x.startsWith(`${g} `)))
    setSaid({
      ok: refused.length === 0,
      words: `${name} was added in ${where}${granted.length === 0 ? '' : ` and granted to ${granted.join(', ')}`}. The link is used up; the value is not shown again, here or anywhere.${refused.length === 0 ? '' : ` Not granted: ${refused.join('; ')}.`}`,
    })
  }

  return (
    <section className="sec-step" aria-label="Set the value in a one-time form">
      <h3>Set the value in a one-time form</h3>
      <p className="ur-small">
        A form for <code>{name}</code> in {where}. It works once, for ten minutes, and is never prefilled. The value is
        stored write-only and never shown again.
      </p>
      {!opened ? (
        <>
          <Button kind="primary" size="sm" busy={busy} onClick={() => void open()}>Open the one-time form</Button>
          <div className="sec-note">
            <p className="ur-small">From a terminal instead:</p>
            <p className="sec-sc"><code>{`/sc secrets link ${scopeArg}`}</code></p>
          </div>
        </>
      ) : (
        <form className="pk-form" autoComplete="off" aria-label={`Set ${name}`} onSubmit={(e) => void save(e)}>
          {structured === undefined ? (
            <SecretField id={`${id}-value`} label="Value" masked input={value} onInput={() => setFilled((value.current?.value ?? '') !== '')} />
          ) : (
            <>
              <SecretField id={`${id}-a`} label={structured[0].label} masked={structured[0].masked} input={first}
                onInput={() => setFilled((first.current?.value ?? '') !== '' && (second.current?.value ?? '') !== '')} />
              <SecretField id={`${id}-b`} label={structured[1].label} masked={structured[1].masked} input={second}
                onInput={() => setFilled((first.current?.value ?? '') !== '' && (second.current?.value ?? '') !== '')} />
            </>
          )}
          <div className="pk-form-row">
            <Button kind="primary" size="sm" type="submit" busy={busy} disabled={!filled}>Set secret</Button>
          </div>
        </form>
      )}
      {said !== null && (said.ok ? <p className="pk-saved" role="status">{said.words}</p> : <Banner tone="bad" title={said.words} role="alert" />)}
    </section>
  )
}
