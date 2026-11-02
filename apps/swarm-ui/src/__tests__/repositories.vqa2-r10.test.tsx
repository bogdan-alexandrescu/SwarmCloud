// WORK › REPOSITORIES, the visual QA re-run's findings for lane VQA2-R10
// (part of #1427, 2026-10-16).
//
// WHAT EACH CASE HOLDS, by finding:
//   * V216 a module path breaks only after a `/`, in a column as wide as its paths;
//   * V217 every test-map test row has the same three slots, so evidence and Copy are columns;
//   * V218 a hot-spot path carries its whole text as a title, and wraps on a phone;
//   * V219 a long repository title wraps the head instead of clipping its actions;
//   * V220 the change trigger's sentence options are drawn one per line;
//   * V221 a stacked languages row names each of its cells;
//   * V222 Escape on Index now's confirmation puts focus back on Index now;
//   * V223 the header's age is the registration's read, not the index's;
//   * V224 the list's count line is there while reading and after a failure.
//
// Properties, not markup shape: the class or attribute the finding turns on,
// the rendered words, the focused element, and the declaration the cascade picks.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'
import { JSON_HEADERS, coverageOf, liveIndex, repo, serve, sha, visible } from './repofixture'
import { cascade } from './cssgate'
import { SHEETS as ALL } from './sheets'

const WAIT = { timeout: 4000 }
const ID = 'repo_5555555555555555'
const CSS = ALL.map(([, t]) => t).join('\n')
const PHONE = { width: 400 }
const WIDE = { width: 1440 }

const won = (el: Element, prop: string | readonly string[], env: { width: number }, pseudo: string | null = null) =>
  cascade(CSS, el, prop, env, pseudo).winner?.value ?? null

function detail(name = 'swarmcloud', index: Record<string, unknown> = {}) {
  return {
    repository: {
      ...repo({ repo_id: ID, repo: name }, { current_sha: sha('9f8e7d6'), head_sha: sha('9f8e7d6'), coverage: coverageOf(83, 20, 1339, 7), ...index }),
      graph: { depth: 3, min_confidence: 0.2 },
      selection_policy: { policy: 'P3', mode: 'X2', inherited_from_tenant: false },
    },
    index_runs: [],
    used_by: [],
  }
}

type R = { status: number; body: unknown }

function routes(over: { detail?: R; list?: R } = {}) {
  return serve((m, url) => {
    if (m !== 'GET') return null
    const u = new URL(url, 'http://x')
    if (u.pathname === '/v1/repositories') return over.list ?? { status: 200, body: { repositories: [detail().repository] } }
    if (u.pathname === `/v1/repositories/${ID}`) return over.detail ?? { status: 200, body: detail() }
    if (u.pathname === `/v1/repositories/${ID}/index` && u.searchParams.get('format') === 'json') return { status: 200, body: liveIndex(ID) }
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

const loaded = (name = 'swarmcloud') =>
  waitFor(() => expect(document.querySelector('h1')?.textContent).toBe(`example-org/${name}`), WAIT)

describe('V216: a module path breaks at a directory, not inside a name', () => {
  it('puts a break opportunity after every `/`, keeps the words, and sizes the column to its paths', async () => {
    routes()
    await mount(`repo=${ID}`)
    await waitFor(() => expect(document.querySelectorAll('.ur-mrow')).toHaveLength(3), WAIT)
    const row = document.querySelector('.ur-mrow')!
    const code = row.querySelector('code')!
    expect(visible(code)).toBe('apps/common/swarm_common')
    expect(code.getAttribute('title')).toBe('apps/common/swarm_common')
    expect(code.querySelectorAll('wbr')).toHaveLength(2)
    expect(won(code, 'overflow-wrap', WIDE), 'a name breaks mid-word').toBe('break-word')
    expect(won(row, 'grid-template-columns', WIDE)).toBe('fit-content(45%) minmax(0, 1fr) 54px')
  })
})

describe('V217: test-map evidence and Copy are columns', () => {
  it('draws every test row as the same three slots, with the Copy slot kept when there is no command', async () => {
    routes()
    await mount(`repo=${ID}&tab=test-map`)
    await waitFor(() => expect(document.querySelectorAll('.ur-tmap-row')).toHaveLength(2), WAIT)
    const tests = Array.from(document.querySelectorAll<HTMLElement>('.ur-test'))
    expect(tests).toHaveLength(4)
    for (const t of tests) {
      expect(Array.from(t.children).map((c) => c.tagName === 'CODE' ? 'code' : c.className)).toEqual(['code', 'ur-test-ev', 'ur-test-copy'])
    }
    // tests/unit/scheduler/test_admission.py has no command: its chip stays in the evidence slot.
    const noCmd = tests.find((t) => visible(t.querySelector('code')) === 'tests/unit/scheduler/test_admission.py')!
    expect(visible(noCmd.querySelector('.ur-test-ev'))).toBe('co-change')
    expect(noCmd.querySelector('.ur-test-copy')!.children).toHaveLength(0)
    expect(tests[0]!.querySelector('.ur-test-copy button')).not.toBeNull()
    expect(won(tests[0]!, 'display', WIDE)).toBe('grid')
    expect(won(tests[0]!, 'grid-template-columns', WIDE)).toBe('minmax(0, 1fr) 84px 64px')
  })
})

describe('V218: a hot-spot path can be read whole', () => {
  it('carries the path as its title, and on a phone the path wraps on its own line', async () => {
    routes()
    await mount(`repo=${ID}&tab=hot-spots`)
    await waitFor(() => expect(document.querySelectorAll('.ur-hs')).toHaveLength(2), WAIT)
    const p = document.querySelector('.ur-hs .ur-p')!
    expect(p.getAttribute('title')).toBe('apps/swarm-ui/src/App.tsx')
    expect(visible(p)).toBe('apps/swarm-ui/src/App.tsx')
    expect(won(p, 'white-space', PHONE)).toBe('normal')
    expect(won(p, 'grid-column', PHONE)).toBe('1 / -1')
    // On a phone it breaks after a `/`, not inside a name.
    expect(p.querySelectorAll('wbr')).toHaveLength(3)
    expect(won(p.querySelector('wbr')!, 'display', PHONE)).toBe('inline')
    expect(won(p, 'overflow-wrap', PHONE)).toBe('break-word')
    // Wide, it is still the one-line ellipsised column: Chromium breaks at a <wbr> even under nowrap, so none is drawn.
    expect(won(p, 'white-space', WIDE)).toBe('nowrap')
    expect(won(p.querySelector('wbr')!, 'display', WIDE)).toBe('none')
  })
})

describe('V219: a two-line repository title does not squeeze the head', () => {
  it('lets the head wrap, and keeps the refresh and the in-flight mark whole', async () => {
    const name = 'a-repository-whose-name-is-long-enough-to-take-two-lines-of-the-page-head'
    serve((m, url) => {
      if (m !== 'GET') return null
      const u = new URL(url, 'http://x')
      if (u.pathname === `/v1/repositories/${ID}`) return { status: 200, body: detail(name, { in_flight_task_id: 'task_index' }) }
      if (u.pathname === `/v1/repositories/${ID}/index`) return { status: 200, body: liveIndex(ID) }
      return null
    })
    await mount(`repo=${ID}`)
    await loaded(name)
    const head = document.querySelector('.ur-detail > .c-phead')!
    expect(won(head, ['flex-wrap', 'flex-flow'], WIDE)).toBe('wrap')
    const acts = head.querySelector(':scope > .c-acts')!
    expect(won(acts, 'flex', WIDE), 'the actions shrink and clip').toBe('none')
    const refresh = acts.querySelector('.c-refresh')!
    expect(won(refresh, 'flex', WIDE)).toBe('none')
    const mark = Array.from(acts.children).find((c) => visible(c) === 'indexing now')!
    expect(mark).toBeDefined()
    expect(won(mark, 'white-space', WIDE)).toBe('nowrap')
  })
})

describe('V220: the change trigger is one option per line', () => {
  it('stacks the Settings trigger group, and leaves the selection policy a row', async () => {
    routes()
    await mount(`repo=${ID}&tab=settings`)
    await loaded()
    const trig = document.querySelector('.ur-trigger [role="radiogroup"]')!
    expect(trig.classList.contains('is-stack')).toBe(true)
    expect(won(trig, ['flex-direction', 'flex-flow'], WIDE)).toBe('column')
    expect(won(trig, ['flex-direction', 'flex-flow'], PHONE)).toBe('column')
    const off = Array.from(trig.querySelectorAll('button')).find((b) => visible(b) === 'Off')!
    expect(won(off, 'border-right', WIDE), 'a stacked segment draws a side divider').toBe('0')
    for (const g of Array.from(document.querySelectorAll('.ur-policy [role="radiogroup"]'))) expect(g.classList.contains('is-stack')).toBe(false)
  })

  it('stacks any group of three or more whose options are sentences (Register step 2 draws TRIGGER_OPTIONS)', async () => {
    const { UrRadio, stacksOf } = await import('../RepositoriesParts')
    const { TRIGGER_OPTIONS } = await import('../RepositoriesData')
    expect(stacksOf(TRIGGER_OPTIONS)).toBe(true)
    expect(stacksOf([{ label: 'X2 run in this repository\'s GitHub Actions' }, { label: 'X1 run in SwarmCloud' }])).toBe(false)
    expect(stacksOf([{ label: '6 h' }, { label: '12 h' }, { label: 'Off' }])).toBe(false)
    render(<UrRadio label="Change trigger" options={TRIGGER_OPTIONS} value="poll" />)
    expect(document.querySelector('[aria-label="Change trigger"]')!.classList.contains('is-stack')).toBe(true)
  })
})

describe('V221: a stacked languages row names its cells', () => {
  it('labels grammar, language server and last index, and draws the label only on a phone', async () => {
    routes()
    await mount(`repo=${ID}&tab=settings`)
    await loaded()
    await waitFor(() => expect(document.querySelectorAll('.ur-langs .ur-lang')).toHaveLength(2), WAIT)
    const row = document.querySelector('.ur-langs .ur-lang')!
    const labelled = Array.from(row.querySelectorAll('[data-label]'))
    expect(labelled.map((c) => c.getAttribute('data-label'))).toEqual(['grammar', 'language server', 'last index'])
    expect(visible(labelled[1]!)).toBe('pyright')
    for (const c of labelled) {
      expect(won(c, 'content', PHONE, 'before') ?? '').toMatch(/attr\(data-label\)/)
      expect(won(c, 'content', WIDE, 'before'), 'the head row names the column at 1440').toBeNull()
    }
    expect(won(row.querySelector('.ur-lang-h') ?? document.querySelector('.ur-lang-h')!, 'display', PHONE)).toBe('none')
  })
})

describe('V222: Escape on the Index now confirmation returns focus to Index now', () => {
  it('focuses Index now, not <body>, once the confirmation closes', async () => {
    routes()
    await mount(null)
    await waitFor(() => expect(document.querySelector('.ur-card')).not.toBeNull(), WAIT)
    const indexNow = () => Array.from(document.querySelectorAll<HTMLButtonElement>('.ur-card button')).find((b) => visible(b) === 'Index now')!
    fireEvent.click(indexNow())
    const cancel = await waitFor(() => {
      const c = document.querySelector<HTMLButtonElement>('.ur-confirm button[data-safe]')
      expect(c).not.toBeNull()
      return c!
    }, WAIT)
    expect(document.activeElement).toBe(cancel)
    fireEvent.keyDown(cancel, { key: 'Escape' })
    await waitFor(() => expect(document.querySelector('.ur-confirm')).toBeNull(), WAIT)
    expect(document.activeElement).not.toBe(document.body)
    expect(document.activeElement).toBe(indexNow())
  })
})

describe('V223: the header age is the registration\'s read', () => {
  function pendingDetail(answer: R | null) {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const u = new URL(String(input), 'http://x')
      if (u.pathname === `/v1/repositories/${ID}/index`) return new Response(JSON.stringify(liveIndex(ID)), { status: 200, headers: JSON_HEADERS })
      if (u.pathname === `/v1/repositories/${ID}`) {
        if (answer === null) return new Promise<Response>(() => {})
        return new Response(JSON.stringify(answer.body), { status: answer.status, headers: JSON_HEADERS })
      }
      return new Response(JSON.stringify({ detail: 'Not Found' }), { status: 404, headers: JSON_HEADERS })
    }) as unknown as typeof fetch
    return globalThis.fetch as unknown as ReturnType<typeof vi.fn>
  }

  it('says "reading" while the registration is pending, though the index has answered', async () => {
    const f = pendingDetail(null)
    await mount(`repo=${ID}`)
    await waitFor(() => expect(f.mock.calls.some(([u]) => String(u).includes('/index'))).toBe(true), WAIT)
    await new Promise((r) => setTimeout(r, 50))
    const refresh = document.querySelector('.c-refresh')!
    expect(visible(refresh)).not.toMatch(/read .* ago/)
    expect(visible(refresh)).toBe('⟳ reading…')
  })

  it('says no age after the registration failed', async () => {
    pendingDetail({ status: 500, body: { detail: 'boom' } })
    await mount(`repo=${ID}`)
    await waitFor(() => expect(document.querySelector('.c-refresh')?.textContent ?? '').not.toContain('reading'), WAIT)
    await new Promise((r) => setTimeout(r, 50))
    const refresh = document.querySelector('.c-refresh')!
    expect(visible(refresh)).not.toMatch(/read .* ago/)
    expect(visible(refresh)).toBe('⟳ refresh')
  })
})

describe('V224: the list\'s count line holds its place', () => {
  it('is there while the list is read', async () => {
    globalThis.fetch = vi.fn(() => new Promise<Response>(() => {})) as unknown as typeof fetch
    await mount(null)
    const note = document.querySelector('.c-count-note')
    expect(note, 'the count line is absent while reading').not.toBeNull()
    expect(visible(note)).toBe('Reading the registered repositories…')
  })

  it('is there, as a dash, when the list failed', async () => {
    routes({ list: { status: 500, body: { detail: 'boom' } } })
    await mount(null)
    await waitFor(() => expect(document.querySelector('.c-count-note .c-dash, .c-count-note [title]')).not.toBeNull(), WAIT)
    const note = document.querySelector('.c-count-note')!
    expect(visible(note)).toMatch(/registered$/)
    expect(visible(note)).not.toMatch(/\d/)
  })

  it('says the count once read', async () => {
    routes()
    await mount(null)
    await waitFor(() => expect(visible(document.querySelector('.c-count-note'))).toBe('Registered repositories · 1'), WAIT)
  })
})
