// WORK › REPOSITORIES › GIT TOKENS (repositories.html screen 12, pick B:
// cards by scope plus a command line, NO paste box) and PERMISSIONS (screen
// 13, pick A: a tokens × repositories grid, one cell per capability).
//
// WHAT EACH CASE HOLDS:
//   * one card per token record: its scope, owner (the account it acts as),
//     forge, kind and last 4, expiry, last verified and the repositories it
//     covers; the scope filter narrows them;
//   * a token's value is NEVER on the page -- not in text, an attribute, a
//     title, an aria-label or a copy button -- even when the API serves one;
//     the page has no field that could take one (no paste box);
//   * registering creates the token entry (POST /v1/git-tokens, scope and
//     repo only) and shows the secret's name and the exact
//     `scripts/create-secrets.sh --stdin` command, read from the answer the
//     API actually sends -- `{token, store_command, note}` (GT-GUIDE: it read
//     `git_token` and reported every create as a failure); Rotate shows the
//     command for an existing entry; the only copy button copies the NAME;
//   * GT-GUIDE: the terminal path is folded behind "Use a personal access
//     token instead (advanced)"; the team (tenant) token, and a repository's,
//     which the API registers for an admin only, are offered to an admin only,
//     and Rotate is offered only where the caller may register;
//   * GT-GUIDE: a token GitHub's App renews says "renews automatically", not
//     "Expires today"; one whose renewal failed says that;
//   * the permission grid: grouped by repository, the resolving token
//     marked, each cell ok / missing / unknown with its reason as the cell's
//     title, expires-in and last verified; an unknown cell is never ok;
//   * Verify all re-probes every token;
//   * a route that is not there yet is "not served yet", naming it;
//   * at 390 the grid becomes per-repository cards;
//   * G4-17: at 1440 the grid fits its card: the capability heads wrap in
//     72 px columns and the last head is "Verified", never clipped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { DAY, HOUR, ago, leakedValue, repo, serve, token, tokenShapedIn, visible } from './repofixture'
import { cascade } from './cssgate'
import { SHEETS as ALL } from './sheets'

const WAIT = { timeout: 4000 }

const TOKENS = {
  git_tokens: [
    token(),
    token({
      token_id: 'tok_r',
      scope: 'repository',
      repo_ids: ['repo_0a1b2c3d4e5f6071'],
      repositories: ['example-org/example-api'],
      provider_suffix: 'git-r-0a1b2c3d4e5f6071',
      secret_name: 'swarm-tenant-eng-git-r-0a1b2c3d4e5f6071',
      forge_login: 'example-api-bot',
      last4: '7f3a',
      expires_at: new Date(Date.now() + 41 * DAY + HOUR).toISOString(),
      verified_at: ago(3 * HOUR),
    }),
    token({
      token_id: 'tok_u',
      scope: 'user',
      user: 'operator@swarm.example.com',
      provider_suffix: 'git-u-89abcdef01234567',
      secret_name: 'swarm-tenant-eng-git-u-89abcdef01234567',
      forge_login: 'operator-gh',
      kind: 'classic_pat',
      last4: '19c2',
      expires_at: null,
      verified_at: ago(5 * HOUR),
      yours: true,
    }),
  ],
}

const REPOS = { repositories: [repo(), repo({ repo_id: 'repo_1111111111111111', repo: 'example-web' })] }

const PAT = 'Use a personal access token instead (advanced)'
const TEAM = 'Workspace token for eng (admins only)'

async function mount(view: string, go = vi.fn()) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  const utils = render(<RepositoriesScreen view={view} go={go} />)
  return { ...utils, go }
}

afterEach(() => vi.unstubAllEnvs())

const tokCards = () => Array.from(document.querySelectorAll<HTMLElement>('.ur-toks > .ur-tok'))

const ME = (isAdmin: boolean) => ({
  tenant: { tenant_id: 'eng', kind: 'group' },
  principal: { email: 'operator@swarm.example.com', domain: 'swarm.example.com', groups: [], is_admin: isAdmin },
})

function tokenRoutes(
  extra: (m: string, url: string, body: unknown) => { status: number; body: unknown } | null = () => null,
  { admin = false }: { admin?: boolean } = {},
) {
  return serve((m, url, body) => {
    if (m === 'GET' && url === '/v1/git-tokens') return { status: 200, body: TOKENS }
    if (m === 'GET' && url === '/v1/repositories') return { status: 200, body: REPOS }
    if (m === 'GET' && url.startsWith('/v1/tenants/me')) return { status: 200, body: ME(admin) }
    return extra(m, url, body)
  })
}

/** What POST /v1/git-tokens answers (routes/gittokens.py `register_git_token`). */
function created(record: Record<string, unknown>) {
  const t = token(record)
  return {
    status: 201,
    body: {
      token: t,
      store_command: `scripts/create-secrets.sh --tenant eng --provider ${String(t.provider_suffix)} --stdin`,
      note: "this registered the slot's record only; store the token with the command, which reads it from stdin.",
    },
  }
}

const open = (name: string) => fireEvent.click(screen.getByRole('button', { name }))

describe('Git tokens are cards by scope (pick B)', () => {
  it('draws one card per token with scope, owner, forge, kind and last 4, expiry, last verified and repos covered', async () => {
    tokenRoutes()
    await mount('page=tokens')
    await waitFor(() => expect(tokCards()).toHaveLength(3), WAIT)
    expect(document.querySelector('h1')?.textContent).toBe('Git tokens')
    expect(visible(document.querySelector('.ur-crumb'))).toBe('Config › Repositories › Git tokens')
    const [ten, rep, usr] = tokCards()
    expect(visible(ten!.querySelector('.c-chip'))).toBe('Tenant')
    expect(visible(ten!.querySelector('h2'))).toBe('eng default')
    expect(visible(ten!)).toContain('acts as eng-swarm-bot')
    expect(visible(ten!)).toContain('github')
    expect(visible(ten!)).toContain('fine-grained PAT ··· a41c')
    expect(visible(ten!)).toContain('Expires in 9 days')
    expect(visible(ten!)).toContain('Last verified 2h ago')
    expect(visible(ten!)).toContain('Repos covered: every repository no narrower token covers')
    expect(visible(rep!.querySelector('h2'))).toBe('example-org/example-api')
    expect(visible(rep!)).toContain('Repos covered: example-org/example-api')
    expect(visible(usr!.querySelector('h2'))).toBe('operator@swarm.example.com')
    expect(visible(usr!)).toContain('classic PAT ··· 19c2')
    // No expiry served: a dash with its reason, never "never" or 0.
    expect(usr!.querySelector('.c-dash')?.getAttribute('title')).toBe('No expiry recorded: a classic PAT without an expiration header does not expire')
    expect(visible(usr!)).toContain("Repos covered: operator@swarm.example.com's own dispatches")
  })

  it('filters by scope', async () => {
    tokenRoutes()
    await mount('page=tokens')
    await waitFor(() => expect(tokCards()).toHaveLength(3), WAIT)
    const scope = screen.getByRole('radiogroup', { name: 'Scope' })
    fireEvent.click(within(scope).getByRole('radio', { name: 'Repository' }))
    expect(tokCards().map((c) => visible(c.querySelector('h2')))).toEqual(['example-org/example-api'])
    fireEvent.click(within(scope).getByRole('radio', { name: 'All' }))
    expect(tokCards()).toHaveLength(3)
  })

  it('never puts a token value on the page, and has no field that could take one', async () => {
    tokenRoutes()
    await mount('page=tokens')
    await waitFor(() => expect(tokCards()).toHaveLength(3), WAIT)
    open(PAT)
    expect(tokenShapedIn(document.documentElement.outerHTML)).toEqual([])
    // No paste box: no text input, textarea or password field anywhere.
    expect(document.querySelectorAll('textarea, input[type="password"], input[type="text"], input:not([type])')).toHaveLength(0)
    // The value's characters do not leak through an attribute either.
    for (const el of Array.from(document.querySelectorAll('*'))) {
      for (const a of Array.from(el.attributes)) expect(a.value.includes(leakedValue())).toBe(false)
    }
  })

  it('a token the App renews says "renews automatically", never "Expires today"; a failed renewal says so', async () => {
    const soon = new Date(Date.now() + 2 * HOUR).toISOString()
    serve((m, url) => {
      if (m === 'GET' && url === '/v1/git-tokens') {
        return {
          status: 200,
          body: {
            git_tokens: [
              token({ token_id: 'tok_app', scope: 'user', user: 'devops@swarm.example.com', kind: 'app_user', forge_login: 'octo-dev', last4: null, expires_at: soon, yours: true }),
              token({ token_id: 'tok_old', scope: 'user', user: 'other@swarm.example.com', kind: 'app_user', forge_login: 'octo-old', last4: null, expires_at: ago(3 * HOUR), state: 'expired' }),
            ],
          },
        }
      }
      return null
    })
    await mount('page=tokens')
    await waitFor(() => expect(tokCards()).toHaveLength(2), WAIT)
    const [live, dead] = tokCards()
    expect(visible(live!)).toContain('GitHub App (user)')
    expect(visible(live!)).toContain('renews automatically')
    expect(visible(live!)).not.toContain('today')
    expect(live!.querySelector('.ur-bad')).toBeNull()
    expect(visible(dead!)).toContain('Renewal failed: reconnect GitHub')
    expect(dead!.querySelector('.ur-bad')).not.toBeNull()
  })

  it('Rotate shows the exact create-secrets command for that entry, and the only copy button copies the NAME', async () => {
    const writeText = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    tokenRoutes(undefined, { admin: true })
    await mount('page=tokens')
    await waitFor(() => expect(within(tokCards()[1]!).queryByRole('button', { name: 'Rotate' })).not.toBeNull(), WAIT)
    fireEvent.click(within(tokCards()[1]!).getByRole('button', { name: 'Rotate' }))
    const reg = document.querySelector('.ur-reg')!
    expect(visible(reg.querySelector('.ur-secret code'))).toBe('swarm-tenant-eng-git-r-0a1b2c3d4e5f6071')
    expect(visible(reg.querySelector('pre.ur-term'))).toBe('$ scripts/create-secrets.sh --tenant eng --provider git-r-0a1b2c3d4e5f6071 --stdin')
    const copies = Array.from(document.querySelectorAll('button')).filter((b) => /copy/i.test(b.textContent ?? '') || /copy/i.test(b.getAttribute('aria-label') ?? ''))
    expect(copies.map((b) => b.textContent)).toEqual(['Copy secret name'])
    fireEvent.click(copies[0]!)
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('swarm-tenant-eng-git-r-0a1b2c3d4e5f6071'), WAIT)
  })

  it('Rotate is offered only on the entries the caller may register: their own, or every one for an admin', async () => {
    tokenRoutes()
    await mount('page=tokens')
    await waitFor(() => expect(tokCards()).toHaveLength(3), WAIT)
    expect(tokCards().map((c) => within(c).queryByRole('button', { name: 'Rotate' }) !== null)).toEqual([false, false, true])
  })

  it('after Rotate, the scope the radio shows is the scope Create posts', async () => {
    const calls = tokenRoutes((m, url) => (m === 'POST' && url === '/v1/git-tokens' ? created({ token_id: 'tok_new2', scope: 'repository', repo_ids: ['repo_1111111111111111'] }) : null), { admin: true })
    await mount('page=tokens')
    await waitFor(() => expect(within(tokCards()[1]!).queryByRole('button', { name: 'Rotate' })).not.toBeNull(), WAIT)
    // Rotate the REPOSITORY card: the radio shows One repository.
    fireEvent.click(within(tokCards()[1]!).getByRole('button', { name: 'Rotate' }))
    const reg = document.querySelector<HTMLElement>('.ur-reg')!
    expect(within(reg).getByRole('radio', { name: 'One repository' }).getAttribute('aria-checked')).toBe('true')
    // Picking Me forgets the rotated entry; picking One repository again drives the select and Create.
    fireEvent.click(within(reg).getByRole('radio', { name: 'Me' }))
    expect(within(reg).getByRole('radio', { name: 'Me' }).getAttribute('aria-checked')).toBe('true')
    expect(within(reg).queryByRole('combobox', { name: 'Repository' })).toBeNull()
    expect(reg.querySelector('pre.ur-term')).toBeNull()
    fireEvent.click(within(reg).getByRole('radio', { name: 'One repository' }))
    fireEvent.change(within(reg).getByRole('combobox', { name: 'Repository' }), { target: { value: 'repo_1111111111111111' } })
    fireEvent.click(within(reg).getByRole('button', { name: 'Create the token entry' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true), WAIT)
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ scope: 'repository', repo_id: 'repo_1111111111111111' })
  })

  it('after Rotate of your own entry, Create without touching the radio posts that scope', async () => {
    const calls = tokenRoutes((m, url) => (m === 'POST' && url === '/v1/git-tokens' ? created({ token_id: 'tok_new3', scope: 'user' }) : null))
    await mount('page=tokens')
    await waitFor(() => expect(tokCards()).toHaveLength(3), WAIT)
    fireEvent.click(within(tokCards()[2]!).getByRole('button', { name: 'Rotate' }))
    const reg = document.querySelector<HTMLElement>('.ur-reg')!
    // A non-admin may create their own entry only, so there is no choice to show.
    expect(within(reg).queryByRole('radio')).toBeNull()
    expect(visible(reg.querySelector('pre.ur-term'))).toBe('$ scripts/create-secrets.sh --tenant eng --provider git-u-89abcdef01234567 --stdin')
    fireEvent.click(within(reg).getByRole('button', { name: 'Create the token entry' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true), WAIT)
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ scope: 'user' })
  })

  it('creating your own entry reads the answer the API sends, {token, store_command}, and shows the name and the command', async () => {
    const calls = tokenRoutes((m, url) =>
      m === 'POST' && url === '/v1/git-tokens'
        ? created({
            token_id: 'tok_mine',
            scope: 'user',
            user: 'devops@swarm.example.com',
            provider_suffix: 'git-u-0123456789abcdef',
            secret_name: 'swarm-tenant-eng-git-u-0123456789abcdef',
            last4: null,
            state: 'unverified',
          })
        : null,
    )
    await mount('page=tokens')
    await waitFor(() => expect(tokCards()).toHaveLength(3), WAIT)
    open(PAT)
    const reg = document.querySelector<HTMLElement>('.ur-reg')!
    expect(visible(reg)).toContain('value never enters the console')
    // A non-admin is offered their own entry only: the API registers the
    // team's and a repository's for an admin, and refused them after the click.
    expect(within(reg).queryByRole('radio', { name: 'One repository' })).toBeNull()
    expect(within(reg).queryByRole('radio', { name: /tenant|team/i })).toBeNull()
    fireEvent.click(within(reg).getByRole('button', { name: 'Create the token entry' }))
    await waitFor(() => expect(reg.querySelector('pre.ur-term')).not.toBeNull(), WAIT)
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ scope: 'user' })
    expect(visible(reg)).not.toContain('was not created')
    expect(visible(reg)).not.toMatch(/\bslot\b|\brecord\b/)
    expect(visible(reg.querySelector('.ur-secret code'))).toBe('swarm-tenant-eng-git-u-0123456789abcdef')
    expect(visible(reg.querySelector('pre.ur-term'))).toBe('$ scripts/create-secrets.sh --tenant eng --provider git-u-0123456789abcdef --stdin')
    expect(tokenShapedIn(document.documentElement.outerHTML)).toEqual([])
  })

  it("an admin registers a repository's entry, and the team token in its own section", async () => {
    const calls = tokenRoutes(
      (m, url, body) => {
        if (m !== 'POST' || url !== '/v1/git-tokens') return null
        return (body as { scope: string }).scope === 'tenant'
          ? created({ token_id: 'tok_team', scope: 'tenant' })
          : created({
              token_id: 'tok_new',
              scope: 'repository',
              repo_ids: ['repo_1111111111111111'],
              provider_suffix: 'git-r-1111111111111111',
              secret_name: 'swarm-tenant-eng-git-r-1111111111111111',
              last4: null,
              state: 'unverified',
            })
      },
      { admin: true },
    )
    await mount('page=tokens')
    await waitFor(() => expect(screen.queryByRole('button', { name: TEAM })).not.toBeNull(), WAIT)
    open(PAT)
    const reg = document.querySelector<HTMLElement>('.ur-reg')!
    fireEvent.click(within(reg).getByRole('radio', { name: 'One repository' }))
    fireEvent.change(within(reg).getByRole('combobox', { name: 'Repository' }), { target: { value: 'repo_1111111111111111' } })
    fireEvent.click(within(reg).getByRole('button', { name: 'Create the token entry' }))
    await waitFor(() => expect(reg.querySelector('pre.ur-term')).not.toBeNull(), WAIT)
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ scope: 'repository', repo_id: 'repo_1111111111111111' })
    expect(visible(reg.querySelector('pre.ur-term'))).toBe('$ scripts/create-secrets.sh --tenant eng --provider git-r-1111111111111111 --stdin')

    open(TEAM)
    const team = document.querySelector<HTMLElement>('.ur-reg-team')!
    // config2-12 (QA2-FIX-19): the credential is the workspace's, so it is the
    // 'Workspace token' on the button and on the card it opens.
    // MUTATION: put back 'Team token for' and these fail.
    expect(visible(team)).toContain('Workspace token for eng: from your terminal')
    expect(document.body.textContent).not.toMatch(/Team token/)
    expect(within(team).queryByRole('radio')).toBeNull()
    fireEvent.click(within(team).getByRole('button', { name: 'Create the token entry' }))
    await waitFor(() => expect(team.querySelector('pre.ur-term')).not.toBeNull(), WAIT)
    expect(calls.filter((c) => c.method === 'POST').map((c) => c.body)).toContainEqual({ scope: 'tenant' })
  })

  it('a non-admin is never offered the team token', async () => {
    tokenRoutes()
    await mount('page=tokens')
    await waitFor(() => expect(tokCards()).toHaveLength(3), WAIT)
    expect(screen.queryByRole('button', { name: TEAM })).toBeNull()
    expect(screen.queryByText(/admins only/)).toBeNull()
  })

  it('the token route not being there yet is "not served yet", naming it', async () => {
    serve(() => null)
    await mount('page=tokens')
    await waitFor(() => expect(document.querySelector('[data-notserved="GET /v1/git-tokens"]')).not.toBeNull(), WAIT)
    // The command-line path still opens: it needs only the entry it creates.
    open(PAT)
    expect(document.querySelector('.ur-reg')).not.toBeNull()
  })

  it('G4-22: the terminal path is folded behind one disclosure; Rotate opens it too', async () => {
    tokenRoutes(undefined, { admin: true })
    await mount('page=tokens')
    await waitFor(() => expect(within(tokCards()[1]!).queryByRole('button', { name: 'Rotate' })).not.toBeNull(), WAIT)
    const toggle = screen.getByRole('button', { name: PAT })
    // Folded on arrival.
    expect(document.querySelector('.ur-reg')).toBeNull()
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    const reg = document.querySelector<HTMLElement>('.ur-reg')!
    expect(reg.id).toBe(toggle.getAttribute('aria-controls'))
    // The same button folds it again.
    fireEvent.click(toggle)
    expect(document.querySelector('.ur-reg')).toBeNull()
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    // Rotate opens it on that entry's command.
    fireEvent.click(within(tokCards()[1]!).getByRole('button', { name: 'Rotate' }))
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(visible(document.querySelector('.ur-reg pre.ur-term'))).toBe('$ scripts/create-secrets.sh --tenant eng --provider git-r-0a1b2c3d4e5f6071 --stdin')
    // No second way in: the header carries no Register button any more.
    expect(screen.queryByRole('button', { name: /register token/i })).toBeNull()
  })

  it('no tokens is an empty state that points at the steps and the terminal path', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/git-tokens' ? { status: 200, body: { git_tokens: [] } } : null))
    await mount('page=tokens')
    // The token region's empty state, not the first on the page: the steps
    // card above it draws its own when its route is not served.
    const empties = () => Array.from(document.querySelectorAll('.c-emp')).map(visible)
    await waitFor(() => expect(empties().some((t) => t.includes('No git tokens registered'))).toBe(true), WAIT)
    expect(empties().join(' ')).not.toMatch(/\bslot\b/)
  })
})

// ---------------------------------------------------------------------------

const CAP_OK = { state: 'ok', reason: 'Measured by a read the token was allowed to make' }
const CAP_UNK = { state: 'unknown', reason: 'Fine-grained grants are not readable from the API; the role allows it' }
const allCaps = (over: Record<string, unknown> = {}) => ({
  clone: CAP_OK, push: CAP_OK, open_prs: CAP_UNK, read_checks: CAP_OK, merge: CAP_UNK, close_issues: CAP_UNK, read_issues: CAP_OK, workflow_dispatch: CAP_UNK, ...over,
})

const PERMS = {
  order: 'R2',
  rows: [
    { repo_id: 'repo_0a1b2c3d4e5f6071', repository: 'example-org/example-api', token: TOKENS.git_tokens[1], resolves: true, capabilities: allCaps({ merge: { state: 'missing', reason: 'The branch rules on main allow merges only by the merge App' } }) },
    { repo_id: 'repo_0a1b2c3d4e5f6071', repository: 'example-org/example-api', token: TOKENS.git_tokens[0], resolves: false, capabilities: allCaps({ push: { state: 'missing', reason: 'Push service advertisement refused' } }) },
    { repo_id: 'repo_1111111111111111', repository: 'example-org/example-web', token: TOKENS.git_tokens[0], resolves: true, capabilities: { clone: CAP_OK } },
  ],
}

// The matrix is built from the probe summaries GET /v1/git-tokens carries: no
// permissions route exists, so none is called.
const PERMS_TOKENS = {
  resolution_order: 'R2',
  git_tokens: TOKENS.git_tokens.map((t) => ({
    ...t,
    probe: {
      repositories: PERMS.rows
        .filter((r) => r.token?.token_id === t.token_id)
        .map(({ repo_id, repository, capabilities }) => ({ repo_id, repository, capabilities })),
    },
  })),
}

const gridRows = () => Array.from(document.querySelectorAll<HTMLTableRowElement>('table.ur-mx tbody tr:not(.ur-grp)'))

describe('the permission matrix is a grid of tokens × repositories (pick A)', () => {
  it('groups rows by repository, marks the resolving token, and heads the capability columns', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/git-tokens' ? { status: 200, body: PERMS_TOKENS } : null))
    await mount('page=permissions')
    await waitFor(() => expect(gridRows()).toHaveLength(3), WAIT)
    expect(document.querySelector('h1')?.textContent).toBe('Permissions')
    expect(visible(document.querySelector('.ur-crumb'))).toBe('Config › Repositories › Git tokens › Permissions')
    // #138: the order chip is an action in the head; the tokens × repositories count is the note over the grid.
    expect(visible(document.querySelector('.c-count-note'))).toContain('2 tokens × 2 repositories')
    expect(visible(document.querySelector('.c-phead .c-acts'))).toContain('order R2')
    expect(visible(document.querySelector('.c-phead'))).not.toContain('tokens ×')
    const heads = Array.from(document.querySelectorAll('table.ur-mx thead th')).map((t) => visible(t))
    expect(heads).toEqual(['Token', 'Clone', 'Push branches', 'Open PRs', 'Read checks', 'Merge', 'Close issues', 'Read issues', 'Workflow dispatch', 'Expires in', 'Verified'])
    expect(Array.from(document.querySelectorAll('tr.ur-grp')).map((g) => visible(g))).toEqual(['example-org/example-api', 'example-org/example-web'])
    expect(visible(gridRows()[0]!.cells[0]!)).toBe('example-api-bot · repository resolves')
    expect(visible(gridRows()[1]!.cells[0]!)).toBe('eng-swarm-bot · tenant')
  })

  it('each cell is ok, missing or unknown with its reason as its title; an unserved cell is unknown, never ok', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/git-tokens' ? { status: 200, body: PERMS_TOKENS } : null))
    await mount('page=permissions')
    await waitFor(() => expect(gridRows()).toHaveLength(3), WAIT)
    const cell = (r: number, c: number) => gridRows()[r]!.cells[c]!.querySelector<HTMLElement>('.ur-cap')!
    expect(cell(0, 1).getAttribute('data-cap')).toBe('ok')
    expect(cell(0, 5).getAttribute('data-cap')).toBe('missing')
    expect(cell(0, 5).getAttribute('title')).toBe('Merge: missing. The branch rules on main allow merges only by the merge App')
    expect(cell(0, 3).getAttribute('data-cap')).toBe('unknown')
    expect(visible(cell(0, 3))).toBe('unknown')
    expect(cell(1, 2).getAttribute('data-cap')).toBe('missing')
    // The web row served only clone: every other cell is unknown, not ok.
    expect([2, 3, 4, 5, 6, 7, 8].map((c) => cell(2, c).getAttribute('data-cap'))).toEqual(Array(7).fill('unknown'))
    expect(visible(gridRows()[0]!.cells[9]!)).toBe('41 d')
    expect(visible(gridRows()[1]!.cells[9]!)).toBe('9 d')
    expect(gridRows()[1]!.cells[9]!.querySelector('[data-tone]')?.getAttribute('data-tone')).toBe('warn')
    expect(visible(gridRows()[0]!.cells[10]!)).toBe('3h ago')
    expect(tokenShapedIn(document.documentElement.outerHTML)).toEqual([])
  })

  it('Verify all re-probes every token in the grid once, then re-reads', async () => {
    let reads = 0
    const calls = serve((m, url) => {
      if (m === 'GET' && url === '/v1/git-tokens') {
        reads += 1
        return { status: 200, body: PERMS_TOKENS }
      }
      if (m === 'POST' && url.endsWith('/verify')) return { status: 202, body: {} }
      return null
    })
    await mount('page=permissions')
    await waitFor(() => expect(gridRows()).toHaveLength(3), WAIT)
    fireEvent.click(screen.getByRole('button', { name: 'Verify all' }))
    await waitFor(() => expect(reads).toBe(2), WAIT)
    expect(calls.filter((c) => c.method === 'POST').map((c) => c.url).sort()).toEqual([
      '/v1/git-tokens/tok_0011223344556677/verify',
      '/v1/git-tokens/tok_r/verify',
    ])
  })

  it('"Resolving only" hides the rows that would never be used', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/git-tokens' ? { status: 200, body: PERMS_TOKENS } : null))
    await mount('page=permissions')
    await waitFor(() => expect(gridRows()).toHaveLength(3), WAIT)
    fireEvent.click(screen.getByRole('radio', { name: 'Resolving only' }))
    expect(gridRows()).toHaveLength(2)
  })

  it('the token route not being there yet is "not served yet", and no permissions route is ever fetched', async () => {
    const calls = serve(() => null)
    await mount('page=permissions')
    await waitFor(() => expect(document.querySelector('[data-notserved="GET /v1/git-tokens"]')).not.toBeNull(), WAIT)
    expect(calls.some((c) => c.url.includes('/permissions'))).toBe(false)
  })

  it('G4-17: at 1440 the capability heads wrap in 72 px columns and the last head reads "Verified"', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/git-tokens' ? { status: 200, body: PERMS_TOKENS } : null))
    await mount('page=permissions')
    await waitFor(() => expect(gridRows()).toHaveLength(3), WAIT)
    const sheets = ALL.map(([, t]) => t).join('\n')
    const heads = Array.from(document.querySelectorAll<HTMLElement>('table.ur-mx thead th'))
    const caps = heads.slice(1, 9)
    expect(caps.map((h) => visible(h))).toEqual(['Clone', 'Push branches', 'Open PRs', 'Read checks', 'Merge', 'Close issues', 'Read issues', 'Workflow dispatch'])
    // Each nowrap head held its whole label on one line, which is what pushed the
    // table 18 px past its card at 1440. The eight capability heads now wrap and
    // their columns are 72 px, free to shrink to it.
    for (const h of caps) {
      expect(cascade(sheets, h, 'white-space', { width: 1440 }).winner?.value).toBe('normal')
      expect(cascade(sheets, h, 'width', { width: 1440 }).winner?.value).toBe('72px')
      expect(cascade(sheets, h, 'min-width', { width: 1440 }).winner?.value).toBe('0')
    }
    // The last head is one short word on one line, and says in full what it is.
    const last = heads[heads.length - 1]!
    expect(visible(last)).toBe('Verified')
    expect(last.getAttribute('title')).toBe('Last verified')
    expect(cascade(sheets, last, 'white-space', { width: 1440 }).winner?.value).toBe('nowrap')
  })

  it('at 390 the grid gives way to per-repository cards', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/git-tokens' ? { status: 200, body: PERMS_TOKENS } : null))
    await mount('page=permissions')
    await waitFor(() => expect(gridRows()).toHaveLength(3), WAIT)
    const sheets = ALL.map(([, t]) => t).join('\n')
    const wide = document.querySelector('.ur-mx-wide')!
    const phone = document.querySelector('.ur-mx-cards')!
    expect(cascade(sheets, wide, 'display', { width: 390 }).winner?.value).toBe('none')
    expect(cascade(sheets, phone, 'display', { width: 1440 }).winner?.value).toBe('none')
    expect(cascade(sheets, phone, 'display', { width: 390 }).winner?.value).not.toBe('none')
    const cards = Array.from(phone.querySelectorAll('.ur-mxcard'))
    expect(cards.map((c) => visible(c.querySelector('h2')))).toEqual(['example-org/example-api', 'example-org/example-web'])
    expect(visible(cards[0]!)).toContain('Clone with example-api-bot')
  })
})

// CONFIG-COPY (part of #2157): Repositories is a Config page since CONFIG-NAV,
// so the breadcrumb says Config, and its Repositories step links the Config path.
describe('the Git tokens breadcrumbs name Config, not Work (CONFIG-COPY)', () => {
  const OLD = /\bWork › (Setup|Access|Repositories)|(?<!Workspace )Settings › (Workspace|MCP servers)/

  for (const view of ['page=tokens', 'page=permissions']) {
    it(`${view}: no old address is drawn and Repositories links /config/repositories`, async () => {
      tokenRoutes()
      await mount(view)
      await waitFor(() => expect(document.querySelector('.ur-crumb')).toBeTruthy(), WAIT)
      const crumb = document.querySelector<HTMLElement>('.ur-crumb')!
      expect(visible(crumb)).toMatch(/^Config › Repositories › Git tokens/)
      expect(within(crumb).getByRole('link', { name: 'Repositories' }).getAttribute('href')).toBe('/config/repositories')
      expect(visible(document.body)).not.toMatch(OLD)
    })
  }
})
