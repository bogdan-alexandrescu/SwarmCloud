// FIXTURE-CARD (part of #1680): Pool limits' Additional repositories card
// reads its settings through `loadPlatformSettings()`, not a bare read of
// `/v1/admin/settings`. Visual QA run 4 found the bare read (admin-001, F1):
// no fixture answered it, so the fixture build drew 'Not read' and the Reads
// footer on admin/limits counted a failed read on a screen with nothing wrong.
//
// Both builds are read here through the REAL modules, re-imported per case:
// the fixture build is this suite's default (DEV without VITE_LIVE), the live
// path is the same build with VITE_LIVE set, as mcpServers.test.tsx does.
// The network answers 404 in the fixture case, as the dev server does, so a
// card that reads around the loader fails exactly as it did in the capture.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createElement, Fragment } from 'react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

const JSON_HEADERS = { 'content-type': 'application/json' }
const WAIT = { timeout: 4000 }

function setPersonaCookie(persona: string | null): void {
  document.cookie =
    persona === null
      ? 'swarm_qa_persona=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
      : `swarm_qa_persona=${persona}; path=/`
}

afterEach(() => {
  cleanup()
  setPersonaCookie(null)
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

describe('the Additional repositories card in the fixture build (admin-001, F1)', () => {
  it('draws its form, and the admin/limits Reads footer counts no failed read', async () => {
    expect(import.meta.env.DEV, 'this case reads the fixture build').toBeTruthy()
    expect(import.meta.env.VITE_LIVE, 'VITE_LIVE turns the fixtures off').toBeFalsy()
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 404, headers: JSON_HEADERS })))
    // The persona swarm_qa.py captures Pool limits as: a platform admin, so
    // the card draws its form rather than its refusal.
    setPersonaCookie('platform-admin')
    vi.resetModules()
    const reads = await import('../fetch')
    const { summariseProbes } = await import('../panes')
    const { AdditionalRepositoryLimitsCard } = await import('../AdminSettings')
    const { Dock } = await import('../Dock')

    reads.beginScreenReads('admin/limits')
    const { container } = render(createElement(Fragment, null, createElement(AdditionalRepositoryLimitsCard), createElement(Dock)))

    // The Reads first, then the form, so a card that reads around the loader
    // fails on the count this lane is about rather than on a missing field.
    // The screen's own reads: the card's read settled on admin/limits, and
    // none failed. `settled` is asserted so a read that never registered
    // cannot pass as "no failure".
    await waitFor(() => {
      const now = reads.screenReadsSnapshot()
      expect(now.settled).toBeGreaterThan(0)
      expect(now.inFlight).toBe(0)
    }, WAIT)
    const own = reads.screenReadsSnapshot()
    expect(own.key).toBe('admin/limits')
    // MUTATION: bare read() in the card -> its 404 counts here as one failed read.
    expect(own.failed).toBe(0)

    // The footer itself: the Dock's "N failed" fact, over the routes read.
    const probes = reads.probeSnapshot()
    expect(probes.map((p) => p.path), 'the card\'s read reached the footer').toContain('/v1/admin/settings')
    // MUTATION: bare read() in the card -> the probe records 404 and the summary counts 1 failed.
    expect(summariseProbes(probes).failed).toBe(0)
    const failedFact = () => Array.from(container.querySelectorAll('.ctl-dock-fact')).find((el) => /\bfailed\b/.test(el.textContent ?? ''))
    // MUTATION: bare read() in the card -> the footer reads '1 failed'.
    await waitFor(() => expect(failedFact()?.textContent?.trim()).toMatch(/^0 failed\b/), WAIT)

    const count = (await screen.findByLabelText('Most additional repositories per step', {}, WAIT)) as HTMLInputElement
    // MUTATION: read `/v1/admin/settings` with a bare read() in AdditionalRepositoryLimitsCard -> the 404 draws 'Not read', no field.
    expect(count.value).toBe('4')
    // MUTATION: drop the budget field from REPO_LIMITS -> no such label.
    expect((screen.getByLabelText('Size budget per step, GiB') as HTMLInputElement).value).toBe('20')
    // MUTATION: draw 'Not read' beside the form -> an alert is drawn.
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('the Additional repositories card on the live path', () => {
  it('issues exactly one GET of /v1/admin/settings', async () => {
    const SETTINGS = {
      merge_by_default: false, max_additional_repositories: 5, additional_repositories_budget_gib: 6,
      updated_at: null, updated_by: null, settings_document: 'present',
    }
    const calls: Array<{ method: string; url: string }> = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      calls.push({ method, url })
      if (method === 'GET' && url === '/v1/admin/settings') return new Response(JSON.stringify(SETTINGS), { status: 200, headers: JSON_HEADERS })
      throw new Error(`unexpected request ${method} ${url}`)
    }))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { USE_FIXTURES } = await import('../api')
    expect(USE_FIXTURES, 'VITE_LIVE takes the live path').toBe(false)
    const { AdditionalRepositoryLimitsCard } = await import('../AdminSettings')
    render(createElement(AdditionalRepositoryLimitsCard))

    const count = (await screen.findByLabelText('Most additional repositories per step', {}, WAIT)) as HTMLInputElement
    // MUTATION: answer the card from the fixture on the live path -> '4', and no GET at all.
    expect(count.value).toBe('5')
    expect((screen.getByLabelText('Size budget per step, GiB') as HTMLInputElement).value).toBe('6')
    // A second read, if one were coming, has had its turn by now.
    await new Promise((r) => setTimeout(r, 50))
    // MUTATION: drop the card's effect dependency list, so it reads again after every render -> a second GET.
    // (Two reads started together are one request: fetch.ts joins an identical GET in flight.)
    expect(calls).toEqual([{ method: 'GET', url: '/v1/admin/settings' }])
  })
})
