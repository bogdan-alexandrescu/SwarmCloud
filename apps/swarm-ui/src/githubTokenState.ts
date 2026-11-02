/**
 * ONE STATE FOR THE GITHUB CONNECTION'S TOKEN (visual QA pass 2 config2-02).
 * GitHub access read `✓ active` above `Token ⚠ expired 10d ago` while Secrets
 * › GitHub token read `expired`: two Config pages, two states for one token.
 * An expired token on an active connection is `active · token renews on next
 * use` in warn tone (forgeapp.py renews it on its next use), never the ok
 * check; anything not active is its own word in bad tone. Both pages
 * import it from this module, so Secrets does not load Access's page and
 * stylesheets for one function.
 */
export function githubTokenState(
  state: string | null,
  expiresAt: string | null,
  now: number,
): { word: string; tone: 'ok' | 'warn' | 'bad' } {
  if (state !== 'active') return { word: state ?? 'unknown', tone: 'bad' }
  const t = expiresAt === null ? NaN : Date.parse(expiresAt)
  if (Number.isFinite(t) && t <= now) return { word: 'active · token renews on next use', tone: 'warn' }
  return { word: 'active', tone: 'ok' }
}
