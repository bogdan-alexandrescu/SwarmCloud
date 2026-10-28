// #1857, RAW-PATCH-FIXTURE: THE OWNER-ONLY RAW PATCH, BOTH WAYS, IN THE
// FIXTURE CONSOLE.
//
// The raw patch is offered to a task's submitter only (artifacts.rawpatch).
// Every fixture agent was submitted by the signed-in fixture user, so the
// fixture console only ever showed the offer. BGD, 2026-10-27: one agent a
// different user submitted, reached by id and kept off the lists, so the
// console shows your own agent's `diff.patch` with "Download raw patch" and
// someone else's with the redacted download alone.
//
// Through <App /> on the fixture build: nothing is stubbed, so the signed-in
// user, both tasks and both listings are the fixtures' own reads.
//
// MUTATIONS, each a case below: drop the other user's agent from the by-id
// lookup (its Artifacts tab never lists a patch); give it the signed-in
// user's `submitted_by` (it offers the raw patch); give the user's own agent
// another submitter in `fixtureTaskRows` (the own case loses its offer).

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import { loadAgentDetail, loadMe, loadTasks } from '../api'

vi.mock('../Agents', () => ({ AgentsScreen: () => null }))

const OTHER = 'task_patch_other_submitter'
const OWN = 'task_99284d38'
const LONG = 20_000

async function openArtifacts(id: string): Promise<void> {
  window.location.hash = `#work/task/${id}/artifacts`
  const { App } = await import('../App')
  render(<App />)
}

function outputs(): HTMLElement {
  const drawer = document.querySelector<HTMLElement>('.ctl-drawer')
  expect(drawer, 'no agent drawer is open').not.toBeNull()
  const s = [...drawer!.querySelectorAll<HTMLElement>('section')].find((x) => x.querySelector('h2')?.textContent === 'Outputs')
  expect(s, 'no Outputs section').toBeTruthy()
  return s!
}

function patchRow(): HTMLTableRowElement {
  const r = [...outputs().querySelectorAll<HTMLTableRowElement>('tbody tr')].find((tr) =>
    (tr.querySelector('th')?.textContent ?? '').trim().startsWith('diff.patch'),
  )
  expect(r, 'no row for diff.patch').toBeTruthy()
  return r!
}

const links = (row: HTMLElement, text: string) => [...row.querySelectorAll<HTMLAnchorElement>('a')].filter((a) => a.textContent === text)

/** The patch row is listed, and the signed-in read has had time to draw the offer if it would. */
async function listed(): Promise<HTMLTableRowElement> {
  const row = await waitFor(() => patchRow(), { timeout: LONG })
  // `loadMe` answers in 150 ms in the fixture build; one read after it has
  // landed, so an offer that WOULD be drawn has been.
  await loadMe()
  await new Promise((r) => setTimeout(r, 60))
  return row
}

afterEach(() => {
  window.location.hash = ''
})

describe('the fixture build has an agent another user submitted', () => {
  it('answers it by id, finished, submitted by someone other than the signed-in user, and keeps it off the list', async () => {
    expect(import.meta.env.DEV, 'this file reads the fixture build').toBeTruthy()
    const [me, other, own, list] = await Promise.all([loadMe(), loadAgentDetail(OTHER), loadAgentDetail(OWN), loadTasks()])
    expect(me.status).toBe('ok')
    expect(other.status).toBe('ok')
    expect(own.status).toBe('ok')
    const email = me.status === 'ok' ? me.data.principal.email : null
    const otherTask = other.status === 'ok' ? other.data.task : null
    const ownTask = own.status === 'ok' ? own.data.task : null
    expect(otherTask?.state).toBe('SUCCEEDED')
    expect(otherTask?.submitted_by).toMatch(/@example\.com$/)
    expect(otherTask?.submitted_by).not.toBe(email)
    expect(ownTask?.submitted_by).toBe(email)
    // Off the lists, as the budget agents are: every count other tests pin stands.
    const ids = list.status === 'ok' ? list.data.tasks.map((t) => t.id) : []
    expect(ids).toContain(OWN)
    expect(ids).not.toContain(OTHER)
  })
})

describe("the fixture console shows the raw patch both ways", () => {
  it("the user's own agent offers 'Download raw patch' beside the download, and says it is theirs as its submitter", async () => {
    await openArtifacts(OWN)
    const row = await listed()
    await waitFor(() => expect(links(row, 'Download raw patch')).toHaveLength(1), { timeout: LONG })
    expect(links(row, 'download')).toHaveLength(1)
    expect(row.textContent).toMatch(/not redacted: the patch as stored, for you as its submitter/)
  }, 30_000)

  it("another user's agent offers only the redacted download, without the raw patch or its note", async () => {
    await openArtifacts(OTHER)
    const row = await listed()
    expect(links(row, 'download'), 'the redacted download is not offered').toHaveLength(1)
    expect(new URL(links(row, 'download')[0]!.getAttribute('href') ?? '', 'http://ui.test').searchParams.has('redaction')).toBe(false)
    expect(links(outputs(), 'Download raw patch')).toHaveLength(0)
    expect(outputs().textContent).not.toMatch(/not redacted/)
  }, 30_000)
})
