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

/**
 * AN ADMIN PAGE REFUSED BY ROLE, SAID IN PLACE (visual QA pass 1, admin-shell-23; part of #2596). A member who
 * follows a link to `/admin/limits` or `/admin/people` was sent to Overview with no word of why: the link silently
 * became another page. The frame draws this in the page's place instead, for someone who administers nothing, once
 * both of its reads say so (an unread session is not "not an admin"); it names the page, the caller's role and
 * the workspace by its name, and links to Overview. The API's own refusal of every admin route is unchanged; this
 * says what it would answer before anything is asked of it.
 *
 * `role` is the caller's stored role in the workspace in hand, or null when it was not read: then the sentence
 * says only that the page is for platform admins, never a role guessed.
 *
 * WHO THE PAGE IS FOR (visual QA pass 2, admin2-02; part of #2694). People and roles and Workspaces you admin are
 * shown to a workspace admin who is not a platform admin (`WORKSPACE_ADMIN_PAGE_KEYS`), so titling their refusal
 * "for platform admins" was wrong: `audience="workspace"` titles it "for workspace admins" and says the reader
 * administers no group workspace. And the owner of a personal workspace is its admin only in the
 * data's sense: "You are an admin of Bogdan" read as if the reader qualified, so `personal` says it is their own
 * workspace instead of naming a role.
 */
export function AdminRoleRefusal({
  page,
  role,
  workspace,
  audience = 'platform',
  personal = false,
}: {
  page: string
  role: string | null
  workspace: string
  audience?: 'platform' | 'workspace'
  personal?: boolean
}) {
  const overview = addressToPath('overview/now')
  return (
    <div className="ws-refusal" data-refused="admin" data-audience={audience}>
      <Banner
        tone="info"
        role="status"
        title={`${page} is for ${audience === 'workspace' ? 'workspace' : 'platform'} admins`}
        actions={
          <ButtonLink kind="primary" size="sm" href={overview}>
            Open Overview
          </ButtonLink>
        }
      >
        {refusalLine(audience, role, workspace, personal)}
      </Banner>
    </div>
  )
}

/** Why the reader does not qualify, then their role in the workspace in hand: never a role guessed. */
function refusalLine(audience: 'platform' | 'workspace', role: string | null, workspace: string, personal: boolean): string {
  const lead = audience === 'workspace' ? 'This page is for admins of a group workspace; you administer none.' : null
  if (personal) return `${lead ?? 'You are not a platform admin.'} ${workspace} is your own workspace.`
  if (role === null || role === '') {
    return lead === null
      ? `You are not a platform admin, and your role in ${workspace} was not read.`
      : `${lead} Your role in ${workspace} was not read.`
  }
  const yours = `You are ${article(role)} ${role} of ${workspace}.`
  return lead === null ? yours : `${lead} ${yours}`
}

/** "a member", "an admin": the article a role's name takes. */
function article(word: string): string {
  return /^[aeiou]/i.test(word) ? 'an' : 'a'
}
