/**
 * REAL ROUTES (rebrand, owner decision 2026-10-01; navigation.html §C).
 *
 * The console's addresses are History-API paths now -- `/agents/live`,
 * `/capacity/pools/profiles`, `/help/reading-a-figure#absent-vs-zero` -- and
 * the hash router is gone. Two spellings meet here:
 *
 *   an ADDRESS  the router's internal key, which is the hash the old router
 *               canonicalised to (`work/task/<id>/attempts`, `capacity/profiles`).
 *               `fromAddress` in App.tsx resolves it, aliases and all, and it is
 *               what `go()` and every in-app `href="#..."` still name.
 *   a PATH      what the address bar shows and what a person copies.
 *
 * `addressToPath` and `pathToAddress` are inverses over every canonical
 * address, and `paths.test.ts` holds them to it -- and holds every legacy hash
 * (SECTION_ALIASES, LEGACY, MOVED_PANES) to the path it must land on.
 *
 * THE SERVER MUST SERVE index.html FOR EVERY PATH. images/swarm-ui/nginx.conf
 * already does (`try_files $uri $uri/ /index.html`); `/v1/*` never reaches it.
 */
import { AGENT_TABS, RECENT_STATES, agentList, agentListQuery, listFlags, parseAgentList, type AgentList, type AgentTab, type RecentState } from './agentlist'
import { HELP, HELP_GROUPS, HELP_ROUTE, type TopicId } from './help'

/** The non-section addresses this file knows by name. */
export const SUBMIT_ADDRESS = 'submit'
/**
 * API READS' OLD ADDRESS (NAV-FOOT, #1672): the page is Capacity's last tab,
 * `capacity/api-reads`, and App.tsx `fromAddress` sends this head there.
 */
export const REFERENCE_ADDRESS = 'reference'
/**
 * The paths API reads was served at before it became a Capacity page:
 * `/reference`, the hash router's own spelling, and `/api-reads`, the
 * rebrand's. Each lands on `/capacity/api-reads`, the bar rewritten once by
 * App's normalise, so a saved link still opens the page.
 */
const OLD_API_READS_PATHS: readonly string[] = ['/reference', '/api-reads']

/**
 * THE GITHUB APP'S CALLBACK (#780, OB3): the URL registered on the App,
 * https://swarm.example.com/onboarding/github/callback. Not a section and not in
 * FIXED -- nothing links to it, and the not-found page must never offer it.
 * ITS QUERY IS NEVER PART OF THE ADDRESS: GitHub's `code` and `state` ride on
 * it, and an address is what the router writes back into the bar.
 */
export const GITHUB_CALLBACK_ADDRESS = 'onboarding/github/callback'
export const GITHUB_CALLBACK_PATH = '/onboarding/github/callback'

/**
 * ADMIN › SETUP'S ONE-TIME LINK (DAYONE-SETUP-UI-5B; part of #2688): swarm-api
 * mints `/admin/setup/link/<token>` (`setup_links.LINK_FORM`) for a Claude
 * pool account's owner, a team's git slot or an add-on's secret, and the person
 * it names opens it. Not in FIXED and not behind the admin gate: the owner is
 * rarely a platform admin. ITS TOKEN IS NEVER PART OF AN ADDRESS: App reads it
 * off the bar once (`setupLinkToken`) and rewrites the bar to SETUP_LINK_PATH.
 */
export const SETUP_LINK_PATH = '/admin/setup/link'

/** The link's token, read off a path: only under SETUP_LINK_PATH, '' for a
 *  segment that is not one, null where the path is not the link's. */
export function setupLinkToken(pathname: string): string | null {
  const path = pathname.replace(/\/+$/, '')
  if (path === SETUP_LINK_PATH) return null
  if (!path.startsWith(`${SETUP_LINK_PATH}/`)) return null
  const seg = path.slice(SETUP_LINK_PATH.length + 1)
  return /^[A-Za-z0-9_-]+$/.test(seg) ? seg : ''
}

/** Whether a path is the link's page, with or without its token. */
export function isSetupLinkPath(pathname: string): boolean {
  const path = pathname.replace(/\/+$/, '')
  return path === SETUP_LINK_PATH || path.startsWith(`${SETUP_LINK_PATH}/`)
}

/**
 * THE TYPESAFE KEY FORM'S ONE-TIME LINK (KEYS-UI; docs/provider-keys.md §3):
 * `POST /v1/provider-keys/typesafe/links` answers
 * `/settings/keys/typesafe?scope=user|tenant&t=<token>`, and `/sc` hands that
 * to the person so the key never passes through Claude. Not in FIXED: nothing
 * in the console links to it and the not-found page must never offer it.
 * ITS TOKEN IS NEVER PART OF THE ADDRESS, exactly like the GitHub callback's
 * `code`: the address is what the router writes back into the bar, so the
 * token is dropped here and only `scope` is kept. App reads the token off the
 * bar once (`secretsLinkToken`) and hands it to Config › Secrets' key form.
 */
export const KEY_LINK_ADDRESS = 'settings/keys/typesafe'
export const KEY_LINK_PATH = '/settings/keys/typesafe'

/**
 * The link's token, read off a location: only on KEY_LINK_PATH, so a `t` on
 * any other page is never taken for one. App reads it once, before its
 * normalise effect rewrites the bar without it.
 */
export function keyLinkToken(pathname: string, search: string): string | null {
  if (pathname.replace(/\/+$/, '') !== KEY_LINK_PATH) return null
  const t = new URLSearchParams(search).get('t')
  return t === null || t === '' ? null : t
}

/**
 * SETTINGS › WORKSPACE › SECRETS AND ITS ONE-TIME LINK (lane SEC-UI-LIST,
 * part of #1347; docs/design/workspace-secrets.md §5). The page is a pane of
 * Settings › Workspace. `POST /v1/workspace/secrets/links` answers
 * `/settings/workspace/secrets?action=add|rotate[&name=<secret>]&t=<token>`
 * (swarm_api `WorkspaceSecrets.mint_link`), and `/sc` hands that to the person
 * so the value never passes through Claude. ITS TOKEN IS NEVER PART OF THE
 * ADDRESS, for the key link's reason: only `action` and, on a rotate, `name`
 * enter the route (`secretsLinkView`), in both directions, and the page reads
 * the token off the bar once (WorkspaceSecrets.tsx).
 */
export const SECRETS_ADDRESS = 'settings/workspace/secrets'
export const SECRETS_LINK_PATH = '/settings/workspace/secrets'
/**
 * CONFIG › SECRETS (CONFIG-NAV; part of #2157, BGD 2026-10-23): where the
 * page lives now. THE LINK KEEPS ITS PATH: the API still mints
 * SECRETS_LINK_PATH (swarm_api `WorkspaceSecrets.mint_link`), App reads its
 * token there before the bar is rewritten to this path, and the route keeps
 * the link's action and name and never its token, exactly as it did.
 */
export const CONFIG_SECRETS_ADDRESS = 'config/secrets'
export const CONFIG_SECRETS_PATH = '/config/secrets'

/** Config › Secrets' Browser logins tab, `?view=logins` (GA-MOVE-VIEWS). */
export const SECRETS_LOGINS_VIEW = 'logins'

/**
 * The link's token, read off a location: only on the secrets page's paths, as
 * `keyLinkToken`. Since SSC-UI (part of #2158) that includes the TypeSafe key
 * link's old path, which lands on Config › Secrets with its token: Settings ›
 * API keys moved into Keys for agents and MCP (BGD 2026-10-23 23:57Z).
 */
export function secretsLinkToken(pathname: string, search: string): string | null {
  const path = pathname.replace(/\/+$/, '')
  if (path !== SECRETS_LINK_PATH && path !== CONFIG_SECRETS_PATH && path !== KEY_LINK_PATH) return null
  const t = new URLSearchParams(search).get('t')
  return t === null || t === '' ? null : t
}

/**
 * THE PART OF A SECRETS-PAGE QUERY THE ROUTE KEEPS (SSC-UI, part of #2158;
 * docs/design/secrets-scopes.md §6.1): the scope picker's
 * `scope=workspace|personal|repository`, a repository's `repo` and its
 * destination `dest=agents|actions`; a form's `action=add|rotate` and, on a
 * rotate, `name` (an add link names no secret); the GitHub Actions link's
 * `name` and `env` (swarm_api `actions_secrets`); the runner-key link's
 * `runner_key=<provider>` (SSC-LINKS, BGD 2026-10-24); and `key=typesafe`,
 * the TypeSafe key form Settings › API keys used to be; and, when none of
 * those forms is named, `add=secret` with an optional `consumer` (Add a
 * secret, a consumer picked: `secretsAddAddress`); and, when none of
 * those is named at all, `view=logins`, the Browser logins tab
 * (GA-MOVE-VIEWS). NEVER `t`, and nothing else: an unknown key, or a value
 * of the wrong shape, is dropped, not carried. Always in this order, so one view has one spelling. '' for none.
 */
export function secretsLinkView(query: string): string {
  const given = new URLSearchParams(query)
  const kept = new URLSearchParams()
  const take = (key: string, ok: (v: string) => boolean): string | null => {
    const v = given.get(key)
    if (v === null || v === '' || !ok(v)) return null
    kept.set(key, v)
    return v
  }
  const scope = take('scope', (v) => v === 'workspace' || v === 'personal' || v === 'repository')
  const repo = scope === 'repository' ? take('repo', (v) => SECRETS_ID.test(v)) : null
  const dest = repo !== null ? take('dest', (v) => v === 'agents' || v === 'actions') : null
  if (dest === 'actions') {
    take('name', (v) => SECRETS_NAME.test(v))
    take('env', (v) => v.length <= 255)
    return kept.toString()
  }
  const action = take('action', (v) => v === 'add' || v === 'rotate')
  if (action === 'rotate') take('name', (v) => SECRETS_NAME.test(v))
  let form = action !== null
  if (action === null && scope !== 'repository') {
    form = take('runner_key', (v) => SECRETS_ID.test(v)) !== null || take('key', (v) => v === 'typesafe') !== null
  }
  if (!form && take('add', (v) => v === 'secret') !== null) take('consumer', (v) => SECRETS_CONSUMER.test(v))
  // Browser logins, the page's second tab (GA-MOVE-VIEWS), only when nothing
  // else is named: a scope, a link's form or an add is on the first tab.
  if (kept.toString() === '' && given.get('view') === SECRETS_LOGINS_VIEW) return `view=${SECRETS_LOGINS_VIEW}`
  return kept.toString()
}

/** A catalogue consumer's name, `<kind>:<name>` (`mcp:kb`, `profile:claude-code`); the page checks it against the catalogue. */
const SECRETS_CONSUMER = /^[a-z][a-z_-]{0,31}:[A-Za-z0-9_.-]{1,128}$/

/**
 * ADD A SECRET, A CONSUMER PICKED (lane MCP-LEAD-FORM, part of #2157; BGD
 * 2026-10-25, "Open the form, consumer picked"; proposal E S5a: the MCP
 * servers lead's button "opens setting it"). Config › Secrets with its Add a
 * secret sheet open and `consumer` ticked at Who reads it. NEVER
 * `?action=add`: that is the /sc one-time link's form, and the page then says
 * it was opened from one. Through `secretsLinkView`, so a bookmark survives;
 * a consumer the catalogue does not list opens the sheet with nothing ticked,
 * and a reader who may not add secrets gets the page with no sheet.
 */
export function secretsAddAddress(consumer: string | null): string {
  const q = new URLSearchParams({ add: 'secret' })
  if (consumer !== null) q.set('consumer', consumer)
  return `${CONFIG_SECRETS_ADDRESS}?${secretsLinkView(q.toString())}`
}

/** A registration id or a provider name, as the API mints them. */
const SECRETS_ID = /^[A-Za-z0-9_-]{1,64}$/
/** A store secret's or a GitHub Actions secret's name (the API checks it again). */
const SECRETS_NAME = /^[A-Za-z0-9_.-]{1,128}$/

/** The TypeSafe key link's old address (`settings/keys/typesafe?scope=user|tenant`) as its Config › Secrets view. */
function keyLinkView(query: string): string {
  const scope = keyLinkScope(query)
  return scope === null ? 'key=typesafe' : `scope=${scope === 'user' ? 'personal' : 'workspace'}&key=typesafe`
}

/**
 * THE QA TARGET CREDENTIAL FORM'S ONE-TIME LINK (lane VQ6-ROUTE, part of
 * #1933; docs/design/visual-qa-any-app.md §5): the plugin's
 * `swarm_qa_target_credential_link` hands a workspace admin
 * `/qa-targets/<id>/credential?t=<token>` (swarm_api `qatargets.mint_link`).
 * It lands on Config › Repositories › QA targets (QaTargets.tsx
 * `QA_TARGETS_PAGE`). Not in FIXED, for the key link's reasons, and ITS
 * TOKEN IS NEVER PART OF THE ADDRESS: neither the target id nor `t` enters the
 * route. App reads both off the bar once (qaTargetsApi.ts
 * `qaCredentialLinkFrom`) before its normalise effect rewrites it.
 */
export const QA_LINK_ADDRESS = 'config/repositories?page=qa-targets'
// A `field: "challenge_bypass"` link's `/qa-targets/<id>/challenge-bypass?t=..`
// lands there too, its form opened by the same capture (#2280).
const QA_LINK_PATH = /^\/qa-targets\/[^/]+\/(credential|challenge-bypass)$/

/**
 * ONE QA DISPATCH'S RUN PAGE (lane QAENV-UI-RUNPAGE, part of #2136;
 * docs/design/qa-environments.md §7): `/qa-runs/<group_id>`, one column per
 * leg. The API names this path in the issues a leg files (swarm_api
 * `qaspec._group_sentence`). The page sits under Config › Repositories › QA
 * targets, as Config proposal E's S9b draws its breadcrumb, so its address
 * rides on that tab's query as `qarun=<id>`, the way a repository does.
 */
export function qaRunAddress(groupId: string): string {
  return `config/repositories?${new URLSearchParams({ qarun: groupId }).toString()}`
}

/** The scopes a key link may name; anything else is dropped, not guessed at. */
export function keyLinkScope(query: string): 'user' | 'tenant' | null {
  const scope = new URLSearchParams(query).get('scope')
  return scope === 'user' || scope === 'tenant' ? scope : null
}

/** Address -> path for the panes that are one fixed path each. */
/** Exported for the not-found page, which offers the nearest of these (NotFound.tsx). */
export const FIXED: Readonly<Record<string, string>> = {
  'overview/now': '/overview',
  'work/running': '/agents',
  'work/workflows': '/workflows',
  'work/timeline': '/timeline',
  // Timeline's second page, today's outcome ledger (timeline.html pick A).
  'work/timeline/outcomes': '/timeline/outcomes',
  'work/new': '/submit/task',
  'work/new-workflow': '/submit/workflow',
  // intake-tenants.html 1A: the issue form, and Work › Runs (one run is
  // `/runs/<id>`, below).
  'work/new-issue': '/submit/issue',
  'work/runs': '/runs',
  // docs/schedules.md §6.1 (SD1): Automate › Schedules. One schedule is
  // `/automate/schedules/<id>`, below.
  'automate/schedules': '/automate/schedules',
  // §6.1: Approvals is Automate's second page, at `/automate/approvals` beside
  // `/automate/schedules` (APPROVALS-MOVE, BGD 2026-10-26, VQ-030). One item is
  // `/automate/approvals/<id>`, below; the old `/approvals` and
  // `/approvals/<id>` land on them (`pathToAddress`).
  'automate/approvals': '/automate/approvals',
  submit: '/submit',
  'capacity/pools': '/capacity/pools',
  'capacity/profiles': '/capacity/pools/profiles',
  'capacity/catalogue': '/capacity/runtimes',
  'capacity/holders': '/capacity/holders',
  'capacity/accounts': '/capacity/accounts',
  // ACCOUNT-HEALTH-PAGE (part of #2302): Capacity › Account health.
  'capacity/account-health': '/capacity/account-health',
  'capacity/quota': '/capacity/accounts/quota',
  // NAV-FOOT (#1672): API reads, Capacity's last page. It was `/api-reads`
  // at the spine's foot; that path and `/reference` still land here.
  'capacity/api-reads': '/capacity/api-reads',
  'admin/limits': '/admin/limits',
  // SIZES-UI (part of #1689): Admin › Runner sizes.
  'admin/runner-sizes': '/admin/runner-sizes',
  'admin/tenants': '/admin/tenants',
  'admin/counts': '/admin/counts',
  // SSC-UI (part of #2158; docs/design/secrets-scopes.md §5): Admin › Platform
  // secrets, a platform admin's alone, status only.
  'admin/platform-secrets': '/admin/platform-secrets',
  // DAYONE-SETUP-UI-3 (part of #2688; docs/design/day-one-plan.md §5.1):
  // Admin › Setup, where swarm install hands the people part of an install
  // over to the console, and Health, the scheduled doctor's findings, under
  // it. The GitHub App callback is the page GitHub sends the person back to
  // with `?code=&state=`; FIXED_BACK keeps that query on the way in, and the
  // route drops it from the bar once the page has read it (App.tsx).
  'admin/setup': '/admin/setup',
  'admin/health': '/admin/setup/health',
  'admin/setup/github-app/callback': '/admin/setup/github-app/callback',
  // #847 W8: Admin › People, everyone who has signed in and their workspace requests.
  'admin/people': '/admin/people',
  // NAV-ADMIN-ROLE (part of #1633): Admin › Workspaces you admin, the group
  // workspaces whose stored role for the caller is admin.
  'admin/workspaces': '/admin/workspaces',
  // CONFIG (CONFIG-NAV; part of #2157, BGD 2026-10-23): everything about
  // setting up and running a workspace, in BGD's order. Each page was
  // somewhere else until then -- Work › Access, Work › Repositories, Work ›
  // Setup and three Settings pages -- and its old path and its old address
  // still land here (OLD_CONFIG_PATHS, MOVED_TO_CONFIG, below). A
  // repository's pages are `/config/repositories/<id>/<tab>`, Secrets' link
  // view is CONFIG_SECRETS_PATH, and Workspace setup is
  // `/config/workspace/setup`, all below.
  'config/github': '/config/github',
  'config/repositories': '/config/repositories',
  // A literal, as every row here is: the issue-form test reads this table as text. It is CONFIG_SECRETS_PATH.
  'config/secrets': '/config/secrets',
  'config/mcp-servers': '/config/mcp-servers',
  'config/people': '/config/people',
  'config/workspace': '/config/workspace',
  // Settings › API keys was here (KEYS-UI) until SSC-UI (part of #2158) moved
  // it into Config › Secrets as Keys for agents and MCP (BGD 2026-10-23
  // 23:57Z); `/settings/keys` and its link land there (OLD_CONFIG_PATHS).
  // PROFILE-NAV: Settings › Profile, the person's own identity, workspaces,
  // connections, bell and Claude accounts (docs/design/user-profile.md).
  // Settings keeps it and nothing else since SSC-UI.
  'settings/profile': '/settings/profile',
  // VOICE IT (docs/design/voice-it.md §10, phase one): its three panes, one
  // path each. `/voice` opens Talk (SECTION_ROOTS).
  'voice/talk': '/voice/talk',
  'voice/projects': '/voice/projects',
  'voice/preferences': '/voice/preferences',
  [HELP_ROUTE]: '/help',
}

const FIXED_BACK: Readonly<Record<string, string>> = Object.fromEntries(
  Object.entries(FIXED).map(([a, p]) => [p, a]),
)

/** Config › Workspace Settings' Workspace setup part: today's Work › Setup, unchanged. */
export const WORKSPACE_SETUP_ADDRESS = 'config/workspace?part=setup'
const WORKSPACE_SETUP_PATH = '/config/workspace/setup'

/**
 * THE ADDRESSES THE CONFIG PAGES HAD BEFORE CONFIG-NAV (part of #2157), old
 * spelling to new. The pages themselves still name some of these (their own
 * constants, which later lanes move), and so do saved `#...` links: App's
 * `fromAddress` and this file's `addressToPath` both rewrite through here, so
 * an old address opens the new page and is written as its new path. The
 * query an old address carries goes with it -- a repository, a page of the
 * registry, the Access view -- except the secrets link's, which keeps its
 * action and name and nothing else (`secretsLinkView`), and Setup's, which
 * read none.
 */
const MOVED_TO_CONFIG: Readonly<Record<string, string>> = {
  'work/access': 'config/github',
  'work/repositories': 'config/repositories',
  'work/setup': WORKSPACE_SETUP_ADDRESS,
  'settings/workspace': 'config/workspace',
  [SECRETS_ADDRESS]: CONFIG_SECRETS_ADDRESS,
  'settings/mcp-servers': 'config/mcp-servers',
  'settings/people': 'config/people',
  // Settings › API keys and its TypeSafe link (SSC-UI): Keys for agents and MCP.
  'settings/keys': CONFIG_SECRETS_ADDRESS,
  [KEY_LINK_ADDRESS]: CONFIG_SECRETS_ADDRESS,
}

/**
 * GITHUB ACCESS' TWO OLD VIEWS, EACH ON THE PAGE WHOSE SUBJECT IT IS
 * (GA-MOVE-VIEWS; part of #2157, BGD 2026-10-24: "Members to Config › People
 * and roles, Browser logins to Config › Secrets"). GitHub access is mock-up
 * S2's two tabs and nothing else since; `?view=members` and `?view=logins`
 * there -- and on `/access` and `#work/access` before it -- name these.
 */
const MOVED_VIEWS: ReadonlyMap<string, string> = new Map([
  ['members', 'config/people?view=members'],
  [SECRETS_LOGINS_VIEW, `${CONFIG_SECRETS_ADDRESS}?view=${SECRETS_LOGINS_VIEW}`],
])

/** An old Config page's address, rewritten to its new one; null for any other address. */
export function movedToConfig(address: string): string | null {
  const q = address.indexOf('?')
  const bare = q === -1 ? address : address.slice(0, q)
  const query = q === -1 ? '' : address.slice(q + 1)
  const view = MOVED_VIEWS.get(new URLSearchParams(query).get('view') ?? '')
  if (view !== undefined && (bare === 'config/github' || bare === 'work/access')) return view
  const to = MOVED_TO_CONFIG[bare]
  if (to === undefined) return null
  if (bare === SECRETS_ADDRESS || bare === 'settings/keys') {
    const view = secretsLinkView(query)
    return view === '' ? to : `${to}?${view}`
  }
  if (bare === KEY_LINK_ADDRESS) return `${to}?${keyLinkView(query)}`
  return query === '' || to.includes('?') ? to : `${to}?${query}`
}

/**
 * THE PATHS THE CONFIG PAGES HAD, each landing on its new address, the bar
 * rewritten once by App's normalise (CONFIG-NAV). Bookmarks, issue links,
 * the plugin's console links and the API's notification links
 * (`/access`, `/setup#workspace`) name these. `/repositories/<id>/...` and the
 * secrets link's path are read below, with their tails.
 */
const OLD_CONFIG_PATHS: Readonly<Record<string, string>> = {
  '/access': 'config/github',
  '/repositories': 'config/repositories',
  '/setup': WORKSPACE_SETUP_ADDRESS,
  '/settings/workspace': 'config/workspace',
  '/settings/mcp-servers': 'config/mcp-servers',
  '/settings/people': 'config/people',
  '/settings/keys': CONFIG_SECRETS_ADDRESS,
}

/**
 * A SECTION'S ROOT OPENS ITS FIRST PAGE (owner QA R14, 2026-10-04): `/capacity`
 * and `/admin` said "No page at /capacity", though each is a section the spine
 * names. One way only -- the address goes back to the page's own path, so the
 * bar is rewritten to `/capacity/pools` (App's one-time redirect) and no page
 * has two spellings. `/agents` and `/overview` are pages already.
 */
export const SECTION_ROOTS: Readonly<Record<string, string>> = {
  '/work': 'work/running',
  '/automate': 'automate/schedules',
  '/capacity': 'capacity/pools',
  '/admin': 'admin/limits',
  '/config': 'config/github',
  '/voice': 'voice/talk',
  // Not a section, but `/settings` is the head of Profile's path, and a person
  // trimming the link back to it should land on the page rather than a 404.
  '/settings': 'settings/profile',
}

/**
 * WORK'S TABS BY THEIR NAMES UNDER `/work` (visual QA pass 2 runs2-14, was
 * runs-submit-32, part of #2694): `/work` opens Work, so a person who types
 * `/work/runs` for the Runs tab was told "Not found". Each tab's name, as the
 * spine writes it, lands on the tab -- one way only, like a section root: the
 * address goes back to the tab's own path (`/runs`), the bar rewritten once by
 * App's normalise, so no page gains a second spelling. `/work/running` is the
 * internal address, not a name anyone reads, and stays a missing page.
 */
const WORK_TAB_PATHS: Readonly<Record<string, string>> = {
  '/work/agents': 'work/running',
  '/work/workflows': 'work/workflows',
  '/work/runs': 'work/runs',
  '/work/timeline': 'work/timeline',
}

const GROUP_IDS: readonly string[] = HELP_GROUPS.map((g) => g.id)

/** The Help group a topic is drawn under, or null for a topic this build lacks. */
export function helpGroupOf(topic: string): string | null {
  const t = (HELP as Readonly<Record<string, { group: string } | undefined>>)[topic as TopicId]
  return t === undefined ? null : t.group
}

/**
 * `changes` is the agent's Changes tab (design docs/design/diff-viewer.md §2
 * variant 2): `/agents/<tab>/<id>/changes`, and `/changes/<file>` with the open
 * file as ONE encoded segment, so a link can name the file it is about.
 * `browser` is the live browser's tab (live-browser.md §6, LB-UX1):
 * `/agents/<tab>/<id>/browser`, and `?theater=1` there opens the theater.
 */
export const TASK_PANES = ['logs', 'children', 'attempts', 'artifacts', 'checkpoints', 'changes', 'browser'] as const

function isAgentTab(s: string | undefined): s is AgentTab {
  return (AGENT_TABS as readonly string[]).includes(s ?? '')
}

function isRecentState(s: string | null): s is RecentState {
  return (RECENT_STATES as readonly string[]).includes(s ?? '')
}

/**
 * A list's query on a path: `state=failed&group=wf&first=failed` (G2-22). The
 * state is the address's last segment and a query key on the path, as it
 * always was; the toggles are a query on both.
 *
 * EXPORTED FOR AN OPEN AGENT'S PATH, which carries the list it was opened
 * from: `/agents/recent/<id>?state=failed&group=wf`. Opening an agent from
 * `Recent · failed` wrote `/agents/recent/<id>`, so a reload or a shared link
 * landed beside the unfiltered list. `pathToAddress` reads it back as `list`.
 */
export function agentListSearch(list: AgentList): string {
  const q = new URLSearchParams()
  if (list.tab === 'recent' && list.state !== null) q.set('state', list.state)
  for (const [k, v] of new URLSearchParams(agentListQuery(list))) q.append(k, v)
  return q.toString()
}

/** The list a path's query names, with the tab the path carries. */
function listFromQuery(tab: AgentTab, query: string): AgentList {
  const state = new URLSearchParams(query).get('state')
  return agentList(tab, isRecentState(state) ? state : null, listFlags(query))
}


/**
 * The panes of one workflow that are a path segment: `/workflows/<id>/<pane>`.
 * `changes` is the workflow's Changes tab (diff-viewer.md §2 variants 2 + 5).
 */
export const WORKFLOW_PANES: readonly string[] = ['table', 'timeline', 'changes']

/** The panes of one issue run that are a path segment: `/runs/<id>/changes`. Its page is the bare id. */
export const RUN_PANES: readonly string[] = ['changes']
/**
 * The query a run's Changes tab carries: the matrix's step filter and its
 * open file (`/runs/<id>/changes?step=fix&file=src%2Fa.ts`, diff-viewer.md §2
 * variant 5). A workflow's carries the same two through its filters.
 */
const RUN_PANE_QUERY: readonly string[] = ['step', 'file']

/** Config › Repositories' path (CONFIG-NAV); it was `/repositories`, which still lands here. */
const REPOSITORIES_PATH = '/config/repositories'

/** A repository's tabs that are a path segment: `/config/repositories/<id>/<tab>`; Overview is the bare id. */
export const REPO_TABS: readonly string[] = ['graph', 'impact', 'test-map', 'hot-spots', 'index-runs', 'settings', 'used-by']
/**
 * The query a repository tab carries in the bar: the pull request the Impact
 * tab opens on, and the Graph view and symbol search a Test map row opens
 * (`view=tests&q=<directory>`, QA G4-09).
 */
const REPO_TAB_QUERY: readonly string[] = ['pr', 'view', 'q']

/**
 * The path for an address. `agentTab` is the list an open agent sits in
 * (`/agents/<tab>/<id>`), which the address does not carry; it defaults to
 * `live`, the list a pasted agent link is most often from.
 *
 * The list's state and toggles ride on an open agent's path as its query
 * (`agentListSearch`, G2-22), which `App` appends: this signature is the one
 * `swarm_api.codec.agent_console_url` is held to.
 */
export function addressToPath(address: string, agentTab: AgentTab = 'live'): string {
  // An old Config page's address is written as its new path, never its old one.
  const moved = movedToConfig(address)
  if (moved !== null) return addressToPath(moved, agentTab)
  const q = address.indexOf('?')
  const bare = q === -1 ? address : address.slice(0, q)
  const query = q === -1 ? '' : address.slice(q + 1)
  const seg = bare.split('/')

  // One agent: `work/task/<id>[/<pane>]` -> `/agents/<tab>/<id>[/<pane>]`.
  if (seg[0] === 'work' && seg[1] === 'task' && seg.length > 2) {
    return `/agents/${agentTab}/${seg.slice(2).join('/')}`
  }
  // The agent list: `work/running/recent/failed?group=wf` ->
  // `/agents/recent?state=failed&group=wf`.
  if (seg[0] === 'work' && seg[1] === 'running' && isAgentTab(seg[2])) {
    const list = parseAgentList(seg.slice(2), query)
    if (list === null) return seg[3] === undefined ? `/agents/${seg[2]}` : `/agents/${seg[2]}?state=${seg[3]}`
    const search = agentListSearch(list)
    return search === '' ? `/agents/${list.tab}` : `/agents/${list.tab}?${search}`
  }
  // One workflow rides on the list's query as `wf=<id>`; the rest is its filters.
  if (bare === 'work/workflows' && query !== '') {
    const params = new URLSearchParams(query)
    const wf = params.get('wf')
    params.delete('wf')
    // One workflow's Table or Timeline tab is a path segment of its own:
    // `/workflows/<id>/timeline` (workflows.html, section F). Only on a workflow.
    const tab = params.get('tab')
    const hasWf = wf !== null && wf !== ''
    const pane = hasWf && tab !== null && WORKFLOW_PANES.includes(tab) ? `/${tab}` : ''
    if (pane !== '') params.delete('tab')
    const rest = params.toString()
    const base = wf !== null && wf !== '' ? `/workflows/${encodeURIComponent(wf)}${pane}` : '/workflows'
    return rest === '' ? base : `${base}?${rest}`
  }
  // One issue run rides on the Runs list's query as `run=<id>`: `/runs/<id>`,
  // and its Changes tab as `tab=changes`: `/runs/<id>/changes`.
  if (bare === 'work/runs' && query !== '') {
    const params = new URLSearchParams(query)
    const run = params.get('run')
    const tab = params.get('tab')
    if (run !== null && run !== '' && tab !== null && RUN_PANES.includes(tab)) {
      // The Changes tab's step filter and open file ride on its query.
      const rest = new URLSearchParams()
      for (const k of RUN_PANE_QUERY) {
        const v = params.get(k)
        if (v !== null && v !== '') rest.set(k, v)
      }
      const path = `/runs/${encodeURIComponent(run)}/${tab}`
      return rest.toString() === '' ? path : `${path}?${rest.toString()}`
    }
    // The run's page: the worker's issue comment links here (swarm_api
    // `run_console_url`, held to this line by test_issue_writeback.py).
    if (run !== null && run !== '') return `/runs/${encodeURIComponent(run)}`
  }
  // One schedule rides on the list's query as `schedule=<id>`: `/automate/schedules/<id>`.
  // Its tab rides as `?tab=`, and its editor is a segment, `/edit` (AUT-11).
  if (bare === 'automate/schedules' && query !== '') {
    const q = new URLSearchParams(query)
    const id = q.get('schedule')
    if (id !== null && id !== '') {
      const base = `/automate/schedules/${encodeURIComponent(id)}${q.get('edit') === '1' ? '/edit' : ''}`
      const tab = q.get('tab')
      return tab === null || tab === '' ? base : `${base}?${new URLSearchParams({ tab }).toString()}`
    }
  }
  // One inbox item rides on the page's query as `approval=<id>`: `/automate/approvals/<id>`,
  // keeping the inbox's kind filter as `?kind=` (AUT-15).
  if (bare === 'automate/approvals' && query !== '') {
    const q = new URLSearchParams(query)
    const id = q.get('approval')
    if (id !== null && id !== '') {
      const kind = q.get('kind')
      const base = `/automate/approvals/${encodeURIComponent(id)}`
      return kind === null || kind === '' ? base : `${base}?${new URLSearchParams({ kind }).toString()}`
    }
  }
  // Config › Repositories (repositories.html): the page rides on the tab's query.
  if (bare === 'config/repositories' && query !== '') {
    const q = new URLSearchParams(query)
    const qarun = q.get('qarun')
    if (qarun !== null && qarun !== '') return `/qa-runs/${encodeURIComponent(qarun)}`
    const page = q.get('page')
    const repo = q.get('repo')
    // Register keeps the repository a dashed card picked (REPOS-FOLLOWUPS), so
    // a reload or a shared link opens it on the same repository.
    if (page === 'register') return repo === null || repo === '' ? `${REPOSITORIES_PATH}/register` : `${REPOSITORIES_PATH}/register?${new URLSearchParams({ repo }).toString()}`
    if (page === 'tokens') return `${REPOSITORIES_PATH}/tokens`
    if (page === 'permissions') return `${REPOSITORIES_PATH}/tokens/permissions`
    if (repo !== null && repo !== '') {
      const tab = q.get('tab')
      const base = `${REPOSITORIES_PATH}/${encodeURIComponent(repo)}`
      if (tab === null || !REPO_TABS.includes(tab)) return base
      const rest = new URLSearchParams()
      for (const k of REPO_TAB_QUERY) {
        const v = q.get(k)
        if (v !== null) rest.set(k, v)
      }
      return rest.toString() === '' ? `${base}/${tab}` : `${base}/${tab}?${rest.toString()}`
    }
  }
  // Help: a topic lands at its group's page, scrolled to it; a group is a page.
  if (seg[0] === HELP_ROUTE && seg.length > 1) {
    const tail = seg.slice(1).join('/')
    if (GROUP_IDS.includes(tail)) return `/help/${tail}`
    const group = helpGroupOf(tail)
    // An unknown topic keeps its name, so the page can say which was asked for.
    return group === null ? `/help/${tail}` : `/help/${group}#${tail}`
  }
  if (bare === GITHUB_CALLBACK_ADDRESS) return GITHUB_CALLBACK_PATH
  // The secrets page keeps its action and name and nothing else, so an
  // address that carries a `t` by hand still writes no token into the bar.
  if (bare === CONFIG_SECRETS_ADDRESS) {
    const view = secretsLinkView(query)
    return view === '' ? CONFIG_SECRETS_PATH : `${CONFIG_SECRETS_PATH}?${view}`
  }
  // Workspace Settings' Workspace setup part is a path of its own.
  if (bare === 'config/workspace' && new URLSearchParams(query).get('part') === 'setup') return WORKSPACE_SETUP_PATH
  const fixed = FIXED[bare]
  if (fixed !== undefined) return query === '' ? fixed : `${fixed}?${query}`
  return '/overview'
}

export interface PathRoute {
  /** The address to resolve, in the router's internal spelling. */
  address: string
  /** For an agent path, the list it was opened from. */
  agentTab: AgentTab | null
  /**
   * For an agent path whose query names the list's state or toggles, that
   * whole list (G2-22). Absent when the query names neither, so the list is
   * `agentTab` alone.
   */
  list?: AgentList
}

/**
 * The address for a path, or null for a path this console does not serve
 * (the router then lands on Overview, and rewrites the bar to say so).
 */
export function pathToAddress(pathname: string, search = '', hash = ''): PathRoute | null {
  const path = pathname.replace(/\/+$/, '') || '/'
  const query = search.replace(/^\?/, '')
  const seg = path.split('/').slice(1)
  const plain = (address: string): PathRoute => ({ address, agentTab: null })

  if (path === '/') return plain('overview/now')

  if (seg[0] === 'agents') {
    const tab = seg[1]
    if (isAgentTab(tab)) {
      const rest = seg.slice(2)
      const list = listFromQuery(tab, query)
      if (rest.length > 0) {
        const named = list.state !== null || agentListQuery(list) !== ''
        // THE THEATER RIDES ON THE BROWSER TAB'S QUERY (live-browser.md §6):
        // the graph card, the hand-off banner and a shared link open
        // `/agents/<tab>/<id>/browser?theater=1`. The rest of the query is
        // the list's, read above.
        const theater = rest[rest.length - 1] === 'browser' && new URLSearchParams(query).get('theater') === '1'
        const address = `work/task/${rest.join('/')}`
        return { address: theater ? `${address}?theater=1` : address, agentTab: tab, ...(named ? { list } : {}) }
      }
      // The state is kept as written, as it always was: a misspelt one lands
      // on the plain list through `parseAgentList`, not here.
      const state = new URLSearchParams(query).get('state')
      const flags = agentListQuery(list)
      const at = state === null || tab !== 'recent' ? `work/running/${tab}` : `work/running/${tab}/${state}`
      return plain(flags === '' ? at : `${at}?${flags}`)
    }
    return seg.length === 1 ? plain('work/running') : null
  }

  if (seg[0] === 'workflows' && seg.length >= 2) {
    // `wf`, then the tab a trailing `/table` or `/timeline` names, then the
    // filters in the order the path carried them, which is the order
    // `addressToPath` took them off: the two stay exact inverses.
    const last = seg[seg.length - 1] ?? ''
    const pane = seg.length >= 3 && WORKFLOW_PANES.includes(last) ? last : null
    const idSegs = pane === null ? seg.slice(1) : seg.slice(1, -1)
    const params = new URLSearchParams({ wf: decodeURIComponent(idSegs.join('/')) })
    if (pane !== null) params.append('tab', pane)
    for (const [k, v] of new URLSearchParams(query)) if (k !== 'wf' && (pane === null || k !== 'tab')) params.append(k, v)
    return plain(`work/workflows?${params.toString()}`)
  }

  if (seg[0] === 'runs' && seg.length >= 2) {
    // A trailing `/changes` is the run's Changes tab, as on a workflow.
    const last = seg[seg.length - 1] ?? ''
    const pane = seg.length >= 3 && RUN_PANES.includes(last) ? last : null
    const idSegs = pane === null ? seg.slice(1) : seg.slice(1, -1)
    const params = new URLSearchParams({ run: decodeURIComponent(idSegs.join('/')) })
    if (pane !== null) {
      params.set('tab', pane)
      const given = new URLSearchParams(query)
      for (const k of RUN_PANE_QUERY) {
        const v = given.get(k)
        if (v !== null && v !== '') params.set(k, v)
      }
    }
    return plain(`work/runs?${params.toString()}`)
  }

  if (seg[0] === 'automate' && seg[1] === 'schedules' && seg[2] !== undefined && seg[2] !== '' && (seg.length === 3 || (seg.length === 4 && seg[3] === 'edit'))) {
    const q = new URLSearchParams({ schedule: decodeURIComponent(seg[2]) })
    const tab = new URLSearchParams(query).get('tab')
    if (tab !== null && tab !== '') q.set('tab', tab)
    if (seg.length === 4) q.set('edit', '1')
    return plain(`automate/schedules?${q.toString()}`)
  }

  // One inbox item, `/automate/approvals/<id>`, and the same item at its old
  // address, `/approvals/<id>` (APPROVALS-MOVE, VQ-030): a notification or a
  // saved link still opens the item, the bar rewritten once by App's normalise.
  const approvalId = seg[0] === 'automate' && seg[1] === 'approvals' && seg.length === 3 ? seg[2] : seg[0] === 'approvals' && seg.length === 2 ? seg[1] : undefined
  if (approvalId !== undefined && approvalId !== '') {
    const q = new URLSearchParams({ approval: decodeURIComponent(approvalId) })
    const kind = new URLSearchParams(query).get('kind')
    if (kind !== null && kind !== '') q.set('kind', kind)
    return plain(`automate/approvals?${q.toString()}`)
  }

  if (seg[0] === 'qa-runs' && seg.length === 2 && seg[1] !== '') return plain(qaRunAddress(decodeURIComponent(seg[1]!)))

  // A repository's pages: `/config/repositories/<id>/...`, and the same tail
  // under `/repositories`, where they were until CONFIG-NAV.
  const repoTail = seg[0] === 'repositories' ? seg.slice(1) : seg[0] === 'config' && seg[1] === 'repositories' ? seg.slice(2) : []
  if (repoTail.length >= 1) {
    const at = (q: Record<string, string>) => plain(`config/repositories?${new URLSearchParams(q).toString()}`)
    if (repoTail[0] === 'register' && repoTail.length === 1) {
      const picked = new URLSearchParams(query).get('repo')
      return picked === null || picked === '' ? at({ page: 'register' }) : at({ page: 'register', repo: picked })
    }
    if (repoTail[0] === 'tokens') return repoTail[1] === 'permissions' ? at({ page: 'permissions' }) : at({ page: 'tokens' })
    const repo = decodeURIComponent(repoTail[0] ?? '')
    const tab = repoTail[1]
    if (tab === undefined || !REPO_TABS.includes(tab)) return at({ repo })
    const extra: Record<string, string> = {}
    const given = new URLSearchParams(query)
    for (const k of REPO_TAB_QUERY) {
      const v = given.get(k)
      if (v !== null) extra[k] = v
    }
    return at({ repo, tab, ...extra })
  }

  if (seg[0] === 'help' && seg.length >= 2) {
    const topic = hash.replace(/^#/, '')
    // `/help/<group>#<topic>` names the topic; `/help/<group>` the page.
    return plain(topic !== '' ? `${HELP_ROUTE}/${topic}` : `${HELP_ROUTE}/${seg.slice(1).join('/')}`)
  }

  // The callback's query (`code`, `state`, `error`) is dropped HERE, so the
  // route never holds it and the router's normalise writes a bare path.
  if (path === GITHUB_CALLBACK_PATH) return plain(GITHUB_CALLBACK_ADDRESS)
  // THE KEY LINK'S TOKEN (`t`) IS DROPPED HERE, for the same reason: only the
  // scope enters the route, so no address the router writes can carry it. It
  // lands on Config › Secrets' TypeSafe key form (SSC-UI); App has read the
  // token off the bar first (`secretsLinkToken`).
  if (path === KEY_LINK_PATH) return plain(`${CONFIG_SECRETS_ADDRESS}?${keyLinkView(query)}`)

  // THE SECRETS LINK'S TOKEN (`t`) IS DROPPED HERE, for the key link's reason,
  // on the path the API mints and on the page's own.
  if (path === SECRETS_LINK_PATH || path === CONFIG_SECRETS_PATH) {
    const view = secretsLinkView(query)
    return plain(view === '' ? CONFIG_SECRETS_ADDRESS : `${CONFIG_SECRETS_ADDRESS}?${view}`)
  }
  if (path === WORKSPACE_SETUP_PATH) return plain(WORKSPACE_SETUP_ADDRESS)
  // The Config pages' old paths (CONFIG-NAV). Setup read no query; the others
  // keep theirs, as the new path would.
  const oldConfig = OLD_CONFIG_PATHS[path]
  if (oldConfig !== undefined) return plain(query === '' || oldConfig.includes('?') ? oldConfig : `${oldConfig}?${query}`)

  // THE QA CREDENTIAL LINK'S TOKEN (`t`) AND TARGET ARE DROPPED HERE: the route
  // is the QA targets page alone, so no address the router writes carries them.
  if (QA_LINK_PATH.test(path)) return plain(QA_LINK_ADDRESS)

  // ADMIN › SCHEDULES IS GONE (SCHED-ONE-PAGE, owner decision 2026-10-13):
  // every tenant's schedules are Automate › Schedules' All tenants view, and
  // its old path lands there, the bar rewritten once by App's normalise.
  if (path === '/admin/schedules') return plain('automate/schedules?scope=all')
  // Approvals' old page (APPROVALS-MOVE, VQ-030): `/approvals` lands on
  // `/automate/approvals`, its `?kind=` kept.
  if (path === '/approvals') return plain(query === '' ? 'automate/approvals' : `automate/approvals?${query}`)
  // API reads' old paths, and anything under them (NAV-FOOT, #1672).
  if (OLD_API_READS_PATHS.some((old) => path === old || path.startsWith(`${old}/`))) return plain(REFERENCE_ADDRESS)

  const fixed = FIXED_BACK[path] ?? SECTION_ROOTS[path] ?? WORK_TAB_PATHS[path]
  if (fixed === undefined) return null
  return plain(query === '' ? fixed : `${fixed}?${query}`)
}

/**
 * Whether a `#...` fragment is an OLD ROUTE rather than an anchor on the page:
 * its head names a section, an alias, a legacy page, Help or API reads. On
 * `/help/<group>` a bare topic id is an anchor, and `help` is not one, so the
 * two never collide. Anything else is left to the browser.
 */
export function isLegacyHash(hash: string, heads: readonly string[], pathname = ''): boolean {
  const h = hash.replace(/^#/, '')
  if (h === '') return false
  // On `/help/...` a bare `#topic` is ALWAYS an in-page anchor. A topic id may
  // equal a section id (`capacity`, help.ts), and the section would win.
  if (pathname.startsWith('/help/') && !h.includes('/')) return false
  const head = h.split(/[/?]/)[0] ?? ''
  return heads.includes(head)
}
