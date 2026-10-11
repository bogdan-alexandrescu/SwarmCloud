// WORK › REPOSITORIES, the visual QA findings of lane VQA-L07 (#1038).
//
// WHAT EACH CASE HOLDS, by finding:
//   * V046 the Settings selection policy is a read-only group drawn at full
//     strength, not the shared disabled fade;
//   * V114 the list's and Register's "not served" agree with a plural subject;
//   * V115 "1 source path", not "1 source paths";
//   * V116 a meta item is inline text, so no flex gap opens before ", read";
//   * V117 a meta line's "·" is drawn in the gap and clipped at a line start;
//   * V118 a row of repository cards is one height, actions on the bottom edge;
//   * V120 at 390 a module's purpose takes its own line under the path;
//   * V121 Languages detected is a full-width card, its head on one line.
//
// Properties, not markup shape: the class or attribute the finding turns on,
// the rendered words, and the declaration the cascade picks.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { repo, serve, sha, visible } from './repofixture'
import { cascade } from './cssgate'
import { SHEETS as ALL } from './sheets'

const WAIT = { timeout: 4000 }
const ID = 'repo_1111111111111111'
const CSS = ALL.map(([, t]) => t).join('\n')
const PHONE = { width: 390 }
const WIDE = { width: 1440 }

const won = (el: Element, prop: string | readonly string[], env: { width: number }, pseudo: string | null = null) =>
  cascade(CSS, el, prop, env, pseudo).winner?.value ?? null

const DETAIL = {
  repository: {
    ...repo({ repo_id: ID, repo: 'example-web' }, { current_sha: sha('9f8e7d6'), head_sha: sha('5c4b3a2'), behind_by: 3 }),
    graph: { depth: 3, min_confidence: 0.2 },
    selection_policy: { policy: 'P3', mode: 'X2', inherited_from_tenant: false },
  },
  index_runs: [],
  used_by: [],
}

function index(unmapped: string[]) {
  return {
    commit_sha: sha('9f8e7d6'),
    bytes: 4096,
    truncated: [],
    modules: [{ path: 'apps/scheduler/scheduler', purpose: 'admission, dispatch and the reconciler that reclaims stale leases', files: 12, lines: 3000 }],
    entry_points: [],
    hot_spots: [],
    co_changes: [],
    test_map: [],
    unmapped,
  }
}

function routes(unmapped: string[] = ['src/legacy/old.ts']) {
  return serve((m, url) => {
    if (m !== 'GET') return null
    if (url === `/v1/repositories/${ID}`) return { status: 200, body: DETAIL }
    if (url === `/v1/repositories/${ID}/index?format=json`) return { status: 200, body: index(unmapped) }
    if (url === '/v1/repositories') return { status: 200, body: { repositories: [DETAIL.repository] } }
    return null
  })
}

async function mount(view: string | null) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RepositoriesScreen } = await import('../Repositories')
  return render(<RepositoriesScreen view={view} go={vi.fn()} />)
}

afterEach(() => vi.unstubAllEnvs())

const loaded = () => waitFor(() => expect(document.querySelector('h1')?.textContent).toBe('example-org/example-web'), WAIT)

describe('V046: the served selection policy reads at full contrast', () => {
  it('draws the policy groups read-only, and no fade wins on their buttons', async () => {
    routes()
    await mount(`repo=${ID}&tab=settings`)
    await loaded()
    const groups = Array.from(document.querySelectorAll('.ur-policy [role="radiogroup"]'))
    expect(groups).toHaveLength(2)
    for (const g of groups) {
      expect(g.classList.contains('is-readonly')).toBe(true)
      expect(g.getAttribute('aria-readonly')).toBe('true')
      const checked = g.querySelector('[aria-checked="true"]')!
      // Still not a control: changing the policy is not served.
      expect((checked as HTMLButtonElement).disabled).toBe(true)
      expect(won(checked, 'opacity', WIDE), 'the served value is faded').toBe('1')
      expect(won(checked, 'cursor', WIDE)).toBe('default')
    }
  })

  it('a single option disabled in a live group keeps the disabled fade', async () => {
    const { UrRadio } = await import('../RepositoriesParts')
    render(<UrRadio label="Trigger" value="poll" options={[{ key: 'poll', label: 'Poll' }, { key: 'webhook', label: 'Webhook', disabled: true }]} />)
    const g = document.querySelector('[role="radiogroup"]')!
    expect(g.classList.contains('is-readonly')).toBe(false)
    expect(won(g.querySelector('[aria-checked="false"]')!, 'opacity', WIDE)).toBe('.45')
  })
})

describe('V114: "not served" agrees with a plural subject on the list and Register', () => {
  it('the list says the registered repositories "are" not served', async () => {
    serve(() => ({ status: 404, body: { detail: 'Not Found' } }))
    await mount(null)
    await waitFor(() => expect(document.querySelector('[data-notserved="GET /v1/repositories"]')).not.toBeNull(), WAIT)
    const words = visible(document.querySelector('[data-notserved]'))
    expect(words).toContain("The tenant's registered repositories are not served")
    expect(words).not.toContain('repositories is not')
  })

  it('Register says the readable repositories "are" not served', async () => {
    serve(() => ({ status: 404, body: { detail: 'Not Found' } }))
    await mount('page=register')
    await waitFor(() => expect(document.querySelector('[data-notserved="GET /v1/repositories/readable"]')).not.toBeNull(), WAIT)
    const words = visible(document.querySelector('[data-notserved]'))
    expect(words).toContain("The repositories the tenant's git token can read are not served")
    expect(words).not.toContain('read is not')
  })
})

describe('V115: the Tests mapped tile counts its unmapped paths in agreement', () => {
  it('one is "1 source path"', async () => {
    routes(['src/legacy/old.ts'])
    await mount(`repo=${ID}`)
    await waitFor(() => expect(document.body.textContent).toContain('with no test edge'), WAIT)
    expect(document.body.textContent).toContain('1 source path with no test edge')
    expect(document.body.textContent).not.toContain('1 source paths')
  })

  it('two are "2 source paths"', async () => {
    routes(['src/legacy/old.ts', 'src/legacy/older.ts'])
    await mount(`repo=${ID}`)
    await waitFor(() => expect(document.body.textContent).toContain('2 source paths with no test edge'), WAIT)
  })
})

describe('V116 and V117: a meta line is a clipped run of inline items with drawn separators', () => {
  it('the head item is inline text, so ", read" follows the sha with no gap', async () => {
    routes()
    await mount(`repo=${ID}`)
    await loaded()
    const meta = document.querySelector('.ur-meta')!
    const head = Array.from(meta.children).find((s) => visible(s).startsWith('head'))!
    expect(visible(head)).toMatch(/^head 5c4b3a2, read /)
    // A flex item's gap is what opened "5c4b3a2 , read": the item lays out inline.
    expect(won(head, 'display', WIDE)).toBe('inline-block')
  })

  for (const cls of ['ur-meta', 'ur-cmeta']) {
    it(`.${cls}: the dot sits in the gap and the run clips it at a line start`, () => {
      const p = document.createElement('p')
      p.className = cls
      p.innerHTML = '<span>a</span><span>b</span>'
      document.body.append(p)
      const [first, second] = [...p.children]
      expect(won(p, ['flex-wrap', 'flex-flow'], WIDE) ?? '').toMatch(/wrap/)
      expect(won(p, ['overflow-x', 'overflow'], WIDE), 'nothing clips a line-start dot').toBe('clip')
      expect(won(p, ['overflow-y', 'overflow'], WIDE), 'the clip cuts vertically too').toBeNull()
      expect(won(second!, 'content', WIDE, 'before') ?? '').toMatch(/·/)
      expect(won(second!, 'position', WIDE, 'before'), 'the dot is in the flow, so it can start a line').toBe('absolute')
      expect(won(second!, 'left', WIDE, 'before') ?? '', 'the dot is not left of the item').toMatch(/-/)
      expect(won(second!, 'position', WIDE)).toBe('relative')
      expect(won(first!, 'content', WIDE, 'before')).toBeNull()
      p.remove()
    })
  }
})

describe('V118: a row of repository cards is one height', () => {
  it('stretches the cards and puts each card\'s actions on its bottom edge', async () => {
    routes()
    await mount(null)
    await waitFor(() => expect(document.querySelector('.ur-cards > .ur-card')).not.toBeNull(), WAIT)
    const grid = document.querySelector('.ur-cards')!
    expect(won(grid, 'align-items', WIDE)).toBe('stretch')
    const acts = grid.querySelector('.ur-card > .ur-cacts')!
    expect(won(acts, ['margin-top', 'margin'], WIDE)).toBe('auto')
  })
})

describe('V120: at 390 a module\'s purpose is not cut beside its path', () => {
  it('puts the purpose on its own full-width, wrapping line; at 1440 it stays a column', async () => {
    routes()
    await mount(`repo=${ID}`)
    await waitFor(() => expect(document.querySelector('.ur-mrow')).not.toBeNull(), WAIT)
    const row = document.querySelector('.ur-mrow')!
    const purpose = row.querySelector(':scope > span')!
    expect(visible(purpose)).toContain('reclaims stale leases')
    expect(won(purpose, 'grid-column', PHONE)).toBe('1 / -1')
    expect(won(purpose, 'white-space', PHONE)).toBe('normal')
    expect(won(row, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr) 54px')
    expect(won(purpose, 'grid-column', WIDE)).toBeNull()
    expect(won(row, 'grid-template-columns', WIDE)).toBe('minmax(0, 180px) minmax(0, 1fr) 54px')
  })
})

describe('V121: the languages table has the full row', () => {
  it('draws Languages detected outside the two half-width columns, its head on one line', async () => {
    serve((m, url) => {
      if (m !== 'GET') return null
      if (url === `/v1/repositories/${ID}`) return { status: 200, body: DETAIL }
      if (url === `/v1/repositories/${ID}/index?format=json`) return { status: 200, body: index([]) }
      if (url === `/v1/repositories/${ID}/languages`) {
        return { status: 200, body: { repo_id: ID, languages: [{ language: 'typescript', files: 120, grammar: 'tree-sitter-typescript', server: 'typescript-language-server', status: 'ok' }], source: 'graph' } }
      }
      return null
    })
    await mount(`repo=${ID}&tab=settings`)
    await loaded()
    await waitFor(() => expect(document.querySelector('.ur-langs .ur-lang')).not.toBeNull(), WAIT)
    const card = document.querySelector('.ur-langs')!
    expect(visible(card.querySelector('h2'))).toBe('Languages detected')
    expect(card.closest('.ur-cols'), 'the languages card is in a half-width column').toBeNull()
    expect(won(card.querySelector('.ur-lang-h')!, 'white-space', WIDE)).toBe('nowrap')
  })
})
