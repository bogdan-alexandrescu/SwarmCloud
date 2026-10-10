import { Banner, ButtonLink } from './components'
import type { ApiError } from './fetch'
import { addressToPath } from './paths'

/**
 * THE SUBMISSION GATE'S TWO REFUSALS, AS ONE BANNER (#847, lane W8;
 * docs/workspaces.md §5.2, §6.1).
 *
 * swarm-api refuses a person's task, workflow or issue run into their OWN
 * tenant with `403 WORKSPACE_NOT_READY` until their workspace is `ready`, and
 * then with `403 NO_CLAUDE_ACCOUNT` until it has a Claude account to run on.
 * Every Submit screen draws either one here: why, in the API's own words
 * (its `message` varies with the record's state, so restating it here would
 * be a second, staler copy), and a "Finish setup" link to the step that
 * fixes it, the `setup_url` the API sent.
 *
 * NO FORM IS DISABLED AHEAD OF TIME. The server is the authority: the gate is
 * behind `WORKSPACE_GATE`, shipped off, and a group tenant's submissions are
 * never gated (WD7), so a client guessing would refuse work the API admits.
 * While the gate is off the API never sends these codes and this draws
 * nothing.
 *
 * "NOTHING WAS CREATED" IS SAID BECAUSE IT IS TRUE HERE AND NOT IN GENERAL. A
 * failed write cannot usually prove nothing was made, and the screens' own
 * failure panels say so. The gate runs first in `submit_tasks` and
 * `submit_workflow`, before validation, signing or any write (§5.1, §5.3), so
 * these two codes are the one failure that can say it.
 */

export const WORKSPACE_NOT_READY = 'WORKSPACE_NOT_READY'
export const NO_CLAUDE_ACCOUNT = 'NO_CLAUDE_ACCOUNT'

/** The anchors `workspaces.setup_url` names, on Work › Setup. */
const ANCHOR: Readonly<Record<string, string>> = {
  [WORKSPACE_NOT_READY]: 'workspace',
  [NO_CLAUDE_ACCOUNT]: 'claude-account',
}

const TITLE: Readonly<Record<string, string>> = {
  [WORKSPACE_NOT_READY]: 'Not submitted: your workspace is not ready yet',
  [NO_CLAUDE_ACCOUNT]: 'Not submitted: your workspace has no Claude account yet',
}

export interface WorkspaceRefusal {
  code: typeof WORKSPACE_NOT_READY | typeof NO_CLAUDE_ACCOUNT
  message: string
  setupUrl: string
}

/**
 * The refusal, when `error` is one of the gate's two; null for anything else,
 * which the screen's own failure panel keeps drawing.
 *
 * The link is the API's `setup_url` when it is a path or an https address.
 * Anything else -- absent, or a scheme a link should not follow -- falls back
 * to this console's own Setup page at the same anchor, which is where the
 * API's URL points anyway.
 */
export function workspaceRefusal(error: ApiError | null | undefined): WorkspaceRefusal | null {
  if (error == null || error.httpStatus !== 403) return null
  const code = error.code
  if (code !== WORKSPACE_NOT_READY && code !== NO_CLAUDE_ACCOUNT) return null
  const detail = typeof error.detail === 'object' && error.detail !== null ? (error.detail as Record<string, unknown>) : {}
  const url = typeof detail.setup_url === 'string' ? detail.setup_url : ''
  const usable = url.startsWith('https://') || (url.startsWith('/') && !url.startsWith('//'))
  return { code, message: error.message, setupUrl: usable ? url : `${addressToPath('work/setup')}#${ANCHOR[code]}` }
}

/** The banner, or nothing when `error` is not the gate's. */
export function WorkspaceRefusalBanner({ error }: { error: ApiError | null | undefined }) {
  const refusal = workspaceRefusal(error)
  if (refusal === null) return null
  return (
    <div className="ws-refusal" data-code={refusal.code}>
      <Banner
        tone="warn"
        role="alert"
        title={TITLE[refusal.code]}
        actions={
          <ButtonLink kind="primary" size="sm" href={refusal.setupUrl}>
            Finish setup
          </ButtonLink>
        }
      >
        {refusal.message} Nothing was created.
      </Banner>
      <p className="checked-at">HTTP 403 · {refusal.code}</p>
    </div>
  )
}
