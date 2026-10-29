import { useEffect, useState } from 'react'
import { loadGitTokens, loadMe, loadOnboarding, loadRepositories, loadTokenPermissions, registerTokenSlot, verifyGitToken } from './api'
import { Banner, Button, Card, Chip, Dash, EmptyState, ToneMark } from './components'
import {
  CAPABILITIES, SCOPE_WORD, createSecretsCommand, daysUntil, expiryWords, kindWords, normToken, renewsItself, repoName, storeCommand,
  type GitToken, type PermissionRow, type RepoRecord, type TokenScope,
} from './RepositoriesData'
import { LIST, PERMISSIONS, GT_PAGE, UrCap, UrCrumb, UrNavButton, UrRadio, UrRefresh, UrRegion, useUrRead, writeFailure, type UrRead } from './RepositoriesParts'
import { CountNote, PageHead } from './Shell'
import { GitHubSetupCard } from './GitHubConnect'
import { timeAgo, type OnboardingDoc } from './types'

/**
 * GIT GT_PAGE, pick B (repositories.html screen 12; PICKS.md): cards by scope
 * -- tenant default, repository, user -- and registration handed to the
 * command line. PERMISSIONS, pick A (screen 13): tokens × repositories, one
 * cell per capability.
 *
 * GUIDED FIRST (GT-GUIDE, owner 2026-10-12): a new person arrives here right
 * after their workspace is approved, so the page opens on "Set up GitHub
 * access" (GitHubConnect.tsx `GitHubSetupCard`), three steps ticked by the
 * onboarding read. The terminal path is folded under "Use a personal access
 * token instead (advanced)", and the team (tenant) token under its own
 * disclosure, drawn for an admin only.
 *
 * A TOKEN VALUE NEVER ENTERS OR LEAVES THIS PAGE (CLAUDE.md, owner rule
 * 2026-09-25; PICKS.md: "No console paste box in phase 1"). There is no field
 * that takes one: registering creates the token entry and prints the exact
 * `scripts/create-secrets.sh --stdin` command, and the value goes from the
 * operator's terminal to Secret Manager. Records are read through
 * `normToken`, which copies metadata by name and nothing else, so a value a
 * response carried by mistake is never drawn. The only copy button copies
 * the secret's NAME, which is not a credential, and says so
 * (git-tokens.md §5.4). `last4` is shown because the owner asked to see it.
 *
 * RESOLUTION ORDER R2 and user tokens as U1 (PICKS.md): a repository token,
 * then the tenant token; a user's token is a tenant secret attributed to a
 * person and used only for attribution.
 */

type ScopeFilter = 'all' | TokenScope

const NO_EXPIRY_CLASSIC = 'No expiry recorded: a classic PAT without an expiration header does not expire'
const NO_EXPIRY = 'No expiry recorded for this token'

/** The terminal path's disclosure, and the team token's (GT-GUIDE). */
const PAT_TOGGLE = 'Use a personal access token instead (advanced)'

/** The scopes as the register card offers them: who the token is for, not the API's word. */
const SCOPE_CHOICE: Readonly<Record<TokenScope, string>> = { user: 'Me', repository: 'One repository', tenant: 'Team' }

function tokenTitle(t: GitToken, repos: readonly RepoRecord[]): string {
  if (t.scope === 'tenant') return `${t.tenant_id ?? 'tenant'} default`
  if (t.scope === 'user') return t.user ?? 'a user'
  const id = t.repo_ids[0]
  const reg = repos.find((r) => r.repo_id === id)
  return t.repositories[0] ?? (reg !== undefined ? repoName(reg) : id ?? 'a repository')
}

function covered(t: GitToken, repos: readonly RepoRecord[]): string {
  const names = t.repositories.length > 0 ? t.repositories : t.repo_ids.map((id) => {
    const reg = repos.find((r) => r.repo_id === id)
    return reg !== undefined ? repoName(reg) : id
  })
  if (t.scope === 'tenant') return 'every repository no narrower token covers'
  if (t.scope === 'user') return names.length === 0 ? `${t.user ?? 'this user'}'s own dispatches` : names.join(', ')
  return names.length === 0 ? 'no repository named' : names.join(', ')
}

/**
 * When the token stops working. A token GitHub's App renews (`app_user`) is
 * hours from its `expires_at` by design, and "Expires today" on it read as an
 * alarm about the normal case (owner, 2026-10-12): it says it renews, and
 * warns only when the record says a renewal failed (`expired`).
 */
function Expires({ t, now }: { t: GitToken; now: number }) {
  if (renewsItself(t)) {
    return t.state === 'expired' ? <b className="ur-bad">Renewal failed: reconnect GitHub</b> : <b>renews automatically</b>
  }
  if (t.expires_at === null) return <>Expires <Dash why={t.kind === 'classic_pat' ? NO_EXPIRY_CLASSIC : NO_EXPIRY} /></>
  const w = expiryWords(t.expires_at, now)
  if (w === null) return <>Expires <Dash why="The expiry could not be read" /></>
  return <>Expires {w.startsWith('expired') ? <b className="ur-bad">{w}</b> : <b>{w}</b>}</>
}

/**
 * WHO IS ASKING: an admin, and which tenant. The API registers a tenant's and
 * a repository's token for an admin only (`routes/gittokens._slot_from`), and
 * a non-admin used to meet that only after the click. Either signal the
 * console already reads will do -- `signed_in`'s `is_admin` on the
 * onboarding read, or the frame's `GET /v1/tenants/me` -- and an unknown is
 * not an admin: the server is still the gate.
 */
function useWho(ob: UrRead<OnboardingDoc>): { admin: boolean; tenant: string | null } {
  const [me, setMe] = useState<{ admin: boolean; tenant: string | null } | null>(null)
  useEffect(() => {
    let live = true
    Promise.resolve(loadMe({ frame: true })).then(
      (r) => {
        if (live && r !== undefined && (r.status === 'ok' || r.status === 'stale')) {
          setMe({ admin: r.data.principal?.is_admin === true, tenant: r.data.tenant?.tenant_id ?? null })
        }
      },
      () => {},
    )
    return () => {
      live = false
    }
  }, [])
  const doc = ob.state.status === 'ok' || ob.state.status === 'stale' ? ob.state.data : null
  const signedIn = doc?.steps.find((s) => s.step === 'signed_in')
  const obAdmin = signedIn?.evidence?.is_admin === true
  return { admin: obAdmin || me?.admin === true, tenant: doc?.tenant_id ?? me?.tenant ?? null }
}

export function GitTokensPage({ go }: { go: (to: string) => void }) {
  const tokens = useUrRead(loadGitTokens, 'tokens')
  const repos = useUrRead(loadRepositories, 'repos')
  const onboarding = useUrRead(loadOnboarding, 'onboarding')
  const who = useWho(onboarding)
  const [scope, setScope] = useState<ScopeFilter>('all')
  const [slot, setSlot] = useState<GitToken | null>(null)
  // G4-22, kept by GT-GUIDE: ONE entry point to each terminal path. The
  // personal one is folded behind its disclosure, the team one (admins only)
  // behind its own, and Rotate opens the one its entry belongs to.
  const [patOpen, setPatOpen] = useState(false)
  const [teamOpen, setTeamOpen] = useState(false)
  useEffect(() => {
    if (patOpen) document.getElementById('ur-reg')?.scrollIntoView?.({ block: 'start' })
  }, [patOpen, slot?.token_id])
  useEffect(() => {
    if (teamOpen) document.getElementById('ur-reg-team')?.scrollIntoView?.({ block: 'start' })
  }, [teamOpen])
  const regs = repos.state.status === 'ok' || repos.state.status === 'stale' ? repos.state.data : []
  const n = tokens.state.status === 'ok' || tokens.state.status === 'stale' ? tokens.state.data.length : tokens.state.status === 'empty' ? 0 : null
  const now = Date.now()
  const tenant = who.tenant ?? 'your team'
  // Rotate only where a Create would be accepted: your own entry, or any for an admin.
  const mayRotate = (t: GitToken) => who.admin || (t.scope === 'user' && t.yours)
  const patScopes: TokenScope[] = who.admin ? ['user', 'repository'] : ['user']
  const patSlot = slot !== null && slot.scope !== 'tenant' ? slot : null
  const teamSlot = slot !== null && slot.scope === 'tenant' ? slot : null

  return (
    <div className="ur-page ur-tokens">
      <UrCrumb trail={[{ label: 'Config' }, { label: 'Repositories', to: LIST }, { label: 'Git tokens' }]} go={go} />
      {/* TITLE LEFT, ACTIONS RIGHT (#138): the scope filter is an action of
          the page, and the count is the note over its first card. */}
      <PageHead title="Git tokens">
        {/* The page's one age, on the control that renews it (#98). */}
        <UrRefresh reads={[tokens, repos, onboarding]} />
        <UrRadio<ScopeFilter>
          label="Scope"
          value={scope}
          onChange={setScope}
          options={[
            { key: 'all', label: 'All' },
            { key: 'tenant', label: 'Tenant' },
            { key: 'repository', label: 'Repository' },
            { key: 'user', label: 'User' },
          ]}
        />
        <span className="ur-acts">
          <UrNavButton to={PERMISSIONS} go={go} size="sm">
            Permissions
          </UrNavButton>
        </span>
      </PageHead>
      {/* SET UP GITHUB ACCESS (GT-GUIDE): the three steps a new person takes,
          first, because connecting through the App needs no terminal. A
          disconnect changes the token entries below, so it re-reads them. */}
      <GitHubSetupCard read={onboarding} go={go} onChange={tokens.reload} />
      <div className="ur-discl">
        <Button kind="ghost" size="sm" className="ur-discl-btn" aria-expanded={patOpen} aria-controls="ur-reg" onClick={() => setPatOpen((o) => !o)}>
          <span aria-hidden="true">{patOpen ? '▾ ' : '▸ '}</span>
          {PAT_TOGGLE}
        </Button>
        {patOpen && (
          <RegisterToken
            id="ur-reg"
            className="ur-reg"
            title="Personal access token: from your terminal"
            scopes={patScopes}
            slot={patSlot}
            onSlot={(t) => {
              setSlot(t)
              tokens.reload()
            }}
            onClear={() => setSlot(null)}
            repos={regs}
          />
        )}
        {who.admin && (
          <>
            <Button kind="ghost" size="sm" className="ur-discl-btn" aria-expanded={teamOpen} aria-controls="ur-reg-team" onClick={() => setTeamOpen((o) => !o)}>
              <span aria-hidden="true">{teamOpen ? '▾ ' : '▸ '}</span>
              {`Workspace token for ${tenant} (admins only)`}
            </Button>
            {teamOpen && (
              <RegisterToken
                id="ur-reg-team"
                className="ur-reg ur-reg-team"
                title={`Workspace token for ${tenant}: from your terminal`}
                scopes={['tenant']}
                slot={teamSlot}
                onSlot={(t) => {
                  setSlot(t)
                  tokens.reload()
                }}
                onClear={() => setSlot(null)}
                repos={regs}
              />
            )}
          </>
        )}
      </div>
      <CountNote>{n === null ? null : `${n} token${n === 1 ? '' : 's'}`}</CountNote>
      <UrRegion
        state={tokens.state}
        route="GET /v1/git-tokens"
        what="The tenant's git tokens"
        onRetry={tokens.reload}
        empty={
          <EmptyState kind="empty" heading="No git tokens registered">
            Connect GitHub in the steps above, the simplest way in. Or use a personal access token from your terminal.
          </EmptyState>
        }
      >
        {(list) => {
          const shown = list.filter((t) => scope === 'all' || t.scope === scope)
          return (
            <div className="ur-toks">
              {shown.map((t) => (
                <Card key={t.token_id} className="ur-tok">
                  <div className="ur-tok-h">
                    <Chip>{SCOPE_WORD[t.scope]}</Chip>
                    <h2>{tokenTitle(t, regs)}</h2>
                    {mayRotate(t) && (
                      <Button
                        size="sm"
                        onClick={() => {
                          setSlot(t)
                          if (t.scope === 'tenant') setTeamOpen(true)
                          else setPatOpen(true)
                        }}
                      >
                        Rotate
                      </Button>
                    )}
                  </div>
                  <p className="ur-cmeta">
                    <span>acts as {t.forge_login === null ? <Dash why="The account was not read: the token has not been verified" /> : <b>{t.forge_login}</b>}</span>
                    <span>{t.forge ?? <Dash why="The forge was not served" />}</span>
                    <span>
                      {kindWords(t.kind) ?? 'kind not read'}
                      {t.last4 !== null ? ` ··· ${t.last4}` : ''}
                    </span>
                  </p>
                  <p className="ur-cmeta">
                    <span>
                      <Expires t={t} now={now} />
                    </span>
                    <span>Last verified {t.verified_at === null ? <Dash why="Never verified" /> : <b>{timeAgo(t.verified_at)}</b>}</span>
                    {t.state !== null && t.state !== 'active' && <ToneMark tone={t.state === 'unverified' ? 'unknown' : 'bad'}>{t.state}</ToneMark>}
                  </p>
                  <p className="ur-cmeta">
                    <span>Repos covered: {covered(t, regs)}</span>
                  </p>
                  {t.last_error !== null && <p className="ur-bad ur-small">The last verification failed: {t.last_error}</p>}
                </Card>
              ))}
              {shown.length === 0 && <p className="ur-none">No {scope} token is registered.</p>}
            </div>
          )
        }}
      </UrRegion>
    </div>
  )
}

/**
 * A token from your terminal. Step 1 creates (or, for Rotate, names) the
 * token entry; step 2 is the command that stores its value from stdin; step 3
 * says what happens next. No step takes a value.
 *
 * THE ANSWER IS `{token, store_command, note}` (`routes/gittokens.py`
 * `register_git_token`). This read `git_token`, then the whole body, found
 * neither, and told every person whose entry WAS created that it was not
 * (owner, 2026-10-12). `token` first; the older names stay as fallbacks.
 */
function RegisterToken({
  id,
  className,
  title,
  scopes,
  slot,
  onSlot,
  onClear,
  repos,
}: {
  id: string
  className: string
  title: string
  /** What this card may create: never a scope the API refuses the caller. */
  scopes: TokenScope[]
  slot: GitToken | null
  onSlot: (t: GitToken) => void
  /** Forget the rotated or created entry: the person chose another scope. */
  onClear: () => void
  repos: readonly RepoRecord[]
}) {
  const [scope, setScope] = useState<TokenScope>(scopes[0]!)
  const [repoId, setRepoId] = useState<string>('')
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  // ONE source of truth for the scope: what the radio shows is what the
  // select follows and what Create posts. A rotated entry shows its own scope
  // until the person picks another, which forgets the entry.
  const shownScope = slot !== null && scopes.includes(slot.scope) ? slot.scope : scopes.includes(scope) ? scope : scopes[0]!
  const pickRepo = repoId !== '' ? repoId : repos[0]?.repo_id ?? ''

  async function create() {
    setBusy(true)
    setRefused(null)
    setCopied(false)
    const res = await registerTokenSlot(shownScope === 'repository' ? { scope: shownScope, repo_id: pickRepo } : { scope: shownScope })
    setBusy(false)
    if (res.status === 'error') {
      setRefused(writeFailure(res.error))
      return
    }
    const data = res.status === 'ok' && typeof res.data === 'object' && res.data !== null ? (res.data as Record<string, unknown>) : null
    const t = data === null ? null : normToken(data.token) ?? normToken(data.git_token) ?? normToken(data)
    if (t === null) {
      setRefused('The token entry was created, but the answer did not describe it; reload the page to see it.')
      return
    }
    onSlot({ ...t, store_command: t.store_command ?? storeCommand(data?.store_command) })
  }

  async function copyName(name: string) {
    await navigator.clipboard?.writeText(name)
    setCopied(true)
  }

  const command =
    slot === null
      ? null
      : slot.store_command ?? (slot.tenant_id !== null && slot.provider_suffix !== null ? createSecretsCommand(slot.tenant_id, slot.provider_suffix) : null)

  return (
    <Card id={id} className={className} title={title} action={<Chip>value never enters the console</Chip>}>
      {scopes.includes('user') && (
        <p className="ur-hint">
          For when Connect GitHub above is not an option. A personal access token does not renew itself: you store a new
          one before it expires.
        </p>
      )}
      <p className="ur-sub">
        1. Create the token entry. SwarmCloud names the secret that will hold the token; the name is not a credential.
      </p>
      <div className="ur-slot">
        {scopes.length > 1 && (
          <UrRadio<TokenScope>
            label="For"
            value={shownScope}
            onChange={(s) => {
              setScope(s)
              setCopied(false)
              if (slot !== null && slot.scope !== s) onClear()
            }}
            options={scopes.map((k) => ({ key: k, label: SCOPE_CHOICE[k] }))}
          />
        )}
        {shownScope === 'repository' && (
          <select className="ur-select" aria-label="Repository" value={pickRepo} onChange={(e) => setRepoId(e.target.value)} disabled={repos.length === 0}>
            {repos.length === 0 && <option value="">No registered repository</option>}
            {repos.map((r) => (
              <option key={r.repo_id} value={r.repo_id}>
                {repoName(r)}
              </option>
            ))}
          </select>
        )}
        <Button size="sm" busy={busy} onClick={() => void create()} disabled={shownScope === 'repository' && pickRepo === ''}>
          Create the token entry
        </Button>
      </div>
      {refused !== null && (
        <Banner tone="bad" title="The token entry was not created">
          {refused}
        </Banner>
      )}
      {slot === null ? (
        <p className="ur-hint">Create the token entry, or press Rotate on a token above, to see its secret's name and the command that stores its value.</p>
      ) : (
        <>
          <div className="ur-secret">
            {slot.secret_name === null ? <Dash why="The answer did not name its secret" /> : <code>{slot.secret_name}</code>}
            {slot.secret_name !== null && (
              <Button size="sm" onClick={() => void copyName(slot.secret_name as string)}>
                Copy secret name
              </Button>
            )}
            {copied && (
              <span className="ur-mu" role="status">
                Copied the name.
              </span>
            )}
          </div>
          <p className="ur-sub">2. Store the token from your terminal; the command reads it from stdin:</p>
          {command === null ? (
            <p className="ur-hint">
              <Dash why="The answer did not name its tenant or provider suffix" /> The command needs the entry's tenant and
              provider suffix, which the answer did not carry.
            </p>
          ) : (
            <pre className="ur-term">
              <span aria-hidden>$</span> {command}
            </pre>
          )}
          <p className="ur-sub">
            3. SwarmCloud sees the new version, keeps the last 4 characters, checks the token's permissions and shows
            them here. The token itself is never shown here: it goes from your terminal to Secret Manager.
          </p>
        </>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Permissions, pick A
// ---------------------------------------------------------------------------

type RowFilter = 'all' | 'resolving'

function groups(rows: readonly PermissionRow[]): { repo_id: string; name: string; rows: PermissionRow[] }[] {
  const out: { repo_id: string; name: string; rows: PermissionRow[] }[] = []
  for (const r of rows) {
    let g = out.find((x) => x.repo_id === r.repo_id)
    if (g === undefined) {
      g = { repo_id: r.repo_id, name: r.repository ?? r.repo_id, rows: [] }
      out.push(g)
    }
    g.rows.push(r)
  }
  // The token a task would use leads its repository; the probe order is per token, not per repository.
  for (const g of out) g.rows.sort((a, b) => Number(b.resolves) - Number(a.resolves))
  return out
}

function tokenLabel(r: PermissionRow): string {
  const who = r.token.forge_login ?? r.token.token_id
  return `${who} · ${r.token.scope}${r.token.state === 'expired' ? ' (expired)' : ''}`
}

function ExpiresIn({ r, now }: { r: PermissionRow; now: number }) {
  if (r.expires_at === null) {
    if (r.token.kind === 'app_installation') return <>1 h, per use</>
    return <Dash why={r.token.kind === 'classic_pat' ? NO_EXPIRY_CLASSIC : NO_EXPIRY} />
  }
  const d = daysUntil(r.expires_at, now)
  if (d === null) return <Dash why="The expiry could not be read" />
  if (d < 0) return <ToneMark tone="bad">expired</ToneMark>
  if (d < 14) return <ToneMark tone="warn" title="Expires within two weeks">{`${d} d`}</ToneMark>
  return <>{`${d} d`}</>
}

export function PermissionsPage({ go }: { go: (to: string) => void }) {
  const perms = useUrRead(loadTokenPermissions, 'perms')
  const [filter, setFilter] = useState<RowFilter>('all')
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<string | null>(null)
  const data = perms.state.status === 'ok' || perms.state.status === 'stale' ? perms.state.data : null
  const tokenIds = data === null ? [] : Array.from(new Set(data.rows.map((r) => r.token.token_id)))
  const repoIds = data === null ? [] : Array.from(new Set(data.rows.map((r) => r.repo_id)))
  const now = Date.now()

  async function verifyAll() {
    setBusy(true)
    setRefused(null)
    const results = await Promise.all(tokenIds.map((id) => verifyGitToken(id).then((r) => [id, r] as const)))
    setBusy(false)
    const failed = results.filter(([, r]) => r.status === 'error')
    if (failed.length > 0) {
      const [, first] = failed[0]!
      setRefused(
        `${failed.length} of ${results.length} could not be re-probed: ${first.status === 'error' ? writeFailure(first.error) : ''}`,
      )
    }
    perms.reload()
  }

  return (
    <div className="ur-page ur-perms">
      <UrCrumb trail={[{ label: 'Config' }, { label: 'Repositories', to: LIST }, { label: 'Git tokens', to: GT_PAGE }, { label: 'Permissions' }]} go={go} />
      <PageHead title="Permissions">
        <UrRefresh reads={[perms]} />
        {data?.order != null && <Chip>{`order ${data.order}`}</Chip>}
        <span className="ur-acts">
          <Button size="sm" busy={busy} disabled={tokenIds.length === 0} onClick={() => void verifyAll()}>
            Verify all
          </Button>
        </span>
      </PageHead>
      <CountNote>
        {data === null ? null : `${tokenIds.length} token${tokenIds.length === 1 ? '' : 's'} × ${repoIds.length} repositor${repoIds.length === 1 ? 'y' : 'ies'}`}
      </CountNote>
      <UrRadio<RowFilter>
        label="Rows"
        value={filter}
        onChange={setFilter}
        options={[
          { key: 'all', label: 'All tokens' },
          { key: 'resolving', label: 'Resolving only' },
        ]}
      />
      {refused !== null && (
        <Banner tone="bad" title="Verify all did not finish">
          {refused}
        </Banner>
      )}
      <UrRegion
        state={perms.state}
        route="GET /v1/git-tokens"
        what="The permission matrix"
        onRetry={perms.reload}
        empty={
          <EmptyState kind="empty" heading="Nothing to check yet">
            The matrix has a row for each token and each repository it covers. Register a repository and a token first.
          </EmptyState>
        }
      >
        {(p) => {
          const gs = groups(p.rows.filter((r) => filter === 'all' || r.resolves))
          return (
            <>
              <div className="c-card ur-mx-wide">
                <table className="ur-mx">
                  <thead>
                    <tr>
                      <th className="is-l">Token</th>
                      {CAPABILITIES.map((c) => (
                        <th key={c.key} className="is-cap">
                          {c.label}
                        </th>
                      ))}
                      <th className="is-l">Expires in</th>
                      {/* G4-17: "Last verified" on one line was the head clipped
                          to "Last verifi" at 1440; the title keeps the full words. */}
                      <th className="is-l" title="Last verified">
                        Verified
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {gs.map((g) => [
                      <tr className="ur-grp" key={`g:${g.repo_id}`}>
                        <td colSpan={CAPABILITIES.length + 3}>{g.name}</td>
                      </tr>,
                      ...g.rows.map((r) => (
                        <tr key={`${g.repo_id}:${r.token.token_id}`}>
                          <td className="is-l">
                            {tokenLabel(r)}
                            {r.resolves && (
                              <>
                                {' '}
                                <Chip>resolves</Chip>
                              </>
                            )}
                          </td>
                          {CAPABILITIES.map((c) => (
                            <td key={c.key}>
                              <UrCap label={c.label} cell={r.capabilities[c.key]} word={r.capabilities[c.key].state} />
                            </td>
                          ))}
                          <td className="is-l">
                            <ExpiresIn r={r} now={now} />
                          </td>
                          <td className="is-l ur-mu">
                            {r.verified_at === null ? <Dash why="Never verified" /> : timeAgo(r.verified_at)}
                            {r.last_error !== null && (
                              <>
                                {' '}
                                <ToneMark tone="warn" title={`The last attempt failed: ${r.last_error}`} />
                              </>
                            )}
                          </td>
                        </tr>
                      )),
                    ])}
                  </tbody>
                </table>
              </div>
              <div className="ur-mx-cards">
                {gs.map((g) => {
                  const use = g.rows.find((r) => r.resolves) ?? g.rows[0]!
                  const lacks = CAPABILITIES.filter((c) => use.capabilities[c.key].state === 'missing').map((c) => c.label.toLowerCase())
                  const unknown = CAPABILITIES.filter((c) => use.capabilities[c.key].state === 'unknown').length
                  return (
                    <Card key={g.repo_id} className="ur-mxcard" title={g.name}>
                      {!use.resolves && <p className="ur-hint">No token resolves here; showing {tokenLabel(use)}.</p>}
                      <div className="ur-rows">
                        {CAPABILITIES.map((c) => (
                          <div className="ur-treerow" key={c.key}>
                            <span>
                              <b>{c.label}</b> <small>with {use.token.forge_login ?? use.token.token_id}</small>
                            </span>
                            <UrCap label={c.label} cell={use.capabilities[c.key]} word={use.capabilities[c.key].state} />
                          </div>
                        ))}
                      </div>
                      <p className="ur-hint">
                        {lacks.length > 0 ? `Lacks: ${lacks.join(', ')}.` : 'Lacks nothing measured.'}
                        {unknown > 0 ? ` ${unknown} unknown.` : ''}
                      </p>
                    </Card>
                  )
                })}
              </div>
              <p className="ur-hint ur-legend">
                <UrCap label="ok" cell={{ state: 'ok', reason: 'measured' }} word="ok" /> measured ·{' '}
                <UrCap label="missing" cell={{ state: 'missing', reason: 'refused, with the reason' }} word="missing" /> refused, with the
                reason · <UrCap label="unknown" cell={{ state: 'unknown', reason: 'not readable without trying' }} word="unknown" /> not
                readable without trying (fine-grained grants), never shown as ok. "resolves" marks the token an agent in that repository would
                use.
              </p>
            </>
          )
        }}
      </UrRegion>
    </div>
  )
}
