// INDEX HISTORY KEPT, SET PER REPOSITORY (lane INDEX-RETENTION, part of #1693).
//
// Owner, 2026-10-20: "Retention should be set from repository settings
// section for each repository". `index.keep_versions` is how many past index
// versions the repository keeps (1-100, default 20); the promoted one and the
// run's own are always kept on top. It is written through the same PATCH as
// the change trigger, by the same members.
//
// WHAT EACH CASE HOLDS:
//   * the served value is shown in a number input with its range;
//   * Save is one PATCH carrying only { index: { keep_versions } };
//   * a refused write keeps the served value and says why in words;
//   * a value outside 1-100 is refused in words before anything is sent;
//   * what is held now ("163 copies, 880 MB") when the last sweep served it,
//     "not read" otherwise, and about how many copies the next run deletes;
//   * the hint above the cards names this setting among what CAN be changed.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { repo, serve, sha, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const ID = 'repo_5555555555555555'
const MB = 1024 * 1024

afterEach(() => vi.unstubAllEnvs())

function detail(index: Record<string, unknown>) {
  return {
    repository: {
      ...repo({ repo_id: ID, repo: 'ai-studio' }, { current_sha: sha('9f8e7d6'), head_sha: sha('9f8e7d6'), ...index }),
      graph: { depth: 3, min_confidence: 0.2 },
      selection_policy: { policy: null, mode: null, inherited_from_tenant: true },
    },
    index_runs: [],
    used_by: [],
  }
}

function routes(start: Record<string, unknown>, write: (m: string, url: string, body: unknown) => { status: number; body: unknown } | null) {
  let current: unknown = detail(start)
  return serve((m, url, body) => {
    if (m === 'GET' && url === `/v1/repositories/${ID}`) return { status: 200, body: current }
    if (m !== 'GET') {
      const r = write(m, url, body)
      if (r !== null && r.status < 300) current = r.body
      return r
    }
    return null
  })
}

async function mount() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  render(<RepositoriesScreen view={`repo=${ID}&tab=settings`} go={vi.fn()} />)
  await waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/ai-studio'), WAIT)
}

const input = () => screen.findByRole('spinbutton', { name: 'Index history kept' }, WAIT) as Promise<HTMLInputElement>
const control = async () => (await input()).closest('.ur-keep') as HTMLElement

const HELD = { retention: { status: 'ok', held_copies: 163, held_bytes: 880 * MB, deleted_copies: 0, kept_commits: 21, task_id: 'task_1' } }

describe('Settings: index history kept', () => {
  it('shows the served value with its range, and what is held now', async () => {
    routes({ keep_versions: 20, ...HELD }, () => null)
    await mount()
    const box = await input()
    // MUTATION: an input that ignores the served keep_versions.
    expect(box.value).toBe('20')
    expect(box.min).toBe('1')
    expect(box.max).toBe('100')
    const words = visible(await control())
    expect(words).toContain('163 copies, 880 MB')
    // 163 held, 20 kept: the next run deletes the rest.
    expect(words).toMatch(/next run deletes about 143 copies/)
  })

  it('says "not read" when no sweep has reported what is held', async () => {
    routes({ keep_versions: 20 }, () => null)
    await mount()
    // MUTATION: drawing 0 copies for a reading that was never made.
    expect(visible(await control())).toMatch(/not read/)
  })

  it('Save is one PATCH carrying only index.keep_versions', async () => {
    const seen: unknown[] = []
    routes({ keep_versions: 20, ...HELD }, (m, url, body) => {
      if (m === 'PATCH' && url === `/v1/repositories/${ID}`) {
        seen.push(body)
        return { status: 200, body: detail({ keep_versions: 5, ...HELD }) }
      }
      return null
    })
    await mount()
    fireEvent.change(await input(), { target: { value: '5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save index history' }))
    // MUTATION: a PATCH that sends the whole index block, or a string.
    await waitFor(() => expect(seen).toEqual([{ index: { keep_versions: 5 } }]), WAIT)
    await waitFor(async () => expect((await input()).value).toBe('5'), WAIT)
  })

  it('a refused write keeps the served value and says why in words', async () => {
    routes({ keep_versions: 20 }, (m) => (m === 'PATCH' ? { status: 403, body: { code: 'forbidden', message: 'Not a member of this workspace.' } } : null))
    await mount()
    fireEvent.change(await input(), { target: { value: '7' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save index history' }))
    // MUTATION: swallowing the error.
    await waitFor(() => expect(document.body.textContent).toContain('The index history kept was not changed'), WAIT)
    expect(document.body.textContent).toContain('Not a member of this workspace.')
  })

  it('a value outside 1-100 is refused in words and nothing is sent', async () => {
    const seen: unknown[] = []
    routes({ keep_versions: 20 }, (m, _url, body) => {
      if (m === 'PATCH') seen.push(body)
      return null
    })
    await mount()
    fireEvent.change(await input(), { target: { value: '101' } })
    const save = screen.getByRole('button', { name: 'Save index history' }) as HTMLButtonElement
    // MUTATION: no client-side range check.
    expect(save.disabled).toBe(true)
    expect(visible(await control())).toMatch(/between 1 and 100/i)
    fireEvent.click(save)
    expect(seen).toEqual([])
  })

  it('the hint names this setting among what can be changed', async () => {
    routes({ keep_versions: 20 }, () => null)
    await mount()
    await input()
    // MUTATION: leaving PATCH_WHY saying only Code intelligence and the trigger can change.
    expect(visible(document.querySelector('.ur-locked'))).toContain('index history kept')
  })
})
