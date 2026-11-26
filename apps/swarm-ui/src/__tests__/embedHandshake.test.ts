// A dashboard on the console's parent domain may frame it and needs to know it
// rendered (embedHandshake.ts): an expired proxy session leaves the frame on a
// sign-in that refuses frames, and no console code runs to say so.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { EMBED_READY_MESSAGE, embedderOrigin, parentDomain, tellEmbedderReady, type EmbedWindow } from '../embedHandshake'

const PARENT = 'example.com'

/** A console at console.example.com framed by a page the browser names through `ancestors` or `referrer`. */
function framed({ ancestors, referrer = '' }: { ancestors?: string[]; referrer?: string }) {
  const postMessage = vi.fn()
  const win: EmbedWindow = {
    parent: { postMessage },
    self: {},
    location: { hostname: `console.${PARENT}`, protocol: 'https:', ancestorOrigins: ancestors },
    document: { referrer },
  }
  return { win, postMessage }
}

describe('the console tells the page framing it that it rendered', () => {
  it('posts the ready message once per load, to the parent named by ancestorOrigins', () => {
    const { win, postMessage } = framed({ ancestors: [`https://dash.${PARENT}`], referrer: 'https://example.org/' })
    tellEmbedderReady(win)
    tellEmbedderReady(win)
    expect(postMessage.mock.calls).toEqual([[{ type: 'swarm:ready' }, `https://dash.${PARENT}`]])
    expect(EMBED_READY_MESSAGE).toEqual({ type: 'swarm:ready' })
  })

  it('falls back to the referrer where the browser has no ancestorOrigins', () => {
    const { win, postMessage } = framed({ referrer: `https://a.dash.${PARENT}/apps/console` })
    tellEmbedderReady(win)
    expect(postMessage).toHaveBeenCalledWith(EMBED_READY_MESSAGE, `https://a.dash.${PARENT}`)
  })

  it.each([
    ['another domain', 'https://dash.example.org'],
    ['a host that only ends in the parent domain', `https://evil${PARENT}`],
    ['the parent domain under another one', `https://${PARENT}.example.org`],
    ['another scheme', `http://dash.${PARENT}`],
    ['nothing to go by', ''],
  ])('tells no one when the parent is %s', (_case, parent) => {
    const { win, postMessage } = framed({ referrer: parent })
    tellEmbedderReady(win)
    expect(postMessage).not.toHaveBeenCalled()
  })

  it('tells no one when the console is in its own tab', () => {
    const postMessage = vi.fn()
    const top = { postMessage }
    const win: EmbedWindow = {
      parent: top,
      self: top,
      location: { hostname: `console.${PARENT}`, protocol: 'https:', ancestorOrigins: [`https://dash.${PARENT}`] },
      document: { referrer: '' },
    }
    tellEmbedderReady(win)
    expect(postMessage).not.toHaveBeenCalled()
  })

  it('takes the parent domain as the host without its first label, and a two-label host as itself', () => {
    expect(parentDomain(`console.${PARENT}`)).toBe(PARENT)
    expect(parentDomain(`a.console.${PARENT}`)).toBe(`console.${PARENT}`)
    expect(parentDomain(PARENT)).toBe(PARENT)
    expect(parentDomain('localhost')).toBe('localhost')
    expect(embedderOrigin(framed({ referrer: `https://${PARENT}/` }).win)).toBe(`https://${PARENT}`)
  })

  it('is told from the console root once it has mounted', () => {
    const app = readFileSync(join(__dirname, '..', 'App.tsx'), 'utf8')
    expect(app).toContain('useEffect(() => tellEmbedderReady(window), [])')
  })
})
