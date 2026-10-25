// CONFIG › REPOSITORIES IN THE NAV AND THE ROUTER (repositories.html §1 put
// it under Work, after Runs; CONFIG-NAV, part of #2157, moved it to Config,
// second, after GitHub access, by BGD's decision of 2026-10-23).
//
// The pages are one tab, `config/repositories`, with the page on its query --
// the way one run is `work/runs?run=<id>` -- and real paths in the bar:
//
//   /config/repositories                        the list
//   /config/repositories/register               Register repository
//   /config/repositories/tokens                 Git tokens
//   /config/repositories/tokens/permissions     Permissions
//   /config/repositories/<repo_id>              one repository, Overview
//   /config/repositories/<repo_id>/<tab>        one of its tabs
//
// The same tails under `/repositories`, and `work/repositories?...` addresses,
// land on these (config.nav.test.tsx holds the landings).
//
// Without the Repositories branch in `fromAddress`, a query on the tab would
// be dropped by the normalise effect and every sub-page would land on the list.

import { describe, expect, it } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import { App, SECTIONS, canonical, fromAddress } from '../App'
import { PANEL_PAGES } from '../Spine'
import { addressToPath, pathToAddress } from '../paths'

const config = () => SECTIONS.find((s) => s.id === 'config')!

const PAIRS: readonly (readonly [string, string])[] = [
  ['config/repositories', '/config/repositories'],
  ['config/repositories?page=register', '/config/repositories/register'],
  // The dashed card's Register carries its repository (REPOS-FOLLOWUPS); a reload keeps it (CONSOLE-SMALL-FIXES).
  ['config/repositories?page=register&repo=example-labs%2Fconsole', '/config/repositories/register?repo=example-labs%2Fconsole'],
  ['config/repositories?page=tokens', '/config/repositories/tokens'],
  ['config/repositories?page=permissions', '/config/repositories/tokens/permissions'],
  ['config/repositories?repo=repo_0a1b2c3d4e5f6071', '/config/repositories/repo_0a1b2c3d4e5f6071'],
  ['config/repositories?repo=repo_0a1b2c3d4e5f6071&tab=settings', '/config/repositories/repo_0a1b2c3d4e5f6071/settings'],
  ['config/repositories?repo=repo_0a1b2c3d4e5f6071&tab=test-map', '/config/repositories/repo_0a1b2c3d4e5f6071/test-map'],
  // Screens 8 and 9; Impact carries the pull request a run's PR card opened it on.
  ['config/repositories?repo=repo_0a1b2c3d4e5f6071&tab=graph', '/config/repositories/repo_0a1b2c3d4e5f6071/graph'],
  ['config/repositories?repo=repo_0a1b2c3d4e5f6071&tab=impact', '/config/repositories/repo_0a1b2c3d4e5f6071/impact'],
  ['config/repositories?repo=repo_0a1b2c3d4e5f6071&tab=impact&pr=57', '/config/repositories/repo_0a1b2c3d4e5f6071/impact?pr=57'],
]

describe('the Config section carries Repositories', () => {
  it('lists Repositories second, after GitHub access', () => {
    const ids = config().tabs.map((t) => t.id)
    expect(ids.indexOf('repositories')).toBe(ids.indexOf('github') + 1)
    expect(config().tabs.find((t) => t.id === 'repositories')!.label).toBe('Repositories')
  })

  it('draws a Repositories row in the Config panel, after GitHub access', () => {
    const keys = PANEL_PAGES.config.map((p) => p.key)
    expect(keys.indexOf('repositories')).toBe(keys.indexOf('github') + 1)
    const row = PANEL_PAGES.config.find((p) => p.key === 'repositories')!
    expect([row.label, row.to, row.icon]).toEqual(['Repositories', 'config/repositories', 'repo'])
  })
})

describe('paths', () => {
  it('spells every page as a path and reads it back to the same address', () => {
    for (const [address, path] of PAIRS) {
      expect(addressToPath(address), address).toBe(path)
      // The bar hands the router its path and its query apart.
      const [pathname, search = ''] = path.split('?')
      expect(pathToAddress(pathname!, search)?.address, path).toBe(address)
    }
    expect(PAIRS).toHaveLength(11)
  })

  it('keeps the picked repository on Register, from either root, and nothing else of the query', () => {
    // MUTATION: write the bare register path and a reload of the dashed card's Register opens step 1.
    expect(addressToPath('config/repositories?page=register&repo=example-labs%2Fconsole')).toBe('/config/repositories/register?repo=example-labs%2Fconsole')
    // MUTATION: drop `repo` on the way in and the shared link loses the repository.
    expect(pathToAddress('/config/repositories/register', '?repo=example-labs%2Fconsole')?.address).toBe('config/repositories?page=register&repo=example-labs%2Fconsole')
    expect(pathToAddress('/repositories/register', '?repo=example-labs/console')?.address).toBe('config/repositories?page=register&repo=example-labs%2Fconsole')
    expect(pathToAddress('/config/repositories/register', '?repo=example-labs%2Fconsole&tab=settings&x=1')?.address).toBe('config/repositories?page=register&repo=example-labs%2Fconsole')
    // An empty `repo` is no repository: the bare page.
    expect(addressToPath('config/repositories?page=register&repo=')).toBe('/config/repositories/register')
    expect(pathToAddress('/config/repositories/register', '?repo=')?.address).toBe('config/repositories?page=register')
  })

  it('reads a trailing slash and an unknown tab as the repository itself', () => {
    expect(pathToAddress('/config/repositories/')?.address).toBe('config/repositories')
    expect(pathToAddress('/config/repositories/repo_0a1b2c3d4e5f6071/bogus')?.address).toBe('config/repositories?repo=repo_0a1b2c3d4e5f6071')
  })
})

describe('the router keeps the page on the tab', () => {
  it('opens each page on the Repositories tab, never as an agent', () => {
    for (const [address] of PAIRS) {
      const r = fromAddress(address)
      expect([r.sectionId, r.tab, r.taskId], address).toEqual(['config', 'repositories', null])
      expect(canonical(r), address).toBe(address)
    }
  })
})

describe('the app opens /config/repositories', () => {
  it('heads the page Repositories and lights its panel row', async () => {
    window.history.pushState(null, '', '/config/repositories')
    render(<App />)
    await waitFor(() => expect(document.querySelector('main h1, .c-phead h1')?.textContent).toBe('Repositories'), { timeout: 4000 })
    const on = document.querySelector('.sk-panel .sk-pk.is-on .sk-pl')
    expect(on?.textContent).toBe('Repositories')
  })
})
