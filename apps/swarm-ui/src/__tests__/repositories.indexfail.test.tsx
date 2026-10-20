// WORK › REPOSITORIES, a repository whose index runs fail (lane INDEX-FAIL,
// operator QA p14, 2026-10-13).
//
// In tenant u-bogdan every index run of example-org/ai-studio failed on its clone
// ("Repository not found ... Authentication failed") while its card said only
// "no index" or "indexing now". WHAT EACH CASE HOLDS:
//   * the card names the last failed run: its cause, short, when, and a link
//     to the task;
//   * an access failure says what to do -- grant SwarmCloud's GitHub access
//     under Setup › Git tokens -- and links there;
//   * a poll the API stopped (`index.backoff`) says so, and that Index now
//     retries;
//   * a transient failure says the next run retries, and sends nobody to
//     Git tokens;
//   * a registration with no failure draws no failure line;
//   * nothing token-shaped reaches the card, even if the API served it.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'
import { MIN, ago, leakedValue, repo, serve, tokenShapedIn, visible } from './repofixture'

const WAIT = { timeout: 4000 }

async function mount(go = vi.fn()) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  const utils = render(<RepositoriesScreen view={null} go={go} />)
  return { ...utils, go }
}

afterEach(() => {
  vi.unstubAllEnvs()
})

function cards(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('.ur-cards > .ur-card'))
}

const ACCESS = {
  task_id: 'task_6ec590237ef54dc68837',
  commit_sha: null,
  at: ago(15 * MIN),
  class: 'access',
  cause: "SwarmCloud's GitHub access can't read this repository",
  detail: "remote: Repository not found. fatal: Authentication failed for 'https://github.com/example-org/example-api/'",
  end_cause: 'runner_error',
}

function listing(index: Record<string, unknown>) {
  serve((m, url) =>
    m === 'GET' && url === '/v1/repositories'
      ? { status: 200, body: { repositories: [repo({}, { current_sha: null, behind_by: null, coverage: null, last_indexed_at: null, ...index })] } }
      : null,
  )
}

describe('a repository whose index runs fail says why on its card', () => {
  it('names the last failure, its cause, when, the task, and what to do', async () => {
    listing({ last_failure: ACCESS, failure_streak: { class: 'access', count: 1 } })
    const { go } = await mount()
    await waitFor(() => expect(cards()).toHaveLength(1), WAIT)
    const fail = cards()[0]!.querySelector('.ur-fail')
    expect(fail).not.toBeNull()
    const text = visible(fail)
    expect(text).toContain('Last index run failed')
    expect(text).toContain("SwarmCloud's GitHub access can't read this repository")
    expect(text).toContain('15m ago')
    expect(text).toContain('grant it under Setup › Git tokens')
    expect(fail!.getAttribute('data-class')).toBe('access')
    // The git line is kept, short, as the reason's title -- not drawn as a wall of text.
    expect(fail!.querySelector('.ur-fail-cause')?.getAttribute('title')).toContain('Repository not found')

    const task = Array.from(fail!.querySelectorAll('a')).find((a) => visible(a).includes('agent'))
    expect(task).toBeDefined()
    fireEvent.click(task!)
    expect(go).toHaveBeenCalledWith('work/task/task_6ec590237ef54dc68837')

    const tokens = Array.from(fail!.querySelectorAll('a')).find((a) => visible(a) === 'Setup › Git tokens')
    expect(tokens).toBeDefined()
    fireEvent.click(tokens!)
    expect(go).toHaveBeenCalledWith('work/repositories?page=tokens')
  })

  it('says the poll stopped, and that Index now retries', async () => {
    listing({
      last_failure: ACCESS,
      failure_streak: { class: 'access', count: 2 },
      backoff: { since: ago(10 * MIN), class: 'access', failures: 2, reason: 'polling stopped after 2 runs failed', credential: 'abc' },
    })
    await mount()
    await waitFor(() => expect(cards()).toHaveLength(1), WAIT)
    const fail = cards()[0]!.querySelector('.ur-fail')
    const text = visible(fail)
    expect(text).toContain('Polling stopped after 2 failed runs')
    expect(text).toContain('Index now retries')
    expect(fail!.getAttribute('data-backoff')).toBe('1')
  })

  it('says a transient failure retries, and sends nobody to Git tokens', async () => {
    listing({
      last_failure: { ...ACCESS, class: 'transient', cause: 'the repository could not be reached; the next run retries', detail: 'Could not resolve host: github.com' },
    })
    await mount()
    await waitFor(() => expect(cards()).toHaveLength(1), WAIT)
    const fail = cards()[0]!.querySelector('.ur-fail')
    expect(visible(fail)).toContain('the next run retries')
    expect(visible(fail)).not.toContain('Git tokens')
    expect(fail!.getAttribute('data-class')).toBe('transient')
  })

  it('draws no failure line for a registration with none', async () => {
    listing({ last_failure: null })
    await mount()
    await waitFor(() => expect(cards()).toHaveLength(1), WAIT)
    expect(cards()[0]!.querySelector('.ur-fail')).toBeNull()
  })

  it('never draws a token, even one the API served', async () => {
    const leaked = leakedValue()
    listing({ last_failure: { ...ACCESS, cause: `access refused ${leaked}`, detail: `https://x:${leaked}@github.com/x` } })
    await mount()
    await waitFor(() => expect(cards()).toHaveLength(1), WAIT)
    expect(cards()[0]!.querySelector('.ur-fail')).not.toBeNull()
    expect(tokenShapedIn(document.body.outerHTML)).toEqual([])
  })
})
