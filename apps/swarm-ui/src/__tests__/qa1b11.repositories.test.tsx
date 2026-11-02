// QA1B-11 (part of #2596): QA1-FIX-20's Repositories.tsx half, applied by
// pass 1b.
//
//   config-15  Each repository had two controls labelled `Remove`: one in the
//              reach table (drops the GitHub grant) and one on its registered
//              card (unregisters it). The card's now says `Unregister`.
//   config-19  `3 registered` floated alone between the reach table and the
//              cards. The count heads the card grid as a heading,
//              `Registered repositories · 3`.
//
// BREAK IT: label the card's button `Remove` again, or put the count back in
// a `<CountNote>` reading `${count} registered` -- a case below turns red.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor, within } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

const WEB = () => repo({ repo_id: 'repo_1111111111111111', repo: 'example-web' })

async function mount(chosen: string[] = []) {
  serve((m, url) => {
    const path = url.split('?')[0]
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [repo(), WEB()] } }
    if (m === 'GET' && path === '/v1/access') {
      return {
        status: 200,
        body: {
          connection: { state: 'active', forge_login: 'octo-dev' }, orgs: [], tenant_id: 'eng',
          grants: chosen.map((repository, i) => ({
            repo_id: `repo_99999999999999${i}${i}`, repository, owner: repository.split('/')[0], mode: 'read',
            can_push: false, archived: false, granted_at: null, granted_by: 'dev@swarm.example.com',
            checks: { clone: { state: 'ok', code: null, checked_at: null } }, verified_at: null,
          })),
        },
      }
    }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view={null} go={vi.fn()} />)
}

const card = (name: string) =>
  Array.from(document.querySelectorAll<HTMLElement>('.ur-cards > .ur-card')).find((c) => visible(c.querySelector('h2')) === name) ?? null

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('config-19: the count heads the card grid', () => {
  it('is a heading reading `Registered repositories · 2`, not a lone `2 registered` note', async () => {
    await mount()
    await waitFor(() => expect(card('example-org/example-web')).not.toBeNull(), WAIT)
    const head = document.querySelector<HTMLElement>('.c-count-note')!
    // MUTATION: restore `<CountNote>{meta}</CountNote>` and the note is a <p>.
    expect(head.tagName).toBe('H3')
    // MUTATION: restore `${count} registered` and the heading says only that.
    expect(visible(head)).toBe('Registered repositories · 2')
  })

  it('still counts the dashed cards apart', async () => {
    await mount(['example-labs/console'])
    await waitFor(() => expect(visible(document.querySelector('.c-count-note'))).toContain('chosen, not registered'), WAIT)
    expect(visible(document.querySelector('.c-count-note'))).toBe('Registered repositories · 2 · 1 chosen, not registered')
  })
})

describe('config-15: the registered card unregisters, and says so', () => {
  it('labels the card’s control `Unregister`, and no card has a `Remove`', async () => {
    await mount()
    await waitFor(() => expect(card('example-org/example-web')).not.toBeNull(), WAIT)
    // MUTATION: label the button `Remove` again and this throws.
    expect(within(card('example-org/example-web')!).getByRole('button', { name: 'Unregister' })).toBeTruthy()
    for (const c of document.querySelectorAll<HTMLElement>('.ur-cards > .ur-card')) {
      expect(within(c).queryByRole('button', { name: 'Remove' })).toBeNull()
    }
  })
})
