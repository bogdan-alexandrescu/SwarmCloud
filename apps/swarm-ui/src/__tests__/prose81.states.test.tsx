// ISSUE 81, THE STATES BEHIND A CLICK: ACCOUNTS, SECRETS, MCP SERVERS AND QA
// TARGETS KEEP ONE SHORT LINE EACH (the owner's standing rule, 2026-11-05).
//
// The app sweep (proseapp.tsx) mounts every nav page; it clicks nothing and
// meets no empty, refused or unread state. Each case here mounts one such
// state of a file the issue names and holds three things:
//   * nothing in it is a visible long run (proseruns.ts: nine words or more,
//     or two sentences or more);
//   * each must-stay line -- an irreversible action's warning, a could-not-
//     check reason, what a one-time link does -- is still on the surface;
//   * each sentence taken off it is still in the document, word for word,
//     behind an (i) (`[data-help-description]`).
//
// BREAK IT: put any listed sentence back on the surface, shorten its tip, or
// drop a warning's line -- the case for its state turns red.
//
// Every token- or password-shaped value is built at runtime.

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard } from '../api'
import type { PoolAccount } from '../accountsPool'
import { allIn, around, oneIn, shownIn } from './proseruns'
import {
  AT,
  BLOG,
  DAY,
  HOUR,
  MIN,
  OWN_LOGIN,
  PORTAL,
  SHOP,
  UNSET,
  WAIT,
  account,
  holds,
  made,
  mount,
  ok,
  qaServe,
  target,
  unmountAll,
  week,
  type Json,
  type Reply,
} from './prose81states'
import { serve } from './repofixture'

const api = vi.hoisted(() => ({
  loadAccountsBoard: vi.fn(),
  loadAccountHealth: vi.fn(),
  removeAccount: vi.fn(),
  setAccountMaxAgents: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  ...api,
}))

beforeEach(() => {
  api.loadAccountHealth.mockReset()
  api.loadAccountsBoard.mockReset()
})

afterEach(() => {
  unmountAll()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
  window.localStorage.clear()
  window.sessionStorage.clear()
})

// ---------------------------------------------------------------------------
// QA targets
// ---------------------------------------------------------------------------

async function qaPage(view = 'page=qa-targets'): Promise<HTMLElement> {
  const { RepositoriesScreen } = await import('../Repositories')
  mount(<RepositoriesScreen view={view} go={vi.fn()} />)
  return waitFor(() => {
    const el = oneIn<HTMLElement>(document, '.qa-page')
    expect(el).not.toBeNull()
    return el!
  }, WAIT)
}

async function button(root: ParentNode, label: string): Promise<HTMLButtonElement> {
  return waitFor(() => {
    const b = allIn<HTMLButtonElement>(root, 'button').find((x) => shownIn(x) === label)
    expect(b, `no button ${label}`).toBeDefined()
    return b!
  }, WAIT)
}

const card = (id: string) => oneIn<HTMLElement>(document, `.qa-target[data-target-id="${id}"]`)!
const formOf = () => oneIn<HTMLElement>(document, '.ur-form')!

const QA_INTRO =
  'Web applications this workspace’s visual-QA runs test, each filing its findings in one of the workspace’s registered repositories. A new or changed host waits for a platform admin’s approval before any run.'

describe('QA targets: each state is one short line', () => {
  it('the empty state, and the page’s own intro behind its title’s (i)', async () => {
    qaServe([])
    const page = await qaPage()
    await screen.findByText('No QA targets registered', undefined, WAIT)
    await button(page, 'Register QA target')
    holds(page, {
      lines: ['A workspace admin registers one here.'],
      moved: [
        QA_INTRO,
        'A workspace admin registers a web application here: its address, the repository its findings are filed in, and the screens to capture.',
      ],
    })
  })

  it('the repository filter, and a filter no target files in', async () => {
    qaServe([target(SHOP, 'shop')])
    await qaPage('page=qa-targets&repository=example-org/example-api')
    const line = await waitFor(() => {
      const el = oneIn(document, '[data-qa-filter]')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    holds(line, { lines: ['Showing the QA targets that file in example-org/example-api', 'Show every QA target'] })
    unmountAll()
    qaServe([target(SHOP, 'shop')])
    const page = await qaPage('page=qa-targets&repository=example-org/other')
    await screen.findByText('No QA target files in example-org/other', undefined, WAIT)
    holds(oneIn(page, '.state') ?? page, {
      lines: ['It files in another repository.'],
      moved: ['The workspace’s one QA target files in other repositories.'],
    })
  })

  it('the lead: every environment can run, a credential is missing, a host is pending', async () => {
    qaServe([target(SHOP, 'shop')])
    await qaPage()
    const lead = () => waitFor(() => {
      const el = oneIn(document, '[data-qa-lead]')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    holds(await lead(), { lines: ['Tick environments on a target, press Run.'], moved: ['Tick the environments on a target and press Run.'] })
    unmountAll()
    qaServe([target(PORTAL, 'portal', { sign_in: OWN_LOGIN })])
    await qaPage()
    holds(await lead(), {
      lines: ['portal has no test credential', 'Cannot run until a workspace admin sets it'],
      moved: ['Its leg cannot run until a workspace admin sets it.'],
    })
    const off = oneIn(card(PORTAL), '[data-cannot-run]')!
    holds(off, {
      lines: ['Cannot run: no test credential is set'],
      moved: ['It signs in with its own login and no test credential is set; a workspace admin sets it.'],
    })
    unmountAll()
    qaServe([target(BLOG, 'blog', { host_state: 'pending' })])
    await qaPage()
    const pending = 'blog.example.test is waiting for a platform admin to approve it; no run starts against a host until then.'
    holds(await lead(), { lines: ['blog cannot run', 'Its host waits for a platform admin’s approval'], moved: [pending] })
    holds(oneIn(card(BLOG), '[data-cannot-run]')!, { lines: ['Cannot run: its host is not approved yet'], moved: [pending] })
    unmountAll()
    qaServe([target(BLOG, 'blog', { host_state: 'revoked' })])
    await qaPage()
    const revoked = "blog.example.test's approval was revoked; a platform admin approves it again before a run."
    holds(await lead(), { lines: ['blog’s host approval was revoked', 'A platform admin approves it again'], moved: [revoked] })
    holds(oneIn(card(BLOG), '[data-cannot-run]')!, { lines: ['Cannot run: its host approval was revoked'], moved: [revoked] })
  })

  it('delete: the warning is one sentence on the surface', async () => {
    qaServe([target(SHOP, 'shop')])
    await qaPage()
    fireEvent.click(await button(await waitFor(() => { expect(card(SHOP)).not.toBeNull(); return card(SHOP) }, WAIT), 'Delete'))
    holds(screen.getByRole('group', { name: 'Delete shop' }), {
      lines: ['Its test credential is deleted with it.', 'Delete shop'],
      moved: ['Delete shop? Its test credential is deleted with it.'],
    })
  })

  it('approving a host: what it grants stays, one short line each', async () => {
    qaServe([target(BLOG, 'blog', {
      host_state: 'pending', environment: 'non_production',
      sign_in: { ...OWN_LOGIN, extra_origin: 'https://id.example.test' }, extra_origins: ['https://api.example.test'],
    })], { write: (m, path) => (m === 'POST' && path.endsWith(':approve-host') ? { status: 200, body: {} } : null) })
    await qaPage()
    fireEvent.click(await button(await waitFor(() => { expect(card(BLOG)).not.toBeNull(); return card(BLOG) }, WAIT), 'Approve host'))
    const form = oneIn<HTMLElement>(document, 'form.qa-approve')!
    holds(form, {
      lines: ['Approving lets QA runs open blog.example.test.', 'The gate lets exactly this POST through.'],
      moved: [
        "Approving blog.example.test lets this workspace's visual-QA runs open it. The approval is recorded under your name. The login also signs in through https://id.example.test, approved with it.",
        'The gate lets exactly a POST to https://id.example.test at this path through, after its own sign-in, so the app can refresh its token. Every other write there stays refused.',
      ],
    })
    holds(oneIn(form, '[data-writes-notice]')!, {
      lines: ['Flows here may write to each ticked host.'],
      moved: ['Flows on this environment may write, and their writes reach each ticked host as well as blog.example.test, a shared API among them.'],
    })
  })

  it('the one-time credential form: the link works once', async () => {
    qaServe([target(PORTAL, 'portal', { sign_in: OWN_LOGIN })], {
      write: (m, path) => (m === 'PUT' && path.endsWith('/credential') ? { status: 422, body: { code: 'invalid', message: 'name the one to use' } } : null),
    })
    window.history.replaceState(null, '', `/qa-targets/${PORTAL}/credential?t=${made('lnk')}`)
    await qaPage()
    await waitFor(() => expect(oneIn(document, 'form.qa-cred')).not.toBeNull(), WAIT)
    holds(formOf(), {
      lines: ['Use a test account; the link works once.'],
      moved: [
        'Type the test account’s username and password. They go from this page to the workspace’s secret store and nowhere else: Claude, the plugin and this page’s address never see them, and the live browser types them into the app’s login without the agent holding them. Use a test account with no MFA or CAPTCHA. The link works once.',
      ],
    })
    const form = oneIn<HTMLFormElement>(document, 'form.qa-cred')!
    fireEvent.submit(form)
    await waitFor(() => expect(shownIn(formOf())).toContain('Type both; nothing was sent.'), WAIT)
    holds(formOf(), { moved: ['Type both the username and the password; nothing was sent.'] })
    const [user, pass] = allIn(form, 'input') as [HTMLInputElement, HTMLInputElement]
    fireEvent.change(user, { target: { value: 'qa-user' } })
    fireEvent.change(pass, { target: { value: made('pw') } })
    fireEvent.submit(form)
    await waitFor(() => expect(shownIn(formOf())).toContain('Nothing was saved; the link still works.'), WAIT)
    holds(formOf(), { moved: ['Nothing was saved; the link still works, fix the field and save again.'] })
  })

  it('a spent link says so in one line, its next step behind the (i)', async () => {
    qaServe([target(PORTAL, 'portal', { sign_in: OWN_LOGIN })])
    window.history.replaceState(null, '', `/qa-targets/${PORTAL}/credential`)
    await qaPage()
    const form = await waitFor(() => {
      const el = oneIn<HTMLFormElement>(document, 'form.qa-cred')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    fireEvent.submit(form)
    await waitFor(() => expect(shownIn(formOf())).toContain('This form needs a fresh one-time link.'), WAIT)
    holds(formOf(), { moved: ['This form needs a one-time link, and its link was already used or never came. Ask for a new one with Set credential.'] })
  })

  it('the challenge bypass form', async () => {
    qaServe([target(SHOP, 'shop', { challenge_bypass: { kind: 'header', name: 'X-QA-Bypass' } })])
    window.history.replaceState(null, '', `/qa-targets/${SHOP}/challenge-bypass?t=${made('lnk')}`)
    await qaPage()
    await waitFor(() => expect(oneIn(document, 'form.qa-cred')).not.toBeNull(), WAIT)
    holds(formOf(), {
      lines: ['Type the value; the link works once.'],
      moved: [
        'Type the value of the challenge bypass the app’s owner made for these runs. It goes from this page to the workspace’s secret store and nowhere else, and the live browser adds it on the target’s approved origins alone. The link works once.',
      ],
    })
  })

  it('the register form: every hint is one short line', async () => {
    qaServe([])
    const page = await qaPage()
    fireEvent.click(await button(page, 'Register QA target'))
    const form = formOf()
    fireEvent.change(oneIn(form, 'select[name="bypass_kind"]')!, { target: { value: 'header' } })
    fireEvent.change(oneIn(form, 'select[name="kind"]')!, { target: { value: 'own_login' } })
    holds(form, {
      lines: ['Plain key=value pairs; never a token or password.', 'Only for an app your workspace owns.'],
      moved: [
        'Plain key=value pairs, joined by &, added to every address the run opens, sign-in included: for an app that picks its environment from the address. Never a token or a password. Changing it keeps the host\'s approval.',
        'Only for an app your workspace owns: the header or cookie its CAPTCHA test keys or WAF skip rule accept. The live browser adds it on this target’s approved origins and nowhere else, and the agent never sees its value.',
        'file.label_rules, e.g. [{"paths": ["/agents/"], "labels": ["agent-page-rework"]}], adds a rule’s labels to the findings on routes starting with one of its paths',
        'What the gate clicks, in order, before the form (a chooser, or a “Sign in” button before the identity provider). Text is the control’s exact visible text; CSS, optional, narrows the candidates (for example button).',
      ],
    })
    fireEvent.change(oneIn(form, 'textarea[name="more"]')!, { target: { value: '{' } })
    fireEvent.submit(oneIn(form, 'form')!)
    await waitFor(() => expect(shownIn(formOf())).toContain('More settings is not JSON.'), WAIT)
    holds(formOf(), { moved: ['More settings is not JSON: write one object, for example {"widths": ["desktop"]}.'] })
  })

  it('the edit form: a new host asks approval again', async () => {
    qaServe([target(SHOP, 'shop', {
      environments: [
        { name: 'prod', environment: 'production', base_url: 'https://shop.example.test/', host: 'shop.example.test', host_state: 'approved', sign_in: { kind: 'none' }, credential: UNSET },
      ],
    })])
    await qaPage()
    fireEvent.click(await button(await waitFor(() => { expect(card(SHOP)).not.toBeNull(); return card(SHOP) }, WAIT), 'Edit'))
    holds(formOf(), {
      lines: ['A new host sets approval back to pending.'],
      moved: [
        "Changing the address's host sets shop.example.test's approval back to pending.",
        'Plain key=value pairs, joined by &, added to every address that environment\'s runs open, sign-in included. Never a token or a password. Changing one keeps its host\'s approval.',
      ],
    })
  })

  it('what a save says: a saved credential is never shown again, a new host waits', async () => {
    qaServe([target(PORTAL, 'portal', { sign_in: OWN_LOGIN })], {
      write: (m, path) => (m === 'PUT' && path.endsWith('/credential') ? { status: 200, body: { credential: { ...UNSET, set: true } } } : null),
    })
    window.history.replaceState(null, '', `/qa-targets/${PORTAL}/credential?t=${made('lnk')}`)
    await qaPage()
    const form = await waitFor(() => {
      const el = oneIn<HTMLFormElement>(document, 'form.qa-cred')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    const [user, pass] = allIn(form, 'input') as [HTMLInputElement, HTMLInputElement]
    fireEvent.change(user, { target: { value: 'qa-user' } })
    fireEvent.change(pass, { target: { value: made('pw') } })
    fireEvent.submit(form)
    const said = around(await screen.findByText('Credential saved', undefined, WAIT), '.c-banner')!
    holds(said, { lines: ['Never shown again.'], moved: ['The test credential for portal is set. No page, route or log can show it again.'] })
    unmountAll()
    window.history.replaceState(null, '', '/')
    qaServe([target(SHOP, 'shop')], {
      write: (m, path) => (m === 'PATCH' && path.startsWith('/v1/qa-targets/') ? { status: 200, body: { qa_target: target(SHOP, 'shop', { host_state: 'pending' }) } } : null),
    })
    await qaPage()
    fireEvent.click(await button(await waitFor(() => { expect(card(SHOP)).not.toBeNull(); return card(SHOP) }, WAIT), 'Edit'))
    fireEvent.change(oneIn(formOf(), 'input[name="name"]')!, { target: { value: 'shop two' } })
    fireEvent.submit(oneIn(formOf(), 'form')!)
    const changed = around(await screen.findByText('Changed shop', undefined, WAIT), '.c-banner')!
    holds(changed, {
      lines: ['Its host waits for a platform admin’s approval.'],
      moved: ["Its host shop.example.test waits for a platform admin's approval before any run."],
    })
  })
})

// ---------------------------------------------------------------------------
// Secrets: GitHub Actions
// ---------------------------------------------------------------------------

const ACTIONS_REPO = 'rep_ex1'
const REPOSITORY = 'example-org/example-api'
const BASE = `/v1/repositories/${ACTIONS_REPO}/actions`
const SETTINGS = `https://github.com/${REPOSITORY}/settings/environments`

function rule(environment: string, kind: string | null): Json {
  return {
    environment, exists: kind !== null, rule: kind, main_only: kind === 'main_only', branch: 'main',
    branches: kind === 'main_only' ? [{ name: 'main', type: 'branch' }] : [],
    create_url: kind === null ? `${SETTINGS}/new` : null, settings_url: SETTINGS,
  }
}

const RULE_OF: Readonly<Record<string, string>> = { 'repo-secrets': 'main_only', staging: 'any_branch' }

function listBody(over: Json = {}): Json {
  const secret = (name: string) => ({ name, created_at: AT, updated_at: AT, set_by_swarmcloud: true, set_by: 'operator@example.com', set_at: AT })
  return {
    tenant_id: 'eng', repo_id: ACTIONS_REPO, repository: REPOSITORY,
    repository_secrets: [secret('TEST_DB_URL')],
    environments: [
      { environment: 'repo-secrets', rule: rule('repo-secrets', 'main_only'), secrets: [] },
      { environment: 'staging', rule: rule('staging', 'any_branch'), secrets: [] },
      { environment: 'preview', rule: rule('preview', 'protected_branches'), secrets: [] },
    ],
    environments_truncated: true, create_url: `${SETTINGS}/new`,
    can_write: true, cannot_write_because: null, checked_at: new Date(Date.now() - MIN).toISOString(),
    ...over,
  }
}

async function actions(opts: { list?: () => Reply; write?: (m: string) => Reply; link?: Json } = {}): Promise<HTMLElement> {
  serve((m, url, body) => {
    if (m === 'GET' && url.startsWith(`${BASE}/secrets`)) return opts.list ? opts.list() : { status: 200, body: listBody() }
    const ruled = m === 'GET' ? /\/actions\/environments\/([^/]+)\/rule/.exec(url) : null
    if (ruled !== null) {
      const env = decodeURIComponent(ruled[1]!)
      return { status: 200, body: { repo_id: ACTIONS_REPO, ...rule(env, RULE_OF[env] ?? null) } }
    }
    if (m === 'POST' && url.startsWith(`${BASE}/links`)) {
      const b = body as Json
      const path = `/config/secrets?scope=repository&repo=${ACTIONS_REPO}&dest=actions&name=${String(b.name)}&t=${made('lnk')}`
      return { status: 201, body: { url: `https://console.example.com${path}`, path, name: b.name, environment: b.environment, expires_at: AT } }
    }
    if (opts.write) return opts.write(m)
    if (m === 'PUT') return { status: 200, body: { secret: { name: 'X' }, created: true, via_link: true, rule: null, note: 'kept only the name' } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SecretsActions } = await import('../SecretsActions')
  mount(<SecretsActions repoId={ACTIONS_REPO} repository={REPOSITORY} {...(opts.link === undefined ? {} : { link: opts.link as never })} />)
  return waitFor(() => {
    const el = oneIn<HTMLElement>(document, '.sa-pane')
    expect(el).not.toBeNull()
    return el!
  }, WAIT)
}

const refusedWith = (reason: string, detail: Json = {}): Reply =>
  ({ status: 409, body: { code: 'ACTIONS_APP', message: 'refused', detail: { reason, ...detail } } })

const dialog = () => screen.getByRole('dialog')
const next = () => fireEvent.click(within(dialog()).getByRole('button', { name: /^next$/i }))

describe('Secrets › GitHub Actions: each state is one short line', () => {
  it('the permission, coverage and installation refusals say one could-not line each', async () => {
    for (const [reason, lines, moved] of [
      ['actions_secrets_not_granted',
        ['The GitHub App lacks Secrets permission', 'SwarmCloud cannot list or write its secrets.'],
        [
          `The SwarmCloud GitHub App lacks Secrets permission on ${REPOSITORY}`,
          'Until the installation’s owner accepts Secrets and Environments: Read and write on GitHub, SwarmCloud can neither list nor write this repository’s Actions secrets. Nothing was written.',
        ]],
      ['actions_repository_not_covered',
        [`The App’s installation does not cover ${REPOSITORY}`, 'Nothing was written.'],
        ['The installation’s owner adds the repository to it. Nothing was written.']],
      ['actions_app_not_installed',
        ['The GitHub App is not installed', 'SwarmCloud cannot list or write its secrets.'],
        [
          `The SwarmCloud GitHub App is not installed for ${REPOSITORY}`,
          'Its owner installs it and enables the repository; until then SwarmCloud can neither list nor write its Actions secrets.',
        ]],
    ] as [string, string[], string[]][]) {
      const pane = await actions({ list: () => refusedWith(reason) })
      await waitFor(() => expect(oneIn(pane, '.c-banner.is-warn')).not.toBeNull(), WAIT)
      holds(pane, { lines, moved })
      unmountAll()
    }
  })

  it('the pane: its lead, who may write, each environment’s readers, and what it cannot do', async () => {
    const pane = await actions()
    await within(pane).findByRole('table', { name: /repository secrets of/i }, WAIT)
    holds(pane, {
      lines: [
        'Names and update times, never values.',
        'You may write here', 'checked 1m ago',
        'GitHub is asked again at each write.',
        'Only the first 30 environments were read.',
        'Create environments on GitHub, then Check rule.',
        'A workflow on any branch can read it.',
        'Not main alone: protected branches can deploy.',
      ],
      moved: [
        `Written into ${REPOSITORY} on GitHub. SwarmCloud reads names and update times, never values, and keeps only the name, who set it and when.`,
        `You may write here: workspace admin, and GitHub calls you admin of ${REPOSITORY} (checked 1m ago).`,
        'SwarmCloud asks GitHub again at each write, with your own GitHub connection.',
        'GitHub was read for the first 30 environments only; more may exist.',
        'SwarmCloud does not create environments yet: create one on GitHub, then Check rule reads its branch rule.',
        'Writing here asks you to type staging: a workflow on any branch can read it.',
        'Writing here asks you to type preview: protected branches can deploy to it, not main alone.',
      ],
    })
  })

  it('who may write when GitHub’s word is not known, and read only with the API’s reason', async () => {
    let pane = await actions({ list: () => ({ status: 200, body: listBody({ can_write: null }) }) })
    await within(pane).findByRole('table', { name: /repository secrets of/i }, WAIT)
    holds(pane, {
      lines: ['Who may write here', 'Takes workspace admin and GitHub admin.'],
      moved: [`Setting or deleting takes both: you manage this workspace, and GitHub calls you admin of ${REPOSITORY}. SwarmCloud asks GitHub at each write, with your own GitHub connection.`],
    })
    unmountAll()
    const because = 'GitHub does not call you an admin of this repository; ask a workspace admin'
    pane = await actions({ list: () => ({ status: 200, body: listBody({ can_write: false, cannot_write_because: because }) }) })
    await within(pane).findByRole('table', { name: /repository secrets of/i }, WAIT)
    holds(pane, {
      lines: ['Read only for you', 'You can read the names here.', because],
      moved: [`Only a workspace manager whom GitHub calls admin of ${REPOSITORY} sets or deletes these.`],
      // The API's own reason, drawn as it wrote it.
      data: [because],
    })
  })

  it('Check rule: the answer’s line, and the missing environment’s fallback step', async () => {
    const pane = await actions()
    await within(pane).findByRole('table', { name: /repository secrets of/i }, WAIT)
    const form = () => within(pane).getByRole('form', { name: "Check an environment's rule" })
    fireEvent.click(within(pane).getAllByRole('button', { name: 'Check rule' })[0]!)
    const check = (env: string) => {
      fireEvent.change(within(form()).getByRole('combobox'), { target: { value: env } })
      fireEvent.submit(form())
    }
    const box = () => oneIn(pane, '.sa-check')!
    check('repo-secrets')
    await waitFor(() => expect(shownIn(box())).toContain('Only main deploys to it.'), WAIT)
    holds(box(), { moved: ['Only main deploys to it: a secret written there reaches workflows on main alone.'] })
    check('staging')
    await waitFor(() => expect(shownIn(box())).toContain('Not main only.'), WAIT)
    holds(box(), { moved: ['Not main only: writing there asks you to type staging.'] })
    check('brand-new')
    await waitFor(() => expect(shownIn(box())).toContain('Create it on GitHub, then check again.'), WAIT)
    holds(box(), { moved: ['Create it on GitHub with its deployment branches set to main only, then check its rule here again.'] })
  })

  it('the sheet: destination, readers and name, the one-time form, and its branch warning', async () => {
    const pane = await actions()
    await within(pane).findByRole('table', { name: /repository secrets of/i }, WAIT)
    fireEvent.click(within(pane).getByRole('button', { name: /^set a secret$/i }))
    fireEvent.click(within(dialog()).getByRole('radio', { name: /staging/ }))
    holds(dialog(), {
      lines: ['Writing needs GitHub admin of the repository.', 'Create environments on GitHub, then Check rule.'],
      moved: [
        `GitHub Actions in ${REPOSITORY}. Writing needs GitHub admin of the repository: SwarmCloud asks GitHub when the form opens and again at the write.`,
        'SwarmCloud does not create environments yet: create one on GitHub, then Check rule reads its branch rule.',
      ],
    })
    next()
    holds(dialog(), {
      lines: ['Read by workflows in staging, from any branch.', 'Letters, digits and _; never the value.'],
      moved: ['Read by workflows that run in staging, which any branch can deploy to.', 'Letters, digits and _; not starting with a digit or GITHUB_. Never the value.'],
    })
    fireEvent.change(within(dialog()).getByLabelText(/^name$/i), { target: { value: '9X' } })
    holds(dialog(), { lines: ['Letters, digits and _; no leading digit.'] })
    fireEvent.change(within(dialog()).getByLabelText(/^name$/i), { target: { value: 'DEPLOY_KEY' } })
    next()
    holds(dialog(), {
      lines: ['Opens a one-time form, for ten minutes.'],
      moved: [`Opens a form for DEPLOY_KEY in ${REPOSITORY} › staging. It works once, for ten minutes. The value is sealed in SwarmCloud, written to GitHub and never shown again.`],
      // The plugin command to copy: a line of code, not a sentence.
      data: ['/sc'],
    })
    fireEvent.click(within(dialog()).getByRole('button', { name: /open the one-time form/i }))
    await within(dialog()).findByLabelText(/^value$/i, undefined, WAIT)
    holds(dialog(), {
      lines: ['At most 48 KB.', 'A workflow on any branch can read it.'],
      moved: ['At most 48 KB. The form is cleared whatever the answer.', 'staging lets any branch deploy: a workflow there can read this secret.'],
    })
  })

  it('a set secret, and a spent link, each say one line', async () => {
    const set = async (write?: (m: string) => Reply) => {
      const pane = await actions(write === undefined ? {} : { write })
      await within(pane).findByRole('table', { name: /repository secrets of/i }, WAIT)
      fireEvent.click(within(pane).getByRole('button', { name: /^set a secret$/i }))
      fireEvent.click(within(dialog()).getByRole('radio', { name: /repo-secrets/ }))
      next()
      fireEvent.change(within(dialog()).getByLabelText(/^name$/i), { target: { value: 'DEPLOY_KEY' } })
      next()
      fireEvent.click(within(dialog()).getByRole('button', { name: /open the one-time form/i }))
      const value = await within(dialog()).findByLabelText(/^value$/i, undefined, WAIT)
      fireEvent.change(value, { target: { value: made('val') } })
      fireEvent.click(within(dialog()).getByRole('button', { name: /^set secret$/i }))
      return pane
    }
    const pane = await set()
    const said = await waitFor(() => {
      const el = oneIn(pane, '.pk-saved')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    holds(said, {
      lines: ['DEPLOY_KEY was set', 'the value is not shown again'],
      moved: [`DEPLOY_KEY was set in ${REPOSITORY} › repo-secrets. The link is used up; the value is not shown again, here or anywhere.`],
    })
    unmountAll()
    await set((m) => (m === 'PUT' ? { status: 403, body: { code: 'SECRET_LINK_INVALID', message: 'unknown' } } : null))
    const bad = await waitFor(() => {
      const el = oneIn(dialog(), '.c-banner.is-bad')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    holds(bad, {
      lines: ['DEPLOY_KEY was not set: its link is spent.'],
      moved: ['DEPLOY_KEY was not set: the one-time link is used, expired or not yours. Open a new one-time form.'],
    })
  })

  it('a pasted link’s form says the link works once', async () => {
    window.history.replaceState(null, '', `/config/secrets?scope=repository&repo=${ACTIONS_REPO}&dest=actions&t=${made('lnk')}`)
    await actions({ link: { name: 'DEPLOY_KEY', environment: 'repo-secrets' } })
    const banner = around(await screen.findByText('Opened from a one-time link', undefined, WAIT), '.c-banner')!
    holds(banner, {
      lines: ['The link works once, for ten minutes.'],
      moved: ['Paste the value into the form below: the /sc command and this page’s address never see it. The link works once, for ten minutes.'],
    })
    holds(dialog(), { data: ['/sc'] })
  })

  it('delete: that GitHub destroys it stays on the surface', async () => {
    const pane = await actions()
    await within(pane).findByRole('table', { name: /repository secrets of/i }, WAIT)
    fireEvent.click(within(pane).getByRole('button', { name: 'Delete TEST_DB_URL' }))
    holds(dialog(), {
      lines: ['GitHub destroys it; SwarmCloud cannot restore it.'],
      moved: [`GitHub destroys TEST_DB_URL in ${REPOSITORY}. Workflows that read it fail until it is set again, and SwarmCloud cannot restore it: it never kept the value.`],
    })
  })
})

// ---------------------------------------------------------------------------
// Secrets: the workspace's own store
// ---------------------------------------------------------------------------

function storeRow(over: Json = {}): Json {
  return {
    section: 'agents', store: 'workspace_secrets', name: 'brave-key', scope: 'workspace', kind: 'api_key', status: 'set',
    set_at: AT, set_by: 'ana@example.com', consumers: [], shadowed_by: [], shadows: [], can_manage: true, ...over,
  }
}

const RECORD: Json = {
  name: 'brave-key', provider: 'brave', kind: 'api_key', current_version: '1', created_at: AT, created_by: 'ana@example.com',
  rotated_at: null, rotated_by: null, grants: [],
}

async function secrets(view: string, rows: Json[], write?: (m: string, u: string) => Reply, record: Json = RECORD) {
  serve((m, url) => {
    const u = decodeURIComponent(url)
    if (m === 'GET' && u.startsWith('/v1/secrets/overview')) {
      const q = new URLSearchParams(u.slice(u.indexOf('?') + 1))
      return { status: 200, body: { tenant_id: 'eng', scope: q.get('scope') ?? 'workspace', personal_tenant_id: 'u-ana', can_manage: true, rows } }
    }
    if (m === 'GET' && u.startsWith('/v1/repositories') && !u.includes('/secrets')) return { status: 200, body: { repositories: [] } }
    if (m === 'GET' && u.startsWith('/v1/workspace/secrets/consumers')) {
      return { status: 200, body: { consumers: [{ consumer: 'profile:claude-code', kind: 'profile', name: 'claude-code', slots: [{ slot: 'files', kinds: ['api_key'] }], grantable: true, note: '' }], kinds: ['api_key'], providers: ['brave'] } }
    }
    if (m === 'GET' && u.startsWith('/v1/workspace/secrets')) return { status: 200, body: { tenant_id: 'eng', can_manage: true, secrets: [record] } }
    if (m === 'POST' && u.endsWith('/links')) return { status: 201, body: { url: null, path: `/config/secrets?action=add&t=${made('lnk')}`, expires_at: AT } }
    return write?.(m, u) ?? null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SecretsScreen } = await import('../Secrets')
  mount(<SecretsScreen view={view} />)
}

describe('Secrets: the workspace store’s confirmations and forms are one short line', () => {
  it('delete a secret: that every version is destroyed stays, and who loses it', async () => {
    await secrets('scope=workspace', [storeRow()])
    fireEvent.click(await screen.findByRole('button', { name: /^delete brave-key$/i }, WAIT))
    holds(dialog(), {
      lines: ['Every version is destroyed, not disabled.', 'No grant is enabled.'],
      moved: [
        'Every version of brave-key is destroyed, not disabled: the only way back is adding the value again.',
        'No grant is enabled, so no consumer loses it.',
        'Running attempts keep the copy they were already given; the next attempt gets nothing.',
      ],
    })
  })

  it('delete the TypeSafe key: its warning is on the surface', async () => {
    await secrets('scope=workspace', [storeRow({ section: 'keys', store: 'provider_keys', name: 'typesafe', kind: null })])
    fireEvent.click(await screen.findByRole('button', { name: /^delete the typesafe/i }, WAIT))
    holds(dialog(), { lines: ['Every version is destroyed.', 'This workspace’s Jev and browser agents lose it.'] })
  })

  it('rotate: the form, what it said, and a spent link', async () => {
    const rotated = async (reply: Reply) => {
      await secrets('scope=workspace&action=rotate&name=brave-key', [storeRow()], (m, u) => (m === 'PUT' && u.includes('/value') ? reply : null))
      const form = await screen.findByRole('form', { name: 'Rotate brave-key' }, WAIT)
      holds(form, {
        lines: ['A new version of brave-key.'],
        moved: ['the previous one stays enabled for 24 hours, for attempts already running.'],
      })
      fireEvent.change(oneIn(form, 'input[type="password"]')!, { target: { value: made('val') } })
      fireEvent.submit(form)
    }
    await rotated({ status: 200, body: { secret: { ...RECORD, current_version: '2' } } })
    const said = await waitFor(() => {
      const el = oneIn(document, '.pk-saved')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    holds(said, {
      lines: ['brave-key was rotated', 'version 2 is current', 'the previous stays enabled 24 hours'],
      moved: ['The previous version stays enabled for 24 hours, then is disabled.'],
    })
    unmountAll()
    await rotated({ status: 403, body: { code: 'SECRET_LINK_INVALID', message: 'unknown' } })
    const bad = await waitFor(() => {
      const el = oneIn(document, '.c-banner.is-bad')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    holds(bad, {
      lines: ['brave-key was not rotated: its link is spent.'],
      moved: ['brave-key was not rotated: the one-time link is used, expired or not yours. Nothing was written. Open the form again from Rotate, or ask /sc for a new link.'],
    })
  })

  it('a key form: where it is read, and a spent link’s refusal', async () => {
    await secrets('scope=workspace&runner_key=anthropic', [], (m, u) => (m === 'POST' && u === '/v1/tenants/me/credentials'
      ? { status: 403, body: { code: 'KEY_LINK_INVALID', message: 'unknown' } } : null))
    const form = await screen.findByRole('form', { name: /set the anthropic runner key/i }, WAIT)
    const where = around(screen.getByText('Where'), 'dl')!
    holds(where, { lines: ['the workspace: profiles that call anthropic'], moved: ['the workspace: the runner profiles that call anthropic read it'] })
    fireEvent.change(oneIn(form, 'input')!, { target: { value: made('val') } })
    fireEvent.submit(form)
    const bad = await waitFor(() => {
      const el = oneIn(document, '.c-banner.is-bad')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    holds(bad, {
      lines: ['The anthropic runner key was not set.', 'Its one-time link is spent.'],
      moved: ['The anthropic runner key was not set: the one-time link is used, expired or not yours. Nothing was written.'],
    })
  })
})

// ---------------------------------------------------------------------------
// MCP servers: no removal, its choices' meanings behind the group's (i)
// ---------------------------------------------------------------------------

describe('MCP servers: the register form', () => {
  it('names each credential choice in a word, and what each means behind the (i)', async () => {
    serve((m, url) => (m === 'GET' && url.startsWith('/v1/mcp-servers')
      ? { status: 200, body: { tenant_id: 'eng', can_manage: true, egress_addresses: [], servers: [] } } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { McpServersScreen } = await import('../McpServers')
    mount(<McpServersScreen />)
    fireEvent.click((await screen.findAllByRole('button', { name: /register an mcp server/i }, WAIT))[0]!)
    const form = await waitFor(() => {
      const el = oneIn<HTMLElement>(document, 'form')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    holds(form, { moved: ['Per-run credential: the caller sends one with each agent, and it dies with the agent.'] })
  })
})

// ---------------------------------------------------------------------------
// Accounts
// ---------------------------------------------------------------------------

const KINDS = ['rate_limit', 'exhausted', 'auth', 'overloaded', 'server_error', 'network', 'assign_refused', 'reauth', 'refresh_failed']
const counts = (over: Json = {}): Json => ({ ...Object.fromEntries(KINDS.map((k) => [k, 0])), ...over })

function record(over: Json = {}): Json {
  return {
    account_id: 'eng:laptop', status: 'healthy', limited_until: null,
    counts: { last_1h: counts(), last_24h: counts(), last_7d: counts() }, hourly: [], events: [], observed_at: new Date().toISOString(), ...over,
  }
}

const event = (kind: string, over: Json = {}): Json => ({
  at: new Date(Date.now() - 20 * MIN).toISOString(), kind, tenant_id: 'eng', task_id: null, window: null, reset_at: null,
  message: `${kind} here`, own: true, ...over,
})

const busyHealth = () => screen.getByRole('region', { name: 'Health' }).getAttribute('aria-busy')

async function health(res: unknown, owned = true): Promise<HTMLElement> {
  api.loadAccountHealth.mockResolvedValue(res)
  const { SheetHealth } = await import('../AccountSheet')
  mount(<SheetHealth a={account()} owned={owned} />)
  await waitFor(() => expect(busyHealth()).toBeNull(), WAIT)
  return screen.getByRole('region', { name: 'Health' })
}

const failure = (kind: string, message: string) =>
  ({ status: 'error', error: { kind, httpStatus: kind === 'not_found' ? 404 : 503, code: null, message } })

describe('the account sheet: the no-reading states are one short line', () => {
  it('the forecast: no reading, a passed reset, a reset with nothing read, and each measured pace', async () => {
    const { SheetForecast } = await import('../AccountSheet')
    const draw = (a: PoolAccount) => {
      unmountAll()
      return mount(<SheetForecast a={a} now={Date.now()} />).container
    }
    holds(draw(account({ windows: {}, observed_at: null })), {
      lines: ['no 7-day reading to take a pace from', 'Laptop sessions spend the same window.'],
      moved: ['Laptop sessions on this subscription spend the same window.'],
    })
    holds(draw(account({ windows: week(0.94, -HOUR) })), {
      lines: ['Its week should have reset', 'nothing has been read since'],
      moved: ['passed, and nothing has been read since. The forecast waits for a fresh reading.'],
    })
    holds(draw(account({ windows: week(0, 6 * DAY, true) })), {
      lines: ['Nothing read since its week reset'],
      moved: ['Its week reset and nothing has been read since, so there is no pace yet. The next reset is'],
    })
    holds(draw(account({ windows: week(0.99, 3 * DAY) })), {
      lines: ['Under its 5% floor now'],
      moved: ['Its 7-day window is under its 5% floor now, and its week resets'],
    })
    holds(draw(account({ windows: week(0.1, 3 * DAY) })), {
      lines: ['Stays above its 5% floor', 'of the week a day'],
      moved: ['At this pace it stays above its 5% floor until its week resets', 'of the week a day, its 7-day share used over the hours since its week opened.'],
    })
    holds(draw(account({ windows: week(0.9, 3 * DAY) })), {
      lines: ['Reaches its 5% floor near'],
      moved: ['At this pace it reaches its 5% floor near'],
    })
  })

  it('health: not served, unreadable, and a measured zero', async () => {
    holds(await health(failure('not_found', 'no such route')), {
      lines: ['Not served here: unknown, not zero.'],
      moved: ['This deployment does not serve the account health record: ', 'answered 404. Its quota broker predates the record (ACCOUNT-429-SCOPE). Rate-limit hits, errors and refusals are unknown here, not zero.'],
    })
    unmountAll()
    holds(await health(failure('server_error', 'the broker did not answer')), {
      lines: ['Could not be read: unknown, not zero.', 'the broker did not answer'],
      moved: ['The health record could not be read: the broker did not answer. Its counts and events are unknown until a read succeeds, not zero.'],
    })
    unmountAll()
    holds(await health(ok(record())), {
      lines: ['No events in 7 days.', 'Read and empty: a measured zero.'],
      moved: [
        'No events in 7 days: no limits, errors or credential trouble.',
        'The record was read and is empty. That is a measured zero, so no chart is drawn.',
      ],
    })
  })

  it('health: re-auth needed, for its owner and for a borrower', async () => {
    const reauth = ok(record({ status: 'reauth_needed' }))
    const moved = ['The provider refused its credential. The broker assigns it nothing until someone signs in again; work that would have used it waits for another account.']
    holds(oneIn(await health(reauth), '.acct-hs-callout')!, {
      lines: ['Nothing is assigned until someone signs in.', 'Sign in again is below'], moved,
    })
    unmountAll()
    holds(oneIn(await health(reauth, false), '.acct-hs-callout')!, {
      lines: ['Nothing is assigned until someone signs in.', 'Its owner, eng, signs in again.'], moved,
    })
  })

  it('health: a limited and an erroring account, the folded kinds, another workspace’s event, what the list keeps', async () => {
    const reset = new Date(Date.now() + 2 * HOUR).toISOString()
    const c = counts({ rate_limit: 1, assign_refused: 3 })
    const s = await health(ok(record({
      status: 'limited', limited_until: reset, counts: { last_1h: c, last_24h: c, last_7d: counts({ rate_limit: 5, assign_refused: 3 }) },
      events: [
        event('rate_limit', { window: 'five_hour', reset_at: reset }),
        event('rate_limit', { own: false, tenant_id: null, message: null }),
      ],
    })))
    holds(s, {
      lines: ['5-hour window refused at', 'None in 7 days:', 'Another workspace’s event: details not shown.', 'The last 2 of 8 in 7 days'],
      moved: [
        'Its 5-hour window refused at', 'The broker withholds it until then: 3 refusals at assignment in 24 h.',
        'None in 7 days, so not drawn as rows:',
        'Another workspace’s event: its tenant, task and message are not shown, and its time is to the hour.',
        'The record keeps the last 50 events: drawn here are 2 of the 8 in 7 days. The 6 older ones count in the hourly series and the 7 d column, and their messages are gone.',
      ],
      data: ['None in 7 days:'],
    })
    unmountAll()
    const e = counts({ overloaded: 2, server_error: 1 })
    holds(await health(ok(record({ status: 'erroring', counts: { last_1h: e, last_24h: e, last_7d: e }, events: [event('overloaded'), event('overloaded'), event('server_error')] }))), {
      lines: ['3 provider errors in the last hour', '2 of them 529 overloaded', 'still takes work', 'All 3 events of 7 days, newest first'],
      moved: ['3 provider errors in the last hour, 2 of them 529 overloaded. It still takes work.', 'All 3 events of 7 days, newest first (the record keeps the last 50).'],
      data: ['None in 7 days:'],
    })
  })

  it('health: a stale record says its failed read in one line', async () => {
    const s = await health({ status: 'stale', data: record(), error: { kind: 'server_error', httpStatus: 503, code: null, message: 'no answer' }, fetchedAt: Date.now() })
    holds(s, { lines: ['The last read failed', 'no answer'], moved: ['The last read failed (no answer); this is the record read at'] })
  })

  it('a duplicate: what the pool does with it, and what to do', async () => {
    const { SheetDuplicate } = await import('../AccountSheet')
    const props = { accounts: [account({ account_id: 'eng:team', label: 'team' })], scope: 'eng', owned: true } as unknown as Omit<Parameters<typeof SheetDuplicate>[0], 'a'>
    const draw = (a: PoolAccount, over: Json = {}) => {
      unmountAll()
      return mount(<SheetDuplicate a={a} {...props} {...over} />).container
    }
    const withheld = { duplicate_of: 'eng:team', blocked_by: [{ duplicate_of: 'eng:team', reason: 'one' }] } as Partial<PoolAccount>
    const FIX = 'Replace the credential below, signed in to the organisation you meant from that organisation’s browser profile (one claude.ai login can hold two, and the sign-in takes the one that is selected). Or remove laptop.'
    holds(draw(account(withheld)), {
      lines: ['The pool hands out only team.', 'Replace the credential below, or remove it.'],
      moved: ['laptop was signed in to the same organisation as team. The pool hands out only team, so laptop adds no capacity.', FIX],
    })
    holds(draw(account(withheld), { owned: false }), { lines: ['eng owns laptop and can fix it.'] })
    holds(draw(account({ duplicate_of: 'eng:team' })), {
      lines: ['The pool keeps handing out laptop.', 'Nothing here.'],
      moved: ['team was signed in to the same organisation later. The pool keeps handing out laptop and withholds the other, which adds no capacity.'],
    })
    holds(draw(account({ possible_duplicate: { of: 'eng:team', reason: 'same' } })), {
      lines: ['The pool still hands out both.'],
      moved: ['laptop and team read identically on two polls. The pool still hands out both; if they are one subscription, the second adds no capacity.', FIX],
    })
  })

  it('the cap: its line, its editor’s hint, and a broker that does not say', async () => {
    const { AgentCap } = await import('../AccountSheet')
    const draw = (a: PoolAccount) => {
      unmountAll()
      return mount(<AgentCap a={a} owned reload={vi.fn()} />).container
    }
    const silent = account({ observed_at: null })
    delete (silent as { max_agents?: number }).max_agents
    holds(draw(silent), {
      lines: ['this broker does not say', 'Until its first reading it takes one.'],
      moved: ['this broker does not say, and the deployed default is'],
    })
    const c = draw(account())
    holds(c, { lines: ['At most 6 agents at once.', 'Change the cap'] })
    fireEvent.click(await button(c, 'Change the cap'))
    holds(c, { lines: ['Lowering it moves no agent already on it.'], moved: ['Lowering it moves no agent already on it; the pool applies it at the next hand-out.'] })
    fireEvent.change(oneIn(c, 'input')!, { target: { value: '0' } })
    holds(c, { lines: ['A whole number from 1 to 50.'], moved: ['To stop it taking agents, pause it.'] })
  })
})

describe('the accounts pool: its empty, unread and withheld states are one short line', () => {
  it('the verdict with nothing to count', async () => {
    const { Verdict } = await import('../AccountsVerdict')
    const draw = (accounts: PoolAccount[], shortfall = 0) => {
      unmountAll()
      return oneIn(mount(<Verdict accounts={accounts} scope="eng" shortfall={shortfall} now={Date.now()} waiting={{ status: 'loading' } as never} />).container, '.acct-verdict-say')!
    }
    holds(draw([]), {
      lines: ['No account is registered.'],
      moved: [
        'No account is registered, so no agent that needs the pool can start.',
        'Agents whose runner profile uses the pool wait until an account is added here, or one is lent to this workspace. Agents with their own key are not affected.',
      ],
    })
    holds(draw([], 2), {
      lines: ['No account here could be read.'],
      moved: ['No account here could be read, so no agent that needs the pool can start.', '2 registered accounts could not be read, so nothing on this page counts them.'],
    })
    const silent = account()
    delete (silent as { can_take_new_agent?: boolean }).can_take_new_agent
    holds(draw([silent]), {
      lines: ['Which accounts can take one is not said.'],
      moved: ['This deployment does not say which accounts can take a new agent.', 'It is older than the per-account answer; the table shows the windows it reported.'],
    })
    holds(draw([account({ state: 'PAUSED', can_take_new_agent: false })]), {
      lines: ['No account can take a new agent.'],
      moved: ['No account can take a new agent, and no clock says when one will.'],
    })
  })

  it('what blocks an account, and an unmeasured window', async () => {
    const { whyOf, WindowCell } = await import('../AccountsTable')
    const draw = (a: PoolAccount) => {
      unmountAll()
      return mount(<div>{whyOf(a, 'eng', Date.now())}</div>).container
    }
    holds(draw(account({ state: 'REAUTH_REQUIRED', can_take_new_agent: false })), {
      lines: ['Signed out: its sign-in expired'],
      moved: ['its sign-in expired, and it takes nothing until someone signs in again'],
    })
    holds(draw(account({ state: 'DRAINING', can_take_new_agent: false })), {
      lines: ['Draining: it takes no new agent'],
      moved: ['it serves the agents it holds and takes no new one'],
    })
    holds(draw(account({ can_take_new_agent: false, blocked_by: [{ first_reading: 'pending', max_before_reading: 1, assigned: 1 }] })), {
      lines: ['Waiting for its first reading'],
      moved: ['a new account takes 1 agent until it is measured, and it holds 1'],
    })
    unmountAll()
    const { container } = mount(<WindowCell a={account({ provider: 'openai-api-key', windows: {}, observed_at: null })} k="five_hour" now={Date.now()} />)
    if ((container.textContent ?? '').includes('not measured')) {
      holds(container, { lines: ['not measured'], moved: ['not measured: OpenAI gives an API key no usage window'] })
    }
  })

  it('remove an account: the warning is one sentence on the surface', async () => {
    const page = {
      status: 'ok', fetchedAt: Date.now(), serverAt: new Date().toISOString(),
      data: {
        page: { accounts: [account({ assigned: 2 })], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 },
        tenants: [], tenantsDetail: null, readAt: Date.now(),
      } as AccountsBoard,
    }
    api.loadAccountsBoard.mockResolvedValue(page)
    const { AccountsScreen } = await import('../Accounts')
    mount(<AccountsScreen />)
    await waitFor(() => expect(oneIn(document, 'button.acct-open')).not.toBeNull(), WAIT)
    fireEvent.click(oneIn<HTMLButtonElement>(document, 'button.acct-open')!)
    const remove = await waitFor(() => {
      const el = oneIn(document, '.acct-action.danger')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    holds(remove, {
      lines: ['Removing it takes it out of the pool.'],
      moved: [
        'Removal takes the account out of the pool. The credential in the secret store is retained rather than deleted',
        '2 agents currently hold this account. Move it to DRAINING first if you want them off it before it goes.',
      ],
    })
  })
})
