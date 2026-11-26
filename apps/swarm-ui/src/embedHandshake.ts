// TELLING A PAGE THAT FRAMES THE CONSOLE THAT IT HAS RENDERED.
//
// A dashboard can show the console in a frame. While the identity-aware
// proxy's session holds, the console renders there. Once it runs out, the
// proxy sends the frame to its sign-in, which refuses to be framed, and none of
// the console's code runs. The dashboard cannot look into a cross-origin frame,
// so the console says when it has rendered, once per load, and the dashboard
// reads silence within its own window as "open the console in a new tab".
//
// WHO HEARS IT. Only a parent page under the console's own parent domain: the
// console at console.example.com tells a parent at dash.example.com, and no
// one else. No host is named here, so a deployment sets nothing for it. The
// parent's origin comes from `location.ancestorOrigins` where the browser has
// it, else from the referrer the frame was opened with. The browser delivers
// the message only when the parent really is that origin, so a forged referrer
// gains nothing. The console listens for nothing back.

/** What the console posts to the page framing it once it has rendered. */
export const EMBED_READY_MESSAGE = { type: 'swarm:ready' } as const

/** The parts of `window` this module reads, so a test can hand it one. */
export interface EmbedWindow {
  parent: { postMessage(message: unknown, targetOrigin: string): void }
  self: unknown
  location: {
    hostname: string
    protocol: string
    ancestorOrigins?: { readonly length: number; readonly [index: number]: string }
  }
  document: { referrer: string }
}

/** The domain a host sits under: the host without its first label, or the host itself when it has two labels or fewer. */
export function parentDomain(host: string): string {
  const labels = host.split('.')
  return labels.length <= 2 ? host : labels.slice(1).join('.')
}

/**
 * The origin of the page framing the console when it is on the console's
 * scheme and under its parent domain; null for any other page, or when the
 * browser gives no origin to go by.
 */
export function embedderOrigin(win: EmbedWindow): string | null {
  const claimed = win.location.ancestorOrigins?.[0] ?? win.document.referrer
  let url: URL
  try {
    url = new URL(claimed)
  } catch {
    return null
  }
  if (url.protocol !== win.location.protocol) return null
  const domain = parentDomain(win.location.hostname)
  return url.hostname === domain || url.hostname.endsWith(`.${domain}`) ? url.origin : null
}

const told = new WeakSet<EmbedWindow>()

/** Posts EMBED_READY_MESSAGE to the framing page: once per load, only inside a frame, only to an embedder under the parent domain. */
export function tellEmbedderReady(win: EmbedWindow): void {
  if (win.parent === win.self || told.has(win)) return
  told.add(win)
  const origin = embedderOrigin(win)
  if (origin !== null) win.parent.postMessage(EMBED_READY_MESSAGE, origin)
}
