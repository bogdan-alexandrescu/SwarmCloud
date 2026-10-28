// VISUAL QA RE-RUN LANE VQA2-R5 (part of #1427): ACCESS AND SETUP.
//
// One case per finding, each asserting the property the finding is about --
// what a layout rule resolves to in the real sheets at the width the finding
// was measured, which element a line is in, what the lede says -- rather than
// the markup's shape:
//
//   V165  "Install on <owner>" may shrink to its card and wrap its label;
//   V226  the grants table's Read/Write segment is as wide as its words;
//   V227  the folded policy rules draw a disclosure marker that turns on open;
//   V228  a step body's actions start under its text and are 44px on a phone,
//         and a step with nothing on its line draws no empty `p.ob-line`;
//   V229  beside the repository pane the owner list is capped and scrolls;
//   V230  Members has column heads where its rows are columns (on People and roles since GA-MOVE-VIEWS);
//   V232  the page lede says what the chosen view shows.
//
// V231 is in permissionCards.test.tsx, V233 and V234 in BrowserLogins.test.tsx,
// beside the cases they extend.
//
// MUTATIONS, each of which turns a case red: drop the `.ac-owner-acts > .c-btn`
// rule; drop `.ac-grant > .c-seg { justify-self: start }`; drop the folded
// summary's `::before`; put the empty `<p className="ob-line">` back or drop
// `.ob-step .ob-ws .ob-acts`; drop the min-width:1080px owner-list cap; drop
// the `.ac-members-h` head; put the one Mine lede back on every view.
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 400 }
const LONG = 'an-organisation-with-a-really-quite-long-name-for-an-owner'

const ID = (n: number) => `repo_${String(n).padStart(16, '0')}`

const CONNECTION = {
  connection_id: 'github:eng:abc',
  forge: 'github',
  method: 'app_user',
  forge_login: 'octo-dev',
  forge_user_id: 42,
  token_id: 'tok_u',
  secret_name: 'swarm-tenant-eng-git-u-89abcdef01234567',
  state: 'active',
  failure: null,
  access_expires_at: new Date(Date.now() + 7 * 3600_000).toISOString(),
  refresh_expires_at: null,
  refreshed_at: new Date(Date.now() - 600_000).toISOString(),
  refreshing: false,
  created_at: new Date().toISOString(),
  connected_at: new Date().toISOString(),
  revoked_at: null,
}

const ORG = (owner: string, over: Record<string, unknown> = {}) => ({
  owner,
  owner_type: owner === 'octo-dev' ? 'User' : 'Organization',
  installation_id: 7,
  repository_selection: 'selected',
  install_state: 'installed',
  sso: 'ok',
  enabled: true,
  enabled_at: null,
  checked_at: null,
  install_url: null,
  ...over,
})

const GRANT = (n: number, repository: string, mode: 'read' | 'write') => ({
  repo_id: ID(n),
  repository,
  owner: repository.split('/')[0],
  mode,
  can_push: true,
  archived: false,
  granted_at: null,
  granted_by: 'dev@swarm.example.com',
  checks: {},
  verified_at: null,
})

const GRANTS = [GRANT(1, 'example-org/example-docs', 'read'), GRANT(2, 'octo-dev/example-api', 'write')]

// A personal account: an organisation not installed offers Request install
// alone since QA1-FIX-01 config-04, so the long Install on link is a user's.
const UNINSTALLED = ORG(LONG, {
  owner_type: 'User',
  installation_id: null,
  repository_selection: null,
  install_state: 'not_installed',
  enabled: false,
  install_url: 'https://github.com/apps/swarmcloud/installations/new',
})

function api(owners: unknown[], extra: (m: string, url: string) => { status: number; body: unknown } | null = () => null) {
  return serve((m, url) => {
    const hit = extra(m, url)
    if (hit !== null) return hit
    if (m === 'GET' && url === '/v1/access') {
      return { status: 200, body: { tenant_id: 'eng', connection: CONNECTION, orgs: [ORG('example-org'), ORG('octo-dev')], grants: GRANTS } }
    }
    if (m === 'GET' && url === '/v1/access/orgs') {
      return { status: 200, body: { tenant_id: 'eng', orgs_listed: true, install_url: 'https://github.com/apps/swarmcloud/installations/new', owners } }
    }
    if (m === 'GET' && url.startsWith('/v1/access/orgs/') && url.includes('/repositories')) {
      const owner = decodeURIComponent(url.split('/')[4] ?? '')
      return {
        status: 200,
        body: { tenant_id: 'eng', owner, repositories: [], page: 1, per_page: 100, max_pages: 10, next_page: null, capped: false, q: null, total_count: 0 },
      }
    }
    return null
  })
}

/**
 * GitHub access' Connection tab, its Choose repositories tab, or the
 * reach-as-you list, which ReachableRepositories.tsx draws since
 * GITHUB-ACCESS-PAGE (part of #2157).
 */
async function mount(at: 'mine' | 'choose' | 'grants' = 'mine') {
  if (at === 'choose') window.history.replaceState(null, '', '/config/github?view=choose')
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AccessScreen } = await import('../Access')
  const { ReachableRepositoriesRead } = await import('../ReachableRepositories')
  return render(at === 'grants' ? <ReachableRepositoriesRead /> : <AccessScreen />)
}

/** Stub `matchMedia`, which jsdom does not have, as a phone or a desktop. */
function media(phone: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && query.includes('560'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', window.location.pathname)
})

describe('Access (VQA2-R5)', () => {
  it('V165: "Install on <a long owner>" shrinks to its card and wraps, at 400 and at 1440', async () => {
    api([ORG('example-org'), UNINSTALLED])
    await mount('choose')
    const link = await screen.findByRole('link', { name: `Install on ${LONG}` })
    expect(link.parentElement!.classList.contains('ac-owner-acts')).toBe(true)
    for (const env of [WIDE, PHONE]) {
      expect(painted(link, 'white-space', env), `nowrap at ${env.width}`).toBe('normal')
      expect(painted(link, 'overflow-wrap', env)).toBe('anywhere')
      expect(painted(link, 'min-width', env)).toBe('0')
      expect(painted(link, 'max-width', env)).toBe('100%')
      expect(painted(link, 'flex', env), `cannot shrink at ${env.width}`).toBe('0 1 auto')
      // The height follows the lines; a fixed 28px would clip the second one.
      expect(painted(link, 'height', env)).toBe('auto')
    }
    expect(painted(link, 'min-height', WIDE)).toBe('var(--h-sm)')
    // Still a phone target.
    expect(painted(link, 'min-height', PHONE)).toBe('44px')
    expect(painted(link.parentElement!, 'min-width', WIDE)).toBe('0')
  })

  it('V226: the grants Read/Write segment is as wide as its words, not its 132px track', async () => {
    api([ORG('example-org'), ORG('octo-dev')])
    await mount('grants')
    await screen.findByRole('list', { name: 'Your granted repositories, failures first' })
    const seg = document.querySelector<HTMLElement>('li.ac-grant[data-repo="octo-dev/example-api"] > .c-seg')!
    expect(seg).not.toBeNull()
    expect(painted(seg, 'justify-self', WIDE)).toBe('start')
    expect(painted(seg, 'justify-self', PHONE)).toBe('start')
  })

  it('V227: on a phone each folded rule draws a disclosure marker, which turns when it opens', async () => {
    media(true)
    api([ORG('example-org')])
    await mount()
    await screen.findByRole('heading', { name: 'What SwarmCloud may do as you' })
    const rules = document.querySelector<HTMLElement>('.ac-rules.is-folded')!
    expect(rules).not.toBeNull()
    const summaries = [...rules.querySelectorAll<HTMLElement>('summary')]
    expect(summaries.length).toBe(3)
    for (const s of summaries) {
      // `display: flex` is what removes the native marker, so one is drawn.
      expect(painted(s, 'display', PHONE)).toBe('flex')
      expect(painted(s, 'content', PHONE, 'before')).toBe("'\\25B8'")
    }
    const first = summaries[0]!
    first.parentElement!.setAttribute('open', '')
    expect(painted(first, 'content', PHONE, 'before')).toBe("'\\25BE'")
    expect(painted(summaries[1]!, 'content', PHONE, 'before')).toBe("'\\25B8'")
  })

  it('V229: beside the repository pane, 24 owners are a capped list that scrolls; stacked, it is the page', async () => {
    const owners = Array.from({ length: 24 }, (_, n) => ORG(`example-owner-${String(n).padStart(2, '0')}`))
    api(owners)
    await mount('choose')
    const list = await screen.findByRole('list', { name: 'Owners your connection reaches' })
    expect(list.querySelectorAll('li.ac-owner').length).toBe(24)
    expect(painted(list, 'max-height', WIDE)).toBe('min(600px, 70vh)')
    expect(painted(list, 'overflow-y', WIDE)).toBe('auto')
    // The typed-repo form and the grants are no longer under the whole list.
    expect(document.querySelector('.ac-typed')).not.toBeNull()
    // Stacked (one column) there is no inner scroller to trap a thumb.
    expect(painted(list, 'max-height', PHONE)).toBeNull()
    expect(painted(list, 'overflow-y', PHONE)).toBeNull()
    expect(painted(list, 'max-height', { width: 900 })).toBeNull()
  })

  it('V230: Members has column heads at desktop width, on the rows\' own tracks; a phone has labels instead', async () => {
    api([ORG('example-org')], (m, url) =>
      m === 'GET' && url === '/v1/access/members'
        ? {
            status: 200,
            body: { tenant_id: 'eng', members: [{ user: 'operator@swarm.example.com', connection: CONNECTION, orgs: [ORG('example-org')], grants: GRANTS }] },
          }
        : null,
    )
    // On People and roles' Members' GitHub tab since GA-MOVE-VIEWS (part of #2157).
    window.history.replaceState(null, '', '/config/people?view=members')
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { WorkspacePeopleScreen } = await import('../WorkspacePeople')
    render(<WorkspacePeopleScreen />)
    const list = await screen.findByRole('list', { name: 'Members of eng' })
    const head = document.querySelector<HTMLElement>('.ac-members-h')!
    expect(head, 'Members has no column heads').not.toBeNull()
    expect([...head.children].map((c) => c.textContent)).toEqual(['Member', 'Connection', 'Owners', 'Repositories'])
    const row = list.querySelector<HTMLElement>('li.ac-member')!
    expect(row.children.length).toBe(head.children.length)
    expect(painted(head, 'display', WIDE)).toBe('grid')
    expect(painted(head, 'grid-template-columns', WIDE)).toBe(painted(row, 'grid-template-columns', WIDE))
    expect(painted(head, 'display', { width: 900 })).toBe('grid')
    expect(painted(head, 'display', PHONE)).toBe('none')
    expect(painted(row.querySelector<HTMLElement>('.ac-cell-l')!, 'display', PHONE)).toBe('inline')
  })

  // Members and Browser logins left the page with GA-MOVE-VIEWS (part of #2157);
  // their ledes went with them to People and roles and Secrets.
  it('V232: the lede says what Connection and Choose repositories each show', async () => {
    api([ORG('example-org')])
    await mount()
    const lede = () => visible(document.querySelector('.ac-page > .ur-sub'))
    await waitFor(() => expect(lede()).toContain('in workspace eng'))
    // The reach-as-you list left the page (GITHUB-ACCESS-PAGE), so no tab's lede promises it.
    // MUTATION: put "The orgs and repositories SwarmCloud may reach as you" back as Mine's lede.
    expect(lede()).toMatch(/^How SwarmCloud acts as you on GitHub in workspace eng/)

    fireEvent.click(screen.getByRole('tab', { name: 'Choose repositories' }))
    expect(lede()).toMatch(/^The owners the GitHub App is installed on in workspace eng/)

    fireEvent.click(screen.getByRole('tab', { name: 'Connection' }))
    expect(lede()).toMatch(/^How SwarmCloud acts as you on GitHub/)
  })
})

// ---------------------------------------------------------------------------
// Setup's checklist (V228)
// ---------------------------------------------------------------------------

function step(name: string, state: string, evidence: Record<string, unknown>, over: Record<string, unknown> = {}) {
  return { step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [], ...over }
}

function checklist() {
  return {
    tenant_id: 'eng',
    user: 'dev@swarm.example.com',
    user_hash: '89abcdef01234567',
    derived_at: new Date().toISOString(),
    next_step: 'claude_account',
    complete: false,
    source: 'derived on this read',
    steps: [
      step('signed_in', 'done', { email: 'dev@swarm.example.com', tenant_id: 'eng' }, { checked_at: new Date().toISOString() }),
      { ...step('claude_account', 'todo', { own: 0, lent: 0, provider_key: false, loan_request: null }), required: true },
      step('github_connected', 'todo', { via: null }),
    ],
  }
}

describe('Setup (VQA2-R5)', () => {
  it('V228: the Claude-account step\'s actions start under its text, are phone targets, and no empty line doubles the gap', async () => {
    vi.resetModules()
    const { Checklist } = await import('../Onboarding')
    render(<Checklist doc={checklist() as never} reload={() => {}} />)
    const el = document.querySelector<HTMLElement>('li.ob-step[data-step="claude_account"]')!
    // No empty `p.ob-line` under the title: the body says what there is.
    const main = el.querySelector<HTMLElement>('.ob-step-main')!
    expect(main.querySelector(':scope > p.ob-line')).toBeNull()
    for (const p of el.querySelectorAll<HTMLElement>('p.ob-line')) expect(visible(p)).not.toBe('')
    // A step with a line keeps it.
    expect(visible(document.querySelector('li.ob-step[data-step="signed_in"] > .ob-step-main > p.ob-line'))).toMatch(/workspace eng · checked /)

    const acts = el.querySelector<HTMLElement>('.ob-ws .ob-acts')!
    const add = within(acts).getByRole('link', { name: 'Add key' })
    const loan = within(acts).getByRole('button', { name: 'Request a loan' })
    expect(painted(acts, 'justify-content', WIDE)).toBe('flex-start')
    expect(painted(acts, 'justify-content', PHONE)).toBe('flex-start')
    for (const b of [add, loan]) expect(painted(b, 'min-height', PHONE), `${b.textContent} is not a phone target`).toBe('44px')
    // As tall as the step-column actions beside them.
    const col = document.querySelector<HTMLElement>('li.ob-step[data-step="github_connected"] .ob-step-act .c-btn')
    if (col !== null) expect(painted(col, 'min-height', PHONE)).toBe('44px')
  })
})
