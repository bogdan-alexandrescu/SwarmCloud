// WORK › REPOSITORIES IN THE NAV AND THE ROUTER (repositories.html §1: "Work ›
// Repositories, after Runs, in the panel"; the mock-up's shell draws it as the
// last Work page, after Timeline).
//
// The pages are one tab, `work/repositories`, with the page on its query --
// the way one run is `work/runs?run=<id>` -- and real paths in the bar:
//
//   /repositories                        the list
//   /repositories/register               Register repository
//   /repositories/tokens                 Git tokens
//   /repositories/tokens/permissions     Permissions
//   /repositories/<repo_id>              one repository, Overview
//   /repositories/<repo_id>/<tab>        one of its tabs
//
// Without the Repositories branch in `fromAddress`, a query on the tab would
// be dropped by the normalise effect and every sub-page would land on the list.

import { describe, expect, it } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import { App, SECTIONS, canonical, fromAddress } from '../App'
import { PANEL_PAGES } from '../Spine'
import { addressToPath, pathToAddress } from '../paths'

const work = () => SECTIONS.find((s) => s.id === 'work')!

const PAIRS: readonly (readonly [string, string])[] = [
  ['work/repositories', '/repositories'],
  ['work/repositories?page=register', '/repositories/register'],
  ['work/repositories?page=tokens', '/repositories/tokens'],
  ['work/repositories?page=permissions', '/repositories/tokens/permissions'],
  ['work/repositories?repo=repo_0a1b2c3d4e5f6071', '/repositories/repo_0a1b2c3d4e5f6071'],
  ['work/repositories?repo=repo_0a1b2c3d4e5f6071&tab=settings', '/repositories/repo_0a1b2c3d4e5f6071/settings'],
  ['work/repositories?repo=repo_0a1b2c3d4e5f6071&tab=test-map', '/repositories/repo_0a1b2c3d4e5f6071/test-map'],
]

describe('the Work section carries Repositories', () => {
  it('lists Repositories after Runs, as the last Work page before the submit forms', () => {
    const ids = work().tabs.map((t) => t.id)
    expect(ids).toContain('repositories')
    expect(ids.indexOf('repositories')).toBeGreaterThan(ids.indexOf('runs'))
    expect(ids.indexOf('repositories')).toBe(ids.indexOf('timeline') + 1)
    expect(work().tabs.find((t) => t.id === 'repositories')!.label).toBe('Repositories')
  })

  it('draws a Repositories row in the Work panel, after Timeline', () => {
    const keys = PANEL_PAGES.work.map((p) => p.key)
    expect(keys[keys.length - 1]).toBe('repositories')
    const row = PANEL_PAGES.work.find((p) => p.key === 'repositories')!
    expect([row.label, row.to, row.icon]).toEqual(['Repositories', 'work/repositories', 'repo'])
  })
})

describe('paths', () => {
  it('spells every page as a path and reads it back to the same address', () => {
    for (const [address, path] of PAIRS) {
      expect(addressToPath(address), address).toBe(path)
      expect(pathToAddress(path)?.address, path).toBe(address)
    }
    expect(PAIRS).toHaveLength(7)
  })

  it('reads a trailing slash and an unknown tab as the repository itself', () => {
    expect(pathToAddress('/repositories/')?.address).toBe('work/repositories')
    expect(pathToAddress('/repositories/repo_0a1b2c3d4e5f6071/bogus')?.address).toBe('work/repositories?repo=repo_0a1b2c3d4e5f6071')
  })
})

describe('the router keeps the page on the tab', () => {
  it('opens each page on the Repositories tab, never as an agent', () => {
    for (const [address] of PAIRS) {
      const r = fromAddress(address)
      expect([r.sectionId, r.tab, r.taskId], address).toEqual(['work', 'repositories', null])
      expect(canonical(r), address).toBe(address)
    }
  })
})

describe('the app opens /repositories', () => {
  it('heads the page Repositories and lights its panel row', async () => {
    window.history.pushState(null, '', '/repositories')
    render(<App />)
    await waitFor(() => expect(document.querySelector('main h1, .c-phead h1')?.textContent).toBe('Repositories'), { timeout: 4000 })
    const on = document.querySelector('.sk-panel .sk-pk.is-on .sk-pl')
    expect(on?.textContent).toBe('Repositories')
  })
})
