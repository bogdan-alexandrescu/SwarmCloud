// START HERE (docs/user-guide/start-here.md): the page a person reads before
// they have a workspace, and the console's links to it.
//
// What this holds:
//   * the page is in guidePages.ts and the first page the index lists;
//   * its three tokens are filled at render time: the console address from
//     the page's own origin, the plugin's home and version from what the
//     deployment serves; a value nobody served is a marked placeholder, never
//     an empty string and never the raw `{{token}}`;
//   * every command block on every page has a Copy button, and it copies the
//     filled command;
//   * a signed-in person with no workspace opens /guide/start-here and every
//     guide page it links, with no error and no redirect to setup;
//   * Help and the setup checklist's request step link it.
//
// BREAK IT: drop `fillGuide` from the code block in Guide.tsx, and "fills the
// tokens" and "Copy copies the filled command" fail.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve } from './repofixture'
import { GUIDE_SOURCES } from '../guidePages'
import { GuideScreen, fillGuide, guidePages, loadGuideServed, openGuide } from '../Guide'
import { HelpScreen } from '../HelpSection'

const HOME = 'example-org/swarmcloud'
const VERSION = '0.5.1'

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

const article = () => document.querySelector<HTMLElement>('.guide-article')!
const codeBlocks = () => Array.from(document.querySelectorAll<HTMLElement>('.guide-code code'))

describe('the page', () => {
  it('is in guidePages.ts and the first page the index lists', () => {
    expect(GUIDE_SOURCES['start-here'], 'docs/user-guide/start-here.md is not synced').toBeDefined()
    const pages = guidePages()
    expect(pages[1]).toEqual({ slug: 'start-here', title: 'Start here' })
    const index = GUIDE_SOURCES.index ?? ''
    expect(/\]\(([a-z0-9-]+)\.md\)/.exec(index)?.[1]).toBe('start-here')
  })

  it('names its three tokens, each in a command', () => {
    const md = GUIDE_SOURCES['start-here'] ?? ''
    for (const t of ['{{console}}', '{{plugin-home}}', '{{plugin-version}}']) expect(md).toContain(t)
  })
})

describe('the tokens', () => {
  it('fills them from the page origin and the served values', () => {
    render(<GuideScreen slug="start-here" served={{ pluginHome: HOME, pluginVersion: VERSION }} />)
    const text = article().textContent ?? ''
    expect(text).not.toContain('{{')
    const blocks = codeBlocks().map((c) => c.textContent)
    expect(blocks).toContain(window.location.origin)
    expect(blocks).toContain(`/plugin marketplace add ${HOME}\n/plugin install sc@swarmcloud`)
    expect(blocks.join('\n')).toContain(`git+https://github.com/${HOME}@sc-v${VERSION}#subdirectory=apps/swarm-mcp' sc login`)
    expect(document.querySelectorAll('.guide-fill-missing')).toHaveLength(0)
  })

  it('draws a value nobody served as a marked placeholder, never an empty string', () => {
    render(<GuideScreen slug="start-here" served={{}} />)
    const text = article().textContent ?? ''
    expect(text).not.toContain('{{')
    expect(text).not.toContain('github.com/@')
    expect(text).not.toContain('@sc-v#')
    const marks = Array.from(document.querySelectorAll<HTMLElement>('.guide-fill-missing'))
    expect(new Set(marks.map((m) => m.textContent))).toEqual(new Set(['<owner/name>', '<version>']))
    for (const m of marks) expect(m.getAttribute('title')).toMatch(/platform team/)
    // The console address always has a value: the page's own origin.
    expect(codeBlocks().map((c) => c.textContent)).toContain(window.location.origin)
  })

  it('fills a string the same way the page draws it', () => {
    const md = 'add {{plugin-home}} at {{console}} v{{plugin-version}}'
    expect(fillGuide(md, { console: 'https://swarm.example.com', pluginHome: HOME, pluginVersion: VERSION })).toBe(
      `add ${HOME} at https://swarm.example.com v${VERSION}`,
    )
    expect(fillGuide(md, { console: 'https://swarm.example.com' })).toBe('add <owner/name> at https://swarm.example.com v<version>')
    expect(fillGuide(md, { console: '', pluginHome: '  ', pluginVersion: '' })).toBe('add <owner/name> at <console-address> v<version>')
  })
})

describe('what the deployment serves', () => {
  const answer = (status: number, body: unknown) => async (_url: string) => new Response(JSON.stringify(body), { status })

  it('reads the plugin version from the public discovery document', async () => {
    const fetcher = vi.fn(answer(200, { schema: 1, version: '2.0.0', plugin: { min_version: VERSION } }))
    expect(await loadGuideServed(fetcher)).toEqual({ pluginVersion: VERSION })
    expect(fetcher.mock.calls[0]?.[0]).toBe('/.well-known/swarmcloud')
  })

  it('serves nothing for a missing, refused or malformed document, and never throws', async () => {
    expect(await loadGuideServed(answer(404, { detail: 'Not Found' }))).toEqual({})
    expect(await loadGuideServed(answer(200, { plugin: { min_version: 'not a version; rm -rf' } }))).toEqual({})
    expect(await loadGuideServed(answer(200, ['x']))).toEqual({})
    expect(await loadGuideServed(async () => new Response('<html>', { status: 200 }))).toEqual({})
    expect(await loadGuideServed(async () => Promise.reject(new Error('offline')))).toEqual({})
  })
})

describe('Copy', () => {
  it('sits on every command block of every page', () => {
    for (const p of guidePages()) {
      cleanup()
      render(<GuideScreen slug={p.slug === 'index' ? '' : p.slug} served={{}} />)
      const blocks = document.querySelectorAll('.guide-code')
      expect(document.querySelectorAll('.guide-copy').length, `${p.slug}: a command block without Copy`).toBe(blocks.length)
    }
  })

  it('copies the filled command', async () => {
    const writeText = vi.fn(async () => undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    render(<GuideScreen slug="start-here" served={{ pluginHome: HOME, pluginVersion: VERSION }} />)
    const block = codeBlocks().find((c) => (c.textContent ?? '').includes('sc login'))!.closest('.guide-codebox') as HTMLElement
    fireEvent.click(within(block).getByRole('button', { name: /^Copy/ }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    const copied = String((writeText.mock.calls[0] as unknown[])[0])
    expect(copied).toContain(`git+https://github.com/${HOME}@sc-v${VERSION}#`)
    expect(copied).not.toContain('{{')
    await within(block).findByText('copied ✓')
  })
})

describe('a signed-in person with no workspace', () => {
  function noWorkspace() {
    return serve((method, url) => {
      if (url.includes('/v1/profile'))
        return {
          status: 200,
          body: {
            identity: { email: 'someone@example.com', domain: 'example.com', platform_role: null, is_admin: false, is_admin_unresolved: false },
            workspaces: [],
            revision: 1,
            unavailable: [],
          },
        }
      if (url.includes('/v1/workspace') && method === 'GET') return { status: 200, body: { state: 'none', setup_url: '/setup#workspace', setup_command: '/sc:setup' } }
      if (url.includes('/v1/tenants/me')) return { status: 404, body: { detail: 'no workspace' } }
      return null
    })
  }

  it('opens /guide/start-here and every guide page it links, with no redirect', async () => {
    noWorkspace()
    window.history.replaceState(null, '', '/guide/start-here')
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { App } = await import('../App')
    render(<App />)
    await screen.findByRole('heading', { level: 1, name: 'Start here' }, { timeout: 5000 })
    const slugs = Array.from(
      new Set(
        Array.from(article().querySelectorAll<HTMLAnchorElement>('a'), (a) => a.getAttribute('href') ?? '')
          .filter((h) => h.startsWith('/guide/'))
          .map((h) => h.split('#')[0] ?? ''),
      ),
    )
    expect(slugs.length, 'Start here links no guide page').toBeGreaterThan(8)
    for (const href of slugs) {
      const link = Array.from(article().querySelectorAll<HTMLAnchorElement>('a')).find((a) => a.getAttribute('href') === href)!
      const title = GUIDE_SOURCES[href.slice('/guide/'.length)]?.match(/^# (.+)$/m)?.[1] ?? ''
      fireEvent.click(link)
      // The page's own h1, read directly: a role query over the whole console is seconds per page.
      await waitFor(() => expect(article().querySelector('h1')?.textContent).toBe(title), { timeout: 5000 })
      expect(window.location.pathname, `${href} redirected`).toBe(href)
      act(() => openGuide('/guide/start-here'))
      await waitFor(() => expect(article().querySelector('h1')?.textContent).toBe('Start here'), { timeout: 5000 })
    }
  }, 30000)
})

describe('the links to it', () => {
  it('Help links it', () => {
    render(<HelpScreen topic="" />)
    const a = screen.getByRole('link', { name: 'Start here' })
    expect(a.getAttribute('href')).toBe('/guide/start-here')
  })
})
