import { createContext, useContext, useEffect, useId, useLayoutEffect, useRef, useState, type FormEvent, type MouseEvent, type ReactNode } from 'react'
import {
  checkActionsRule,
  deleteBrowserLogin,
  deleteProviderKey,
  loadActionsSecrets,
  loadMe,
  loadRepositories,
  loadSecretsOverview,
  loadWorkspaceSecretCatalogue,
  mintActionsLink,
  mintProviderKeyLink,
  mintRunnerKeyLink,
  putProviderKey,
  putRunnerKey,
  secretStore,
  type ActionsEnvironmentRule,
  type ActionsSecretList,
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
import { BrowserLoginsPane, isPersonalTenant } from './BrowserLogins'
import { githubTokenState } from './githubTokenState'
import { Banner, Button, ButtonLink, Card, Dash, Segmented, Tabs, TypedConfirm, routedClick, type TabDef } from './components'
import { SetupContext, setupNext, useSetupSteps, type SetupStep } from './ConfigSetup'
import { configSetupOf } from './configSetupApi'
import { chooseTenant, type Result } from './fetch'
import { QA_LINK_ADDRESS, SECRETS_LOGINS_VIEW, addressToPath, secretsLinkToken, secretsLinkView } from './paths'
import { UrRefresh, UrRegion, useUrRead, type UrRead } from './RepositoriesParts'
import type { RepoRecord } from './RepositoriesData'
import { JevBackendCard } from './JudgmentBackend'
import { SecretsActions, nameProblem, readersOf, ruleWords, scLine, type ActionsFormLink } from './SecretsActions'
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
 * link's token is never part of it. `add=secret&consumer=..` (paths.ts
 * `secretsAddAddress`, MCP-LEAD-FORM) opens Add a secret with that consumer
 * ticked, for a reader who may add one, and never reads as a one-time link.
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
 * THE GITHUB ACTIONS DESTINATION is SSC-UI-ACTIONS' pane (`SecretsActions`,
 * mounted here by lane SECRETS-ACTIONS-MOUNT, S4b2 and S4f): this page draws
 * the switch, and the pane GitHub's live list, environments, Set and Delete.
 * An Actions link (`dest=actions&name=..&env=..&t=..`) hands its token to the
 * pane's one-time form, the same way: off the bar before paint, held in a
 * ref, sent once. The wizard's Actions path (S4c) mints that form's link for
 * the destination and name it chose, and hands it over the same way.
 *
 * THE LEAD (S4a, S4h, S4i) counts what the setup strip counts: the overview's
 * MCP and QA rows, and the workspace TypeSafe key whenever the setup read
 * (`config_setup.secrets`, `provider_key`) lists it -- only while Jev judges
 * with TypeSafe, which the overview cannot tell. With nothing stored and
 * nothing needed it is "Step 3 · Add your first secret".
 *
 * THE LEAD FOLLOWS THE STRIP, AND "STORED" COUNTS THE KEYS (VQ-041). A
 * workspace whose only setting was a key read as empty: the lead said "Step
 * 3 · next · Add your first secret" under a strip that marked step 3 done,
 * and For agents suggested the TypeSafe key the table below showed set. So
 * "nothing stored" is no agents secret AND no key set; with something stored
 * and nothing missing the lead says step 3 is done, what is set, and the
 * strip's next step; and every empty line says what is empty, with the keys
 * that are set beside it.
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
  /** `add=secret`: Add a secret open (MCP-LEAD-FORM), with `consumer` to tick if the catalogue lists it. */
  add: boolean
  consumer: string | null
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
    add: q.get('add') === 'secret',
    consumer: q.get('consumer'),
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
  if (at.add) q.set('add', 'secret')
  if (at.add && at.consumer) q.set('consumer', at.consumer)
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

/** An in-app link, routed like the panel's own; `kind` draws it as a button. */
function AppLink({ address, children, label, kind }: { address: string; children: ReactNode; label?: string; kind?: 'primary' | 'secondary' }) {
  const go = (e: MouseEvent<HTMLAnchorElement>) => {
    if (!routedClick(e)) return
    e.preventDefault()
    window.history.pushState(null, '', addressToPath(address))
    window.dispatchEvent(new PopStateEvent('popstate'))
  }
  if (kind !== undefined) return <ButtonLink kind={kind} size="sm" href={addressToPath(address)} aria-label={label} onClick={go}>{children}</ButtonLink>
  return <a className="ctl-link" href={addressToPath(address)} aria-label={label} onClick={go}>{children}</a>
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

/**
 * THE SIGNED-IN EMAIL, or null until it is known (and when it cannot be read):
 * the frame's read, once per page, so a shadow note can say "yours" to the
 * person it names (visual QA pass 2, config2-16).
 */
const SignedInEmail = createContext<string | null>(null)

function useSignedInEmail(): string | null {
  const [email, setEmail] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    void loadMe({ frame: true }).then((r) => {
      // A reply without its principal says nothing of who is reading: no "yours".
      if (live && (r.status === 'ok' || r.status === 'stale')) setEmail(r.data.principal?.email ?? null)
    })
    return () => {
      live = false
    }
  }, [])
  return email
}

/**
 * A shadow entry in the reader's words: "Yours wins on agents of ana@…". The
 * winner is the reader's own personal secret when its person is the signed-in
 * email: "your own wins on your agents", never the reader in the third
 * person (config2-16).
 */
function winnerWords(s: SecretsShadow, me: string | null): string {
  if (s.scope === 'personal' && s.person !== null && me !== null && s.person.toLowerCase() === me.toLowerCase()) return 'your own wins on your agents'
  const who = s.scope === 'personal' ? 'Yours' : s.scope === 'repository' ? `${s.repository ?? 'the repository'}’s` : 'The workspace’s'
  return s.scope === 'personal' && s.person !== null ? `${s.person}’s wins on their agents` : `${who} wins on ${s.where}`
}

/**
 * The notes under a row's name that the overview reports shadowing for (§2.4).
 * A LINE OF SMALL TEXT, NOT A PILL (visual QA pass 1, config-23): "shadowed:
 * ana@…’s wins on their agents" wrapped to two lines inside a dashed pill and
 * doubled the row's height. Each note is its own line under the name.
 */
function ShadowNotes({ row }: { row: SecretsRow | undefined }) {
  const me = useContext(SignedInEmail)
  if (row === undefined || (row.shadowed_by.length === 0 && row.shadows.length === 0)) return null
  const lost = Array.from(new Set(row.shadowed_by.map((s) => winnerWords(s, me))))
  const won = Array.from(new Set(row.shadows.map((s) => `overrides ${SCOPE_WORDS[s.scope]}${s.scope === 'workspace' ? '' : ` on ${s.where}`}`)))
  return (
    <>
      {won.map((w) => <small key={w} className="sec-note is-info">{w}</small>)}
      {lost.map((w) => <small key={w} className="sec-note">shadowed: {w}</small>)}
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

/** The API's reason as a sentence: capitalised, with its full stop. */
function sentenceOf(text: string): string {
  const t = text.trim()
  return `${t.charAt(0).toUpperCase()}${t.slice(1)}${/[.!?]$/.test(t) ? '' : '.'}`
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

/** The keys that are set: a provider key or a runner key, never an MCP server's row or Subscription accounts. */
function keysSetOf(rows: readonly SecretsRow[]): SecretsRow[] {
  return rows.filter((r) => r.section === 'keys' && r.store !== 'mcp_servers' && r.status === 'set')
}

/** "1 key is" / "2 keys are", for the lines that name the keys set beside an empty list. */
function keysAre(n: number): string {
  return n === 1 ? '1 key is' : `${n} keys are`
}

/** Rows that block something until they are set: a QA sign-in, an MCP server's credential. */
function neededOf(rows: readonly SecretsRow[]): SecretsRow[] {
  return rows.filter((r) => r.status === 'unset' && ((r.section === 'keys' && r.store === 'mcp_servers') || (r.section === 'qa_sign_in' && r.field === 'credential')))
}

const OVERVIEW_ROUTE = 'GET /v1/secrets/overview'

/**
 * BROWSER LOGINS, THE PAGE'S SECOND TAB (GA-MOVE-VIEWS; part of #2157, BGD
 * 2026-10-24: "Browser logins to Config › Secrets"). It was GitHub access'
 * Browser logins view: the person's own saved live-browser sessions, by name
 * and site, with a typed Revoke (BrowserLogins.tsx `BrowserLoginsPane`, moved
 * whole), beside the secrets they belong with. Its address is `?view=logins`,
 * which `/config/github?view=logins` and `/access?view=logins` land on
 * (paths.ts `MOVED_VIEWS`). The tab is the ROUTED view, never the bar read at
 * mount: an old hash bookmark is rewritten to this path only after the first
 * render. A scope, a one-time link's form or Add a secret opens the first
 * tab whatever the address says (`secretsLinkView` keeps `view` only alone),
 * because each is drawn there.
 */
const SECRETS_TABS: readonly TabDef[] = [
  { key: 'secrets', label: 'Secrets' },
  { key: SECRETS_LOGINS_VIEW, label: 'Browser logins' },
]
const CATALOGUE_ROUTE = 'GET /v1/workspace/secrets/consumers'

export function SecretsScreen({ view, token = null }: { view: string | null; token?: string | null }) {
  const at = parseSecretsView(view)
  const viewKey = viewOf(at)
  const logins = secretsLinkView(view ?? '') === `view=${SECRETS_LOGINS_VIEW}`
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
    // A link's token is for the view it opened and no other: leaving that
    // view lets it go, so coming back never hands a spent one over again.
    if (link.current !== null && link.current.view !== viewKey) link.current = { view: link.current.view, token: null }
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
  const picked = registered?.find((r) => r.repo_id === repoId)
  const repoName = picked === undefined ? null : `${picked.owner}/${picked.repo}`
  // AN ACTIONS LINK'S FORM (S4d on the Actions path): the pane's one-time
  // form, opened with the token this view's link brought. Read without
  // taking it, since a render may run twice; the pane keeps it in its own ref
  // as it mounts, and `ActionsPane` lets this page's copy go once it has.
  const actionsToken = at.scope === 'repository' && at.dest === 'actions' && at.name !== null && link.current.view === viewKey ? link.current.token : null
  const actionsLink: ActionsFormLink | null = actionsToken === null || at.name === null ? null : { name: at.name, environment: at.env, token: actionsToken }
  const actionsTaken = () => {
    if (link.current !== null && link.current.view === viewKey) link.current = { view: viewKey, token: null }
  }
  // The wizard's hand-off: the form's link is minted for the destination and
  // name it chose, held for that view, and the page moves there.
  const openActions = (repo: string, name: string, environment: string | null, token: string) => {
    const next = viewOf({ scope: 'repository', repo, dest: 'actions', name, env: environment })
    link.current = { view: next, token }
    goView(next)
  }
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
  const [again, setAgain] = useState(0)
  const go = (next: Partial<SecretsAt>) => goView(viewOf(next))
  const back = (words: string | null = null) => {
    setDone(words)
    go({ scope: at.scope, repo: repoId, dest: at.dest })
  }

  const canManage = ov?.can_manage === true
  const formOpen = isForm(at)
  const mayAdd = canManage || at.scope === 'personal'
  // S4a: the workspace TypeSafe key, when the setup strip counts it missing.
  const setup = useContext(SetupContext)
  const setupItems = configSetupOf(dataOf(setup.doc))?.secrets?.items ?? []
  const keyNeeded = at.scope === 'workspace' && setupItems.some((i) => i.kind === 'provider_key' && i.name === 'typesafe')
  // The strip's step 3 and its next step, for the lead to follow (VQ-041).
  const steps = useSetupSteps()
  const strip: StripRead = { secrets: steps.find((s) => s.key === 'secrets') ?? null, next: setupNext(steps) }
  // S4i: nothing stored and nothing needed, for someone who may add one. Its
  // lead carries Add a secret, so the head does not draw it a second time.
  // A key set is something stored (VQ-041).
  const stored = ov !== null && (ov.rows.some((r) => r.section === 'agents') || keysSetOf(ov.rows).length > 0)
  const first = at.scope === 'workspace' && ov !== null && canManage && !keyNeeded && neededOf(ov.rows).length === 0 && !stored
  // ADD A SECRET FROM ITS ADDRESS (MCP-LEAD-FORM): once the overview says the
  // reader may add one and the catalogue has answered, open the sheet with
  // the address's consumer ticked if the catalogue lists it, and nothing
  // ticked otherwise. Once per arrival at the address, so closing it holds.
  const addOpened = useRef(false)
  const catalogueSettled = catalogue.state.status !== 'loading'
  const catalogueData = dataOf(catalogue.state)
  useEffect(() => {
    if (!at.add) {
      addOpened.current = false
      return
    }
    if (addOpened.current || formOpen || !mayAdd || !catalogueSettled) return
    addOpened.current = true
    const listed = at.consumer !== null && (catalogueData?.consumers ?? []).some((c) => c.consumer === at.consumer)
    setWizard({ scope: at.scope, repo: repoId, consumer: listed ? at.consumer : null })
  }, [at.add, at.consumer, at.scope, repoId, formOpen, mayAdd, catalogueSettled, catalogueData])
  const me = useSignedInEmail()
  return (
    <SignedInEmail.Provider value={me}>
    <div className="pk-page sec-page">
      {/* The head's Refresh and Add a secret are the secrets' own, so the
          logins tab, whose list is read by its own pane, offers neither. */}
      <PageHead title="Secrets">
        {!logins && <UrRefresh reads={[overview, repos]} ageOf={[overview]} />}
        {!logins && !formOpen && mayAdd && !first && (
          <Button kind="primary" size="sm" onClick={() => setWizard({ scope: at.scope, repo: repoId, consumer: null, dest: at.dest })}>
            Add a secret
          </Button>
        )}
      </PageHead>
      {/* `cfg-parts` (config.css): the Config pages' tab strip, as GitHub access draws it. */}
      <Tabs tabs={SECRETS_TABS} current={logins ? SECRETS_LOGINS_VIEW : 'secrets'} label="Secrets" className="cfg-parts" onSelect={(k) => goView(k === SECRETS_LOGINS_VIEW ? `view=${SECRETS_LOGINS_VIEW}` : '')} />
      {logins ? (
        <>
          {/* In a group workspace the pane says where logins live instead, so
              a lede promising "the sessions you saved" would contradict it. */}
          {(ov === null || isPersonalTenant(ov.tenant_id)) && (
            <p className="ur-sub">
              The browser sessions you saved from a live browser in your workspace, by name and site, with when each
              expires. An agent names one to start signed in.
            </p>
          )}
          <BrowserLoginsPane tenantId={ov?.tenant_id || null} />
        </>
      ) : formOpen ? (
        <OneTimeForm
          key={`${viewKey}#${again}`}
          at={at}
          repoId={repoId}
          known={known}
          overview={ov}
          personalSelected={personalSelected}
          catalogue={dataOf(catalogue.state)}
          draft={draft}
          takeLink={() => takeLink(viewKey)}
          onAgain={() => {
            // S4d's "Set another in this place": a fresh form, with its own
            // link, for the same store; a rotate's place takes an add.
            setDraft(null)
            setDone(null)
            setAgain((n) => n + 1)
            if (at.action === 'rotate') go({ scope: at.scope, repo: repoId, action: 'add' })
          }}
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
                  viewKey={viewKey}
                  overview={d}
                  repoId={repoId}
                  repoName={repoName}
                  actionsLink={actionsLink}
                  onActionsTaken={actionsTaken}
                  keyNeeded={keyNeeded}
                  first={first}
                  strip={strip}
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
      {!logins && wizard !== null && (
        <AddWizard
          start={wizard}
          repos={registered ?? []}
          tenant={ov?.tenant_id ?? null}
          personalSelected={at.scope === 'personal' ? personalSelected : null}
          catalogue={dataOf(catalogue.state)}
          onActions={(repo, name, environment, token) => {
            setWizard(null)
            openActions(repo, name, environment, token)
          }}
          onClose={() => {
            setWizard(null)
            // Leave Add a secret's address, so a reload does not reopen it.
            if (at.add) go({ scope: at.scope, repo: repoId, dest: at.dest })
          }}
          onOpen={(d) => {
            setWizard(null)
            setDraft(d)
            go({ scope: d.scope, repo: d.target.scope === 'repository' ? d.target.repoId : null, action: 'add' })
          }}
        />
      )}
    </div>
    </SignedInEmail.Provider>
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
  viewKey,
  overview,
  repoId,
  repoName,
  actionsLink,
  onActionsTaken,
  keyNeeded,
  first,
  strip,
  personalSelected,
  catalogue,
  onChange,
  go,
  onAdd,
}: {
  at: SecretsAt
  viewKey: string
  overview: SecretsOverview
  repoId: string | null
  /** `owner/repo` of the picked registration; null while unread. */
  repoName: string | null
  /** The Actions link this view opened, with its token; null without one. */
  actionsLink: ActionsFormLink | null
  onActionsTaken: () => void
  /** The setup read lists the workspace TypeSafe key as needed (S4a). */
  keyNeeded: boolean
  /** Nothing stored and nothing needed (S4i). */
  first: boolean
  /** The setup strip's step 3 and next step, which the lead follows. */
  strip: StripRead
  personalSelected: boolean
  catalogue: UrRead<WorkspaceSecretCatalogue>
  onChange: () => void
  go: (next: Partial<SecretsAt>) => void
  onAdd: (consumer: string | null) => void
}) {
  const rows = overview.rows
  const of = (section: string) => rows.filter((r) => r.section === section)
  const needed = neededOf(rows)
  const lead = { manages: overview.can_manage === true, tenant: overview.tenant_id, go, onAdd }
  if (at.scope === 'repository' && repoId !== null) {
    const repository = repoName ?? overview.repository ?? 'this repository'
    return (
      <>
        <NeedLead {...lead} rows={needed} keyNeeded={false} scope="repository" repository={repository} repoId={repoId} />
        <div className="sec-dest">
          <Segmented<Dest>
            label="Destination"
            value={at.dest}
            onChange={(dest) => go({ scope: 'repository', repo: repoId, dest })}
            options={[{ key: 'agents', label: 'Agents' }, { key: 'actions', label: 'GitHub Actions' }]}
          />
          {/* The pane's own lead says where an Actions secret is written. */}
          {at.dest === 'agents' && <span className="sec-hint">Released only to agents that clone {repository}.</span>}
        </div>
        {at.dest === 'actions' ? (
          <ActionsPane key={viewKey} repoId={repoId} repository={repository} link={actionsLink} canManage={overview.can_manage ?? null} onTaken={onActionsTaken} />
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
          <YoursList rows={of('agents')} personal={overview.personal_tenant_id} keys={keysSetOf(of('keys')).length} />
        )}
        <BrowserCard rows={of('browser_sessions')} removable={personalSelected} onChange={onChange} />
        <KeysCard rows={of('keys')} scope="personal" go={go} onChange={onChange} onAdd={onAdd} />
        <GitCard rows={of('github')} empty="No GitHub user token of yours is registered." />
      </>
    )
  }
  const keys = keysSetOf(rows)
  // S4a STACKS ITS THREE SECTIONS (VQ-042): For agents, Keys for agents and
  // MCP, then GitHub token, each full width. Beside the GitHub token, the
  // Keys table's four columns broke words mid-word and cut its Read by list.
  return (
    <>
      {first ? <FirstLead tenant={overview.tenant_id} step={strip.secrets} onAdd={() => onAdd(null)} />
        : needed.length > 0 || keyNeeded ? <NeedLead {...lead} rows={needed} keyNeeded={keyNeeded} scope="workspace" repository={null} repoId={null} />
          : strip.secrets?.state === 'done' ? <DoneLead tenant={overview.tenant_id} secrets={of('agents').length} keys={keys.length} next={strip.next} />
            : null}
      <StoreCard title="For agents" label="Workspace secrets" target={{ scope: 'workspace' }} overview={overview} catalogue={catalogue} onChange={onChange} go={go} at={at} empty={<FirstSecret tenant={overview.tenant_id} keys={keys} />} />
      <KeysCard rows={[...of('keys'), ...of('accounts')]} scope="workspace" agents={of('agents')} go={go} onChange={onChange} onAdd={onAdd} />
      <GitCard rows={of('github')} empty="No workspace token is registered. Connect one on GitHub access." />
    </>
  )
}

/** One thing the lead names: a row the overview reports, or the TypeSafe key the setup read lists. */
interface Need {
  /** In a sentence: "MCP server kb’s credential". */
  words: string
  /** What follows "is not set" when it is the only one. */
  why: string
  act: ReactNode
}

/**
 * The page's next action (S4a, S4h): what is needed and not set, and who sets
 * it. A member is told it is an admin's step, pointed to Yours, and offered a
 * note to send an admin, rather than shown buttons they cannot use.
 */
function NeedLead({ rows, keyNeeded, manages, scope, tenant, repository, repoId, go, onAdd }: {
  rows: SecretsRow[]
  keyNeeded: boolean
  manages: boolean
  scope: SecretsScope
  tenant: string
  repository: string | null
  repoId: string | null
  go: (n: Partial<SecretsAt>) => void
  onAdd: (c: string | null) => void
}) {
  const [copied, setCopied] = useState<string | null>(null)
  const needs: Need[] = rows.map((r) => r.section === 'qa_sign_in' ? {
    words: `the QA sign-in for ${r.name} ${r.environment ?? ''}`.trim(),
    why: 'QA runs there stay off until it is set.',
    act: <AppLink key={`${r.name}-${r.environment}`} address="config/repositories?page=qa-targets">Set it on QA targets</AppLink>,
  } : {
    words: `MCP server ${r.name}’s credential`,
    why: `Every call to ${r.name} fails until a secret is granted to ${r.consumer ?? `mcp:${r.name}`}.`,
    act: <Button key={r.name} kind="primary" size="sm" onClick={() => onAdd(r.consumer)}>{credentialLabel(r.name)}</Button>,
  })
  if (keyNeeded) {
    needs.push({
      words: 'the TypeSafe key for the workspace',
      why: 'Jev and browser agents read it, and Jev judges with TypeSafe here.',
      act: <Button key="typesafe" kind={needs.length === 0 ? 'primary' : 'secondary'} size="sm" onClick={() => go({ scope: 'workspace', key: 'typesafe' })}>Set TypeSafe key</Button>,
    })
  }
  if (needs.length === 0) return null
  const n = needs.length
  const whose = scope === 'repository' ? '' : 'workspace '
  const listed = needs.map((x) => x.words).join('; ')
  const where = scope === 'repository' ? `the repository ${repository ?? 'this repository'}` : `the workspace ${tenant}`
  async function copyNote() {
    const address = `${window.location.origin}${addressToPath(addressOf(viewOf({ scope, repo: repoId })))}`
    const note = `Needed and not set in ${where}: ${listed}. A workspace admin sets ${n === 1 ? 'it' : 'them'} on Config › Secrets: ${address}`
    try {
      await navigator.clipboard.writeText(note)
      setCopied('Note copied. Send it to a workspace admin.')
    } catch {
      setCopied('The note was not copied: this browser refused the clipboard.')
    }
  }
  const step = scope === 'repository' ? 'This repository' : 'Step 3'
  return (
    <section className="sec-lead is-warn" aria-label="Needed and not set">
      <span className="sec-k">{manages ? `${step} · ${scope === 'repository' ? `${n} missing` : 'next'}` : `${step} · an admin’s step`}</span>
      <b>
        {manages
          ? (n === 1 ? `${needs[0]!.words.replace(/^./, (c) => c.toUpperCase())} is not set` : `${n} ${whose}secrets are needed and not set`)
          : `${n === 1 ? `1 ${whose}secret is` : `${n} ${whose}secrets are`} needed: a workspace admin sets ${n === 1 ? 'it' : 'them'}`}
      </b>
      <span>
        {manages
          ? (n === 1 ? needs[0]!.why : `${listed.replace(/^./, (c) => c.toUpperCase())}. Each stays off until it is set.`)
          : `${listed.replace(/^./, (c) => c.toUpperCase())}. You can set your own under Yours; yours win for your agents.`}
      </span>
      <span className="sec-acts">
        {manages ? needs.slice(0, 2).map((x) => x.act) : (
          <>
            {scope !== 'personal' && <Button kind="primary" size="sm" onClick={() => go({ scope: 'personal' })}>Go to Yours</Button>}
            <Button kind="secondary" size="sm" onClick={() => void copyNote()}>Copy a note for an admin</Button>
          </>
        )}
      </span>
      {copied !== null && <span role="status">{copied}</span>}
    </section>
  )
}

/** What the lead reads off the setup strip: its step 3 and the step it calls next. */
interface StripRead {
  secrets: SetupStep | null
  next: SetupStep | null
}

/**
 * The empty workspace's lead (S4i): nothing stored, nothing needed, and the
 * one button. Its kicker is the strip's: where the strip measured step 3
 * done ("nothing missing"), the lead does not call it next.
 */
function FirstLead({ tenant, step, onAdd }: { tenant: string; step: SetupStep | null; onAdd: () => void }) {
  return (
    <section className="sec-lead" aria-label="Add your first secret">
      <span className="sec-k">{step?.state === 'done' ? 'Step 3 · nothing missing' : 'Step 3 · next'}</span>
      <b>Add your first secret</b>
      <span>Nothing is stored for {tenant || 'this workspace'} yet, and nothing registered needs one yet either.</span>
      <span className="sec-acts">
        <Button kind="primary" size="sm" onClick={onAdd}>Add a secret</Button>
      </span>
    </section>
  )
}

/**
 * Something stored and nothing missing, as the strip measured it (VQ-041):
 * step 3 is done, what is set, and the strip's next step as the one button.
 */
function DoneLead({ tenant, secrets, keys, next }: { tenant: string; secrets: number; keys: number; next: SetupStep | null }) {
  const what = secrets === 0
    ? `${keysAre(keys)} set; no secret for agents is stored yet.`
    : `${secrets === 1 ? '1 secret for agents' : `${secrets} secrets for agents`}${keys === 0 ? '' : ` and ${keys === 1 ? '1 key' : `${keys} keys`}`} ${secrets + keys === 1 ? 'is' : 'are'} set.`
  return (
    <section className="sec-lead is-ok" aria-label="Nothing missing">
      <span className="sec-k">Step 3 · done</span>
      <b>Nothing {tenant || 'this workspace'} needs is missing</b>
      <span>{what}</span>
      {next !== null && (
        <span className="sec-acts">
          <AppLink address={next.to} kind="primary">Next: {next.label}</AppLink>
        </span>
      )}
    </section>
  )
}

/**
 * For agents with nothing stored (S4i), and what people usually set first,
 * leaving out what is already set: a key set is named beside the empty list
 * rather than suggested (VQ-041).
 */
function FirstSecret({ tenant, keys }: { tenant: string; keys: readonly SecretsRow[] }) {
  const where = tenant || 'this workspace'
  const typesafe = keys.some((r) => r.store === 'provider_keys' && r.name === 'typesafe')
  return (
    <>
      <p className="pk-lead">
        <b>{keys.length === 0 ? `No secret is stored for ${where} yet.` : `No secret for agents is stored for ${where} yet; ${keysAre(keys.length)} set below.`}</b> Usually first:
      </p>
      <ul className="sec-first">
        {!typesafe && <li><b>The TypeSafe key</b><span>Jev and browser agents read it. Set it under Keys for agents and MCP.</span></li>}
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
function YoursList({ rows, personal, keys }: { rows: SecretsRow[]; personal: string | null; keys: number }) {
  const open = () => {
    if (personal === null) return
    chooseTenant(personal)
    goView('')
  }
  return (
    <Card title="For your agents">
      {rows.length === 0 ? (
        <p className="ur-small">
          {keys === 0 ? 'You have no secret of your own yet.' : `You have no secret of your own for agents yet; ${keysAre(keys)} yours, set under Keys below.`}
        </p>
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

/**
 * THE ONE LABEL FOR STORING AN MCP SERVER'S CREDENTIAL (visual QA pass 2,
 * config2-15): the Secrets lead said "Add a secret for kb", the keys table
 * "Add one" and MCP servers "Set kb’s credential" for the same action. All
 * three now read as MCP servers does.
 */
function credentialLabel(name: string): string {
  return `Set ${name}’s credential`
}

/**
 * Keys for agents and MCP (Settings › API keys until SSC-UI; BGD 2026-10-23 23:57Z).
 * `agents` is For agents' rows: a TypeSafe secret granted there is its own
 * record, set on its own date, so the TypeSafe key row says it is a separate
 * one rather than reading as the same key with a second date (config2-05).
 */
function KeysCard({ rows, scope, agents = [], go, onChange, onAdd }: { rows: SecretsRow[]; scope: SecretsScope; agents?: SecretsRow[]; go: (n: Partial<SecretsAt>) => void; onChange: () => void; onAdd: (c: string | null) => void }) {
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
      const twin = r.name === 'typesafe' ? agents.find((a) => a.provider === 'typesafe' || /typesafe/i.test(a.name)) : undefined
      return {
        key: <>{label}<small>{scope === 'personal' ? 'yours' : 'workspace'}</small>{twin !== undefined && <small>not the {twin.name} secret in For agents</small>}</>,
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
        // The API records neither when a runner key was set nor by whom
        // (`legacy_rows` lists the tenant's credentials only), so the line
        // says so beside "set" instead of reading as less than the TypeSafe
        // key's "set 5d ago by …" (chrome-C21).
        state: r.set_at === null && r.set_by === null
          ? <span className="sec-state is-ok">set · <Dash why="When and by whom a runner key was set is not recorded" /></span>
          : <span className="sec-state is-ok">{setWords(r)}</span>,
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
        act: r.can_manage && r.status !== 'set' ? <Button kind="primary" size="sm" onClick={() => onAdd(r.consumer)}>{credentialLabel(r.name)}</Button> : null,
      }
    }
    return {
      key: <>Subscription accounts</>,
      reads: 'the quota broker',
      state: <span className="sec-state">managed elsewhere</span>,
      act: <AppLink address="capacity/accounts">Capacity › Accounts</AppLink>,
    }
  }
  // THE HINT NAMES THE BUTTONS THE TABLE DRAWS (config2-14): it read "Was
  // Settings › API keys. Set and Replace open…", a page no reader today saw,
  // and a Set the table did not draw.
  const opens = Array.from(new Set(rows.flatMap((r) => !r.can_manage ? []
    : r.store === 'provider_keys' ? [r.status === 'set' ? 'Replace' : 'Set']
      : r.store === 'runner_provider_keys' ? ['Replace']
        : r.store === 'mcp_servers' && r.status !== 'set' ? [credentialLabel(r.name)] : [])))
  const hint = opens.length === 0
    ? 'No value is shown again once it is set.'
    : `${opens.length === 1 ? opens[0] : `${opens.slice(0, -1).join(', ')} and ${opens[opens.length - 1]}`} open${opens.length === 1 ? 's' : ''} the one-time form; no value is shown again.`
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
      <p className="sec-hint">{hint}</p>
      {deleting !== null && (
        <TypedConfirm title="Delete the TypeSafe key" name={deleting.name} verb="Delete" keep="Keep it" busy={busy ? 'Deleting…' : false}
          error={failed} onConfirm={() => void remove(deleting)} onClose={() => setDeleting(null)}>
          <p>Every version is destroyed. {scope === 'personal' ? 'Your submissions use the workspace’s key, if it has one.' : 'Jev and browser agents of this workspace lose it.'}</p>
        </TypedConfirm>
      )}
    </Card>
  )
}

/**
 * Whether a token's expiry has passed. AN EXPIRED TOKEN IS NOT DRAWN AS ACTIVE
 * (visual QA pass 1, config-21): the card read State "active" in the ok tone
 * beside Expires "9d ago". The expiry is the API's own figure, so the card
 * believes it over a state the record has not caught up with.
 */
function expired(iso: string | null): boolean {
  if (iso === null) return false
  const ms = Date.parse(iso)
  return !Number.isNaN(ms) && ms <= Date.now()
}

/** GitHub token status (§4): kind, state, login, verified, expires. Never any character of it. */
function GitCard({ rows, empty }: { rows: SecretsRow[]; empty: string }) {
  return (
    <Card title="GitHub token">
      {rows.length === 0 ? <p className="ur-small">{empty}</p> : rows.map((r) => (
        <dl key={r.name} className="sec-kv">
          <dt>Kind</dt><dd>{r.kind === null ? 'not said' : (TOKEN_KINDS[r.kind] ?? r.kind)}{r.token_scope === null ? '' : ` · ${r.token_scope === 'tenant' ? 'workspace' : r.token_scope === 'user' ? 'yours' : r.token_scope}`}</dd>
          {/* ONE STATE WITH GITHUB ACCESS (visual QA pass 2 config2-02): the
              word comes from `githubTokenState`, which GitHub access uses
              too, so an expired token on an active connection reads `active ·
              token renews on next use` on both pages. */}
          <dt>State</dt><dd>{(() => {
            const st = githubTokenState(r.status, r.expires_at, Date.now())
            return <span className={`sec-state${st.tone === 'ok' ? ' is-ok' : st.tone === 'warn' ? ' is-warn' : ''}`}>{st.word}</span>
          })()}</dd>
          <dt>GitHub login</dt><dd>{r.forge_login === null ? 'not said' : `@${r.forge_login}`}</dd>
          <dt>Verified</dt><dd>{r.verified_at === null ? 'never' : timeAgo(r.verified_at)}</dd>
          <dt>Expires</dt><dd title={r.expires_at ?? undefined}>{r.expires_at === null ? 'not said' : expired(r.expires_at)
            ? <span className="sec-state is-warn">expired {timeAgo(r.expires_at)}</span>
            : when(r.expires_at)}</dd>
        </dl>
      ))}
      <p className="sec-hint">
        Status only: no part of a token is ever shown. Connect or disconnect on <AppLink address="config/github">GitHub access</AppLink>.
      </p>
    </Card>
  )
}

function QaCard({ rows }: { rows: SecretsRow[] }) {
  const acts = rows.some((r) => r.can_manage)
  return (
    <Card title="Sign-in for QA">
      {rows.length === 0 ? (
        <p className="ur-small">No QA target of this repository holds a sign-in.</p>
      ) : (
        <table className="mcp-servers" aria-label="Sign-in for QA">
          <thead><tr><th scope="col">Target · environment</th><th scope="col">State</th>{acts && <th scope="col" aria-label="Actions" />}</tr></thead>
          <tbody>
            {rows.map((r) => {
              const what = `the QA sign-in for ${r.name} · ${r.environment ?? 'default'}${r.field === 'challenge_bypass' ? ', challenge bypass' : ''}`
              const verb = r.status === 'set' ? 'Replace' : 'Set'
              return (
                <tr key={`${r.target_id}-${r.environment}-${r.field}`} className={r.status === 'unset' ? 'is-need' : undefined}>
                  <th scope="row" data-label="Target" className="sec-key">
                    {r.name} · {r.environment ?? 'default'}
                    {r.field === 'challenge_bypass' && <small>challenge bypass</small>}
                  </th>
                  <td data-label="State">
                    {r.status === 'set' ? <span className="sec-state is-ok">{setWords(r)}</span> : <span className="sec-chip is-warn">needed, not set</span>}
                  </td>
                  {acts && (
                    <td data-label="Actions">
                      {/* The one-time form is QA targets' own, opened per entry there. */}
                      {r.can_manage && <AppLink address={QA_LINK_ADDRESS} label={`${verb} ${what} on QA targets`} kind={r.status === 'set' ? 'secondary' : 'primary'}>{verb}</AppLink>}
                    </td>
                  )}
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      <p className="sec-hint">Set and Replace open the target&rsquo;s one-time form on <AppLink address={QA_LINK_ADDRESS}>QA targets</AppLink>.</p>
    </Card>
  )
}

/**
 * THE GITHUB ACTIONS PANE (S4b2, S4f), SSC-UI-ACTIONS' `SecretsActions`. It
 * reads a link's token into its own ref as it first renders; once it has
 * mounted, this page lets its copy go, so the token is held in one place.
 */
function ActionsPane({ onTaken, ...pane }: { repoId: string; repository: string; link: ActionsFormLink | null; canManage: boolean | null; onTaken: () => void }) {
  const handed = pane.link !== null
  useEffect(() => {
    if (handed) onTaken()
    // Once, as it mounts: the pane is keyed by its view.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return <SecretsActions {...pane} />
}

// ---------------------------------------------------------------------------
// Add a secret: the four-step side sheet (S4c)
// ---------------------------------------------------------------------------

interface WizardStart {
  scope: SecretsScope
  repo: string | null
  /** A consumer to tick at step 3 (an MCP server that needs one). */
  consumer: string | null
  /** The destination step 2 starts on: the page's, when it is a repository's GitHub Actions. */
  dest?: Dest
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
  onActions,
}: {
  start: WizardStart
  repos: RepoRecord[]
  tenant: string | null
  /** Whether the selected workspace is the person's own; null when not yet known. */
  personalSelected: boolean | null
  catalogue: WorkspaceSecretCatalogue | null
  onClose: () => void
  onOpen: (d: Draft) => void
  /** The Actions path's hand-off: the pane's one-time form, with the link minted for it. */
  onActions: (repo: string, name: string, environment: string | null, token: string) => void
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
  const repoLabel = repoName ? `${repoName.owner}/${repoName.repo}` : 'a repository'
  const where = scope === 'repository' ? repoLabel : scope === 'personal' ? 'yours' : `the workspace${tenant === null ? '' : ` ${tenant}`}`
  const consumers: WorkspaceSecretConsumer[] = (catalogue?.consumers ?? []).filter((c) => GROUPS.some(([k]) => k === c.kind) || c.kind === 'mcp')
  const fits = consumers.map((c) => ({ c, cell: cellOf({ kind }, c) })).filter((x) => x.cell !== null)
  // THE GITHUB ACTIONS PATH (S4c, S4c3, S4c4): a repository's only. The
  // environment is chosen at step 2 (undefined until it is), from GitHub's
  // live list; the name follows GitHub's rule; step 4 mints the pane's
  // one-time form for exactly that destination and name.
  const [dest, setDest] = useState<Dest>(start.dest === 'actions' && start.scope === 'repository' ? 'actions' : 'agents')
  const actions = dest === 'actions' && scope === 'repository'
  const [env, setEnv] = useState<string | null | undefined>(undefined)
  const [list, setList] = useState<{ repo: string; read: Result<ActionsSecretList> } | null>(null)
  const [rules, setRules] = useState<Readonly<Record<string, ActionsEnvironmentRule>>>({})
  const [checking, setChecking] = useState(false)
  const [checkFailed, setCheckFailed] = useState<string | null>(null)
  const [minting, setMinting] = useState(false)
  const [mintFailed, setMintFailed] = useState<string | null>(null)
  useEffect(() => {
    if (!actions || repo === null || list?.repo === repo) return
    let live = true
    setEnv(undefined)
    setRules({})
    void loadActionsSecrets(repo).then((read) => {
      if (live) setList({ repo, read })
    })
    return () => {
      live = false
    }
  }, [actions, repo, list?.repo])
  const listed = list !== null && list.repo === repo ? dataOf(list.read) : null
  const listFailed = list !== null && list.repo === repo && list.read.status === 'error' ? list.read.error.message : null
  const ruleOf = (e: string | null | undefined): ActionsEnvironmentRule | null =>
    e === null || e === undefined ? null : rules[e] ?? listed?.environments.find((x) => x.environment === e)?.rule ?? null
  const createUrl = (env === null || env === undefined ? null : rules[env]?.create_url ?? null) ?? listed?.create_url ?? null
  const actionsWhere = env === null || env === undefined ? repoLabel : `${repoLabel} › ${env}`
  async function checkRule() {
    if (repo === null || env === null || env === undefined || checking) return
    setChecking(true)
    setCheckFailed(null)
    const r = await checkActionsRule(repo, env)
    setChecking(false)
    if (r.status === 'error') setCheckFailed(`The rule was not read: ${r.error.message}`)
    else if (r.status === 'ok' || r.status === 'stale') setRules((m) => ({ ...m, [env]: r.data }))
  }
  async function openActions() {
    if (repo === null || env === undefined || minting) return
    setMinting(true)
    setMintFailed(null)
    const r = await mintActionsLink(repo, name, env)
    setMinting(false)
    if (r.status === 'ok' || r.status === 'stale') {
      onActions(repo, name, env, r.data.token)
      return
    }
    setMintFailed(r.status === 'error' ? `The form was not opened: ${r.error.message}` : 'The form was not opened: the API answered without a link.')
  }
  const ok1 = scope !== 'repository' || repo !== null
  const ok2 = !actions || (env !== undefined && listed?.can_write !== false)
  const ok3 = actions ? name !== '' && nameProblem(name) === null : NAME_RULE.test(name)
  const wrongScope = !actions && scope === 'personal' && personalHere !== true
  const open = () => {
    if (actions) {
      if (ok1 && ok2 && ok3) void openActions()
      return
    }
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
            <Opt name={`${id}-dest`} on={!actions} title="Agents" why={scope === 'repository' ? `Agents that clone ${where}.` : 'Agents, tools and components it is granted to.'} onPick={() => setDest('agents')} />
            {scope === 'repository' && (
              <Opt name={`${id}-dest`} on={actions} title="GitHub Actions" why="Workflows in the repository, on GitHub." onPick={() => setDest('actions')} />
            )}
            {scope !== 'repository' && <p className="sec-hint">A GitHub Actions secret lives on one repository: pick Repository at step 1 for one.</p>}
            {actions && (
              <>
                <p className="sec-hint">
                  {listed?.can_write === true
                    ? 'GitHub Actions needs GitHub admin of the repository: checked, you are.'
                    : listed?.can_write === false
                      ? `${sentenceOf(listed.cannot_write_because ?? 'GitHub was asked and did not allow it')} Only a workspace manager whom GitHub calls admin of ${repoLabel} sets these.`
                      : 'GitHub Actions needs GitHub admin of the repository: SwarmCloud asks GitHub when the form opens and again at the write.'}
                </p>
                {listFailed !== null ? (
                  <Banner tone="bad" title={`${repoLabel}’s environments were not read: ${listFailed}`} role="alert" />
                ) : listed === null ? (
                  <p className="ur-small">Reading {repoLabel}&rsquo;s environments from GitHub…</p>
                ) : (
                  <fieldset className="sec-opts">
                    <legend>Environment</legend>
                    <Opt name={`${id}-env`} on={env === null} title="Repository level" why="Every workflow in the repository can read it." onPick={() => setEnv(null)} />
                    {listed.environments.map((e) => {
                      const r = ruleOf(e.environment)
                      return (
                        <Opt key={e.environment} name={`${id}-env`} on={env === e.environment} title={`${e.environment} · ${ruleWords(r)}`}
                          why={r !== null && r.main_only ? `Only workflows on ${r.branch}.` : `You will type ${e.environment} to confirm.`} onPick={() => setEnv(e.environment)} />
                      )
                    })}
                    <span className="sec-acts">
                      {createUrl !== null && <ButtonLink kind="secondary" size="sm" href={createUrl} target="_blank" rel="noreferrer">Create on GitHub ↗</ButtonLink>}
                      <Button kind="secondary" size="sm" busy={checking ? 'Checking…' : false} disabled={env === null || env === undefined}
                        title={env === null || env === undefined ? 'Pick an environment to read its rule' : undefined} onClick={() => void checkRule()}>Check rule</Button>
                    </span>
                    {env !== null && env !== undefined && rules[env] !== undefined && (
                      <p className="sec-hint" role="status"><b>{env}</b>: {ruleWords(rules[env]!)}, read from GitHub just now.</p>
                    )}
                    {checkFailed !== null && <Banner tone="bad" title={checkFailed} role="alert" />}
                    {listed.environments.length === 0 && <p className="sec-hint">No environment on {repoLabel} yet: create one on GitHub, then Check rule reads its branch rule.</p>}
                  </fieldset>
                )}
              </>
            )}
          </fieldset>
        )}
        {step === 3 && actions && (
          <div className="sec-opts">
            <p className="sec-hint">{readersOf(env ?? null, ruleOf(env), repoLabel)}</p>
            <span className="sec-field">
              <label htmlFor={`${id}-name`}>Name</label>
              <input id={`${id}-name`} className="ur-search" value={name} maxLength={200} spellCheck={false} autoCapitalize="off" autoCorrect="off"
                autoComplete="off" placeholder="MERGE_APP_PRIVATE_KEY" aria-describedby={`${id}-name-why`} aria-invalid={nameProblem(name) !== null}
                onChange={(e) => setName(e.target.value.trim())} />
              <span id={`${id}-name-why`} className="sec-hint">
                {nameProblem(name) ?? 'Letters, digits and _; not starting with a digit or GITHUB_. Never the value.'}
              </span>
            </span>
          </div>
        )}
        {step === 3 && !actions && (
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
        {step === 4 && actions && (
          <div className="sec-opts">
            <p className="sec-big">Set the value in a one-time form</p>
            <p className="sec-hint">
              Opens a form for <b>{name}</b> in {actionsWhere}. It works once, for ten minutes. The value is sealed in
              SwarmCloud, written to GitHub and never shown again; SwarmCloud keeps only the name, who set it and when.
            </p>
            <div className="c-banner is-info sec-term">
              <span>From a terminal instead: <code>{scLine(name, repoLabel, env ?? null)}</code></span>
            </div>
            {mintFailed !== null && <Banner tone="bad" title={mintFailed} role="alert" />}
          </div>
        )}
        {step === 4 && !actions && (
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
            <Button kind="primary" size="sm" disabled={(step === 1 && !ok1) || (step === 2 && !ok2) || (step === 3 && !ok3)} onClick={() => setStep(step + 1)}>Next</Button>
          ) : (
            <Button kind="primary" size="sm" busy={minting ? 'Opening…' : false} disabled={!ok1 || !ok2 || !ok3 || wrongScope} onClick={open}>Open the one-time form</Button>
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
  onAgain,
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
  /** S4d: a fresh one-time form for the same place. */
  onAgain: () => void
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
          <span className="sec-acts">
            <Button kind="secondary" size="sm" onClick={onAgain}>Set another in this place</Button>
            <Button kind="ghost" size="sm" onClick={onAnother}>Back to Secrets</Button>
          </span>
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
      <p className="sec-hint">8 bytes to 8 KiB. The form is cleared whatever the answer.</p>
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
