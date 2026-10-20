// THE FIVE PAGES THAT KEPT THEIR REFRESH BESIDE A CARD TITLE (lane
// QAFIX-PAGEHEAD; visual QA finding QA-066, part of #1680).
//
// Every other page of the console draws its refresh in the page head, right
// of the title, as Access does (`<PageHead><UrRefresh …/></PageHead>`). Five
// drew it as a Card `action`, beside the first card's title, and four of them
// drew no `?` at all because a Settings page has no section to lend it one.
//
// WHAT EACH CASE HOLDS:
//   * the page's one refresh is in the page head's actions (`.c-phead .c-acts`)
//     and no card head (`.c-card-h`) carries one;
//   * on the four Settings-style pages the head carries the page's own `?`,
//     for its own topic, which exists in the registry;
//   * on Admin › People, whose Workspace roles card had the second refresh,
//     the head's one refresh re-reads that card's list too. People takes the
//     Admin section's `?` (`SectionHelp`), which this unit render has no
//     section to supply, so its `?` is not asserted here.
//
// Each assertion's MUTATION names the change that turns it red.
//
// Fixtures only; no network. Nothing here is credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'
import type { ReactElement } from 'react'
import { serve } from './repofixture'
import { HELP, type TopicId } from '../help'

const WAIT = { timeout: 4000 }

afterEach(() => {
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

async function live<T>(load: () => Promise<T>): Promise<T> {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return load()
}

/** The page head, once it is drawn. */
async function head(container: HTMLElement): Promise<HTMLElement> {
  return waitFor(() => {
    const h = container.querySelector<HTMLElement>('.c-phead')
    expect(h, 'no page head').not.toBeNull()
    return h!
  }, WAIT)
}

/** The refresh sits in the head's actions, and in no card's head. */
async function refreshInHead(container: HTMLElement) {
  const h = await head(container)
  await waitFor(() => expect(h.querySelector('.c-acts .c-refresh'), 'the page head carries no refresh').not.toBeNull(), WAIT)
  expect(container.querySelectorAll('.c-card-h .c-refresh').length, 'a card head still carries a refresh').toBe(0)
  expect(container.querySelectorAll('.c-refresh').length, 'the page draws more than one refresh').toBe(1)
}

/** The head's `?` is the page's own topic, by the title its button is named for. */
async function helpInHead(container: HTMLElement, topic: string) {
  const t = HELP[topic as TopicId]
  expect(t, `${topic} is not a topic in help.ts`).toBeDefined()
  const h = await head(container)
  const q = within(h.querySelector<HTMLElement>('.head')!).getByRole('button', { name: `Help: ${t.title}` })
  expect(q.className).toContain('help-glyph')
}

type Case = {
  page: string
  topic: string
  mount: () => Promise<ReactElement>
  routes: (m: string, url: string) => { status: number; body: unknown } | null
  mutation: string
}

const CASES: Case[] = [
  {
    page: 'Settings › People and roles',
    topic: 'workspace-roles',
    // MUTATION: put `action={<UrRefresh reads={[roles]} />}` back on the
    // "Roles in this workspace" card, or drop `help=` from its PageHead.
    mutation: 'WorkspacePeople.tsx',
    mount: async () => {
      const { WorkspacePeopleScreen } = await live(() => import('../WorkspacePeople'))
      return <WorkspacePeopleScreen />
    },
    routes: () => null,
  },
  {
    page: 'Settings › MCP servers',
    topic: 'mcp-servers',
    // MUTATION: put `action={<UrRefresh reads={[list]} />}` back on the
    // "Registered MCP servers" card, or drop `help=` from its PageHead.
    mutation: 'McpServers.tsx',
    mount: async () => {
      const { McpServersScreen } = await live(() => import('../McpServers'))
      return <McpServersScreen />
    },
    routes: () => null,
  },
  {
    page: 'Settings › Workspace',
    topic: 'code-intelligence',
    // MUTATION: put `action={<UrRefresh reads={[ci, judgment]} />}` back on
    // the "Code intelligence" card, or drop `help=` from its PageHead.
    mutation: 'CodeIntelligence.tsx',
    mount: async () => {
      const { WorkspaceSettingsScreen } = await live(() => import('../CodeIntelligence'))
      return <WorkspaceSettingsScreen />
    },
    routes: () => null,
  },
  {
    page: 'Connect GitHub, connected',
    topic: 'github-connect',
    // MUTATION: put `action={<UrRefresh reads={[doc]} />}` back on the
    // "Connected as @…" card, or drop `help=` from `CallbackHead`.
    mutation: 'GitHubConnect.tsx',
    mount: async () => {
      // Built at run time: nothing added may look like a credential.
      const code = ['qa', 'code', 'x'.repeat(8)].join('-')
      const state = ['qa', 'state', 'y'.repeat(8)].join('-')
      window.history.replaceState(null, '', `/onboarding/github/callback?code=${code}&state=${state}`)
      const { GitHubCallbackScreen } = await live(() => import('../GitHubConnect'))
      return <GitHubCallbackScreen go={() => undefined} />
    },
    routes: (m, url) => {
      if (m === 'POST' && url === '/v1/onboarding/github/exchange') return { status: 200, body: { connection: { forge_login: 'octo-dev' } } }
      return null
    },
  },
]

describe('QA-066: a settings page draws its refresh in the page head, with its own ?', () => {
  for (const c of CASES) {
    it(`${c.page}: refresh in the head, not in a card's action slot (${c.mutation})`, async () => {
      serve(c.routes)
      const { container } = render(await c.mount())
      if (c.topic === 'github-connect') {
        await waitFor(() => expect(container.textContent).toMatch(/Connected as @octo-dev/), WAIT)
      }
      await refreshInHead(container)
    })

    it(`${c.page}: the head carries the ${c.topic} topic's ? (${c.mutation})`, async () => {
      serve(c.routes)
      const { container } = render(await c.mount())
      await helpInHead(container, c.topic)
    })
  }

  it('Connect GitHub, before or without a connection: the same head, its ?, and no refresh', async () => {
    // MUTATION: render a second, unhelped `<PageHead title="Connect GitHub" />`
    // for the posting, missing and refused views.
    serve(() => null)
    const { GitHubCallbackScreen } = await live(() => import('../GitHubConnect'))
    const { container } = render(<GitHubCallbackScreen go={() => undefined} />)
    await waitFor(() => expect(container.textContent).toMatch(/Nothing to finish here/), WAIT)
    expect(container.querySelectorAll('.c-phead').length).toBe(1)
    await helpInHead(container, 'github-connect')
    expect(container.querySelectorAll('.c-refresh').length, 'a refresh with nothing to re-read').toBe(0)
  })
})

describe('QA-066: Admin › People has one refresh, and it re-reads the Workspace roles card', () => {
  const PEOPLE = {
    people: [],
    count: 0,
    pending: 0,
    audit: [],
    lendable_accounts: [],
    lendable_error: null,
    admins: [],
    admins_error: null,
  }
  const LIST = { workspaces: [], roles: [], next_page_token: null }

  it('draws the refresh in the page head, not on the Workspace roles card (WorkspaceRolesAdmin.tsx)', async () => {
    // MUTATION: put `action={<UrRefresh reads={[first]} />}` back on the
    // Workspace roles card.
    const calls = serve((m, url) => {
      if (m === 'GET' && url === '/v1/admin/people') return { status: 200, body: PEOPLE }
      if (m === 'GET' && url.startsWith('/v1/admin/workspaces/roles')) return { status: 200, body: LIST }
      return null
    })
    const { PeopleScreen } = await live(() => import('../People'))
    const { container } = render(<PeopleScreen />)
    await waitFor(() => expect(container.querySelector('.wra-card'), 'no Workspace roles card').not.toBeNull(), WAIT)
    await refreshInHead(container)

    // MUTATION: leave the roles list out of the head's `UrRefresh reads`.
    const reads = () => calls.filter((c) => c.method === 'GET' && c.url.startsWith('/v1/admin/workspaces/roles')).length
    await waitFor(() => expect(reads()).toBe(1), WAIT)
    const press = () => container.querySelector<HTMLButtonElement>('.c-phead .c-acts .c-refresh')!
    await waitFor(() => expect(press().disabled, 'the head refresh is still reading').toBe(false), WAIT)
    fireEvent.click(press())
    await waitFor(() => expect(reads(), 'the head refresh did not re-read the roles list').toBe(2), WAIT)
  })
})
