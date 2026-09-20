import { useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  loadAccountsBoard,
  refreshAccount,
  registerAccount,
  removeAccount,
  setAccountLending,
  setAccountState,
  type AccountsBoard,
} from './api'
import { errorHeading, type ApiError, type Result } from './fetch'
import { FailedPanel, Screen, timeAgo } from './Shell'
import {
  BAR_CELLS,
  FIVE_HOUR,
  SEVEN_DAY,
  accountTone,
  barFilled,
  bindingWindow,
  clearsIn,
  isProjected,
  needsAHuman,
  readingOf,
  type Account,
  type AccountReading,
  type AccountStateName,
  type AccountWindow,
  type RefreshResult,
} from './types'

/**
 * THE ONE PROVIDER, AND THE ONE CREDENTIAL KIND -- stated, never picked.
 *
 * This pool holds Claude subscription credentials: the keychain pair Claude
 * Code keeps after `/login`, which quota-broker can exchange for a successor
 * indefinitely. There is no API-key path here and no credential-kind selector,
 * because there is nothing to choose between -- and a hardcoded value with no
 * words around it is worse than a picker, since the operator cannot even tell
 * that a decision was made for them. So the form SAYS what it sends.
 *
 * It is a named constant rather than a literal in the request body so that the
 * value printed on the form and the value the request carries cannot drift
 * apart.
 */
const SUBSCRIPTION_PROVIDER = 'anthropic'

/**
 * The state that must OUTLIVE a reload.
 *
 * Every mutation here reloads the table, and Screen reloads by being remounted
 * -- so anything held inside it is destroyed at the exact moment its answer
 * matters. A refresh that reported "the refresh token is gone" and then
 * vanished because the table refreshed is worse than no button at all: the
 * operator is left with a spinner that ended and no verdict, which is the
 * question the button existed to answer.
 *
 * So the outcomes a person has to READ live above the remount boundary. The
 * genuinely transient things -- a half-typed reason, a dirty lending field, a
 * pasted credential -- deliberately do NOT, because they should not survive the
 * write that consumed them.
 *
 * Holding them here is only half of it. `Screen` renders `children` -- and so
 * everything below -- ONLY when it has data, so a reload that FAILS would take
 * the verdict off screen anyway, along with the table. `lastBoard` below is the
 * other half.
 */
interface Persisted {
  /** Explicitly opened or closed rows. Absent means "whatever the default is". */
  open: Record<string, boolean>
  refresh: Record<string, RefreshState>
  reauth: Record<string, ReauthState>
  register: RegisterState
}

type Patch = (fn: (p: Persisted) => Persisted) => void

const EMPTY_UI: Persisted = { open: {}, refresh: {}, reauth: {}, register: { kind: 'idle' } }

/**
 * Settings -> Accounts. The Claude subscriptions this platform runs agents on.
 *
 * WHY THE COLUMNS ARE `cs status`'s, EXACTLY. An operator already reads
 * ACCOUNT / 5H / 7D / CLEARS / STATE on their laptop. Inventing a second
 * vocabulary for the same five facts about the same five accounts costs them a
 * translation every time they look, and the translation is where the mistakes
 * live. So the scan line here is that line: same order, same headings, same
 * five-cell bar, same `~` for a figure that is projected rather than measured.
 * Everything this screen adds -- who it is lent to, how many agents hold it,
 * and every control -- lives in a row you open, so the table you scan is never
 * anything other than the table you already know.
 *
 * THE RULE THE WHOLE SCREEN TURNS ON. `stale` and "utilisation is zero" are
 * different claims, and so are "no reading has ever arrived" and "the reading
 * says 0%". A five-cell bar with no cells filled is the same picture for both,
 * which is why an unmeasured window draws NO bar at all and prints an em dash.
 * `account_to_api` computes `stale` server-side for the same reason: a
 * reading's age is what decides whether to believe it, and the server owns the
 * clock.
 *
 * WHAT THIS SCREEN MAY NOT DO. quota-broker is the platform's single writer for
 * subscription credentials, because refreshing an OAuth credential REVOKES the
 * token it replaces and two writers racing on one account brick it. Every
 * button here is a call to a broker route through swarm-api's proxy. Nothing
 * in this file reads a secret, exchanges a token, retries a refresh on its own
 * initiative, or renders key material -- not even its length.
 */
export function AccountsScreen() {
  // Bumping this remounts Screen, which re-runs `load`. Every mutation here
  // changes what the table says, so every mutation reloads rather than patching
  // a local copy -- a table edited client-side after a write is a table that
  // can disagree with the platform and never say so.
  const [nonce, setNonce] = useState(0)
  const [ui, setUi] = useState<Persisted>(EMPTY_UI)
  const patch: Patch = (fn) => setUi(fn)

  /**
   * THE LAST READ THAT PRODUCED ROWS, kept on OUR side of the remount.
   *
   * `Screen` owns the stale rule -- data in hand plus a failed refresh is the
   * old rows, dimmed, never a blank screen -- and implements it with a ref that
   * a remount destroys. Reloading by remount therefore disabled the one rule
   * that keeps a failed reload from wiping the screen: the table vanished, and
   * with it the refresh verdict, the register receipt and the re-authenticate
   * report, all of which are rendered by `children` and all of which are the
   * answer to a write somebody just made.
   *
   * This ref lives in `AccountsScreen`, which is NOT remounted, so it survives
   * every reload the screen triggers. `load` below hands the previous read back
   * as `stale` when a reload fails, which is the same shape `Screen` would have
   * produced from its own ref had it not just been thrown away. It is the
   * memory that was missing, not a second copy of the rule.
   *
   * The whole body is then DIMMED, verdict included -- `Screen` dims everything
   * it renders from a stale read, and opacity applies to the group, so nothing
   * inside can opt out. That is the right trade: the stale banner above says
   * the figures were read earlier, and a dimmed verdict that is still on screen
   * is what the button existed to produce. A blank one is not.
   */
  const lastBoard = useRef<{ data: AccountsBoard; fetchedAt: number } | null>(null)

  const load = async (): Promise<Result<AccountsBoard>> => {
    const res = await loadAccountsBoard()
    if (res.status === 'ok') {
      lastBoard.current = { data: res.data, fetchedAt: res.fetchedAt }
      return res
    }
    // Only a FAILED read degrades to stale. An `empty` read is a real zero and
    // must replace the rows -- showing data the platform says is gone is the
    // mirror of the bug this app exists to avoid -- and `loadAccountsBoard`
    // never reports empty anyway, so this is belt and braces rather than a
    // branch the screen relies on.
    if (res.status === 'error' && lastBoard.current !== null) {
      return {
        status: 'stale',
        data: lastBoard.current.data,
        fetchedAt: lastBoard.current.fetchedAt,
        error: res.error,
      }
    }
    return res
  }

  return (
    <Screen
      key={nonce}
      title="Accounts"
      load={load}
      summary={(b) => <SummaryLine board={b} />}
      /*
       * NO `empty` PROP, DELIBERATELY. Screen renders `empty` INSTEAD of its
       * children, so an empty pool would hide the register form -- the single
       * control that fixes an empty pool. `loadAccountsBoard` therefore never
       * reports empty, and zero accounts is handled below as a state panel
       * sitting above a form that is still on screen.
       */
    >
      {(b) => (
        <Body
          board={b}
          ui={ui}
          patch={patch}
          reload={() => setNonce((n) => n + 1)}
        />
      )}
    </Screen>
  )
}

/**
 * OWNERSHIP, NOT VISIBILITY, and the one place the rule is written.
 *
 * `GET /v1/accounts` returns what a tenant may RUN on -- owned AND lent -- so
 * "it is on my screen" is not "it is mine to change". Every mutating route in
 * routes/accounts.py resolves the account and requires ownership, which is why
 * a lent row shows no controls. Anything that TELLS a reader to use those
 * controls has to apply the same test, or it points at a row that deliberately
 * cannot answer.
 *
 * A null scope is a platform-wide listing this UI never asks for; nothing is
 * claimed either way in that case, and the account counts as the caller's,
 * because being silent about an account that IS theirs is the worse mistake.
 */
function isOwned(a: Account, scope: string | null): boolean {
  return scope === null || a.owner_tenant === scope
}

function SummaryLine({ board }: { board: AccountsBoard }) {
  const n = board.page.accounts.length
  const scope = board.page.tenant_id
  const broken = board.page.accounts.filter(needsAHuman)
  // Split, because they are two different jobs for two different people: one is
  // "open the row and paste", the other is "this is not yours to fix".
  const mine = broken.filter((a) => isOwned(a, scope)).length
  const lent = broken.length - mine
  return (
    <>
      {n} account{n === 1 ? '' : 's'} &middot;{' '}
      {scope === null ? 'every tenant' : `${scope} — owned and lent to it`}
      {mine > 0 && (
        <>
          {' '}
          &middot; <strong>{mine} need{mine === 1 ? 's' : ''} a sign-in</strong>
        </>
      )}
      {lent > 0 && (
        <>
          {' '}
          &middot; {lent} lent account{lent === 1 ? '' : 's'} waiting on{' '}
          {lent === 1 ? 'its' : 'their'} owner
        </>
      )}
    </>
  )
}

function Body({
  board,
  ui,
  patch,
  reload,
}: {
  board: AccountsBoard
  ui: Persisted
  patch: Patch
  reload: () => void
}) {
  const accounts = [...board.page.accounts].sort((a, b) =>
    a.account_id.localeCompare(b.account_id),
  )
  return (
    <>
      <Broken accounts={accounts} scope={board.page.tenant_id} />
      <Pool board={board} accounts={accounts} ui={ui} patch={patch} reload={reload} />
      <Register board={board} ui={ui} patch={patch} reload={reload} />
      <Instructions />
      <Legend />
    </>
  )
}

/**
 * The accounts a timer will never fix, at the top, before anything else.
 *
 * REAUTH_REQUIRED is the one state the sweep deliberately stops retrying, so
 * it is the one state that stays until a person acts. Reporting it only as a
 * chip in a row it shares with five healthy ones is how it gets scrolled past.
 *
 * WHICH PERSON, THOUGH. A LENT account in this state is on the reader's screen
 * and is not theirs to fix: its row shows no paste control, no state control
 * and no refresh button, because those routes answer 404 for a tenant that does
 * not own the account. Telling every borrower to "open the row and paste a
 * fresh credential" sends them to a row that deliberately refuses, so the two
 * cases are separated here and only one of them is an instruction.
 */
function Broken({ accounts, scope }: { accounts: Account[]; scope: string | null }) {
  const broken = accounts.filter(needsAHuman)
  if (broken.length === 0) return null
  const mine = broken.filter((a) => isOwned(a, scope))
  const lent = broken.filter((a) => !isOwned(a, scope))
  const soleLentOwner = lent.length === 1 ? (lent[0]?.owner_tenant ?? null) : null
  return (
    <div className="banner bad" role="status">
      <strong>
        {broken.length} account{broken.length === 1 ? '' : 's'} cannot be refreshed
        by the platform
      </strong>
      {mine.length > 0 && (
        <div>
          {mine.map((a) => a.account_id).join(', ')} &mdash; the refresh token is
          gone or unreadable, so the sweep has stopped trying rather than spend the
          token endpoint&rsquo;s rate limit learning the same answer every five
          minutes. Nothing new is assigned to {mine.length === 1 ? 'it' : 'them'}.
          Open the row and paste a fresh credential; the steps are under{' '}
          <em>When an account goes REAUTH_REQUIRED</em> below.
        </div>
      )}
      {lent.length > 0 && (
        <div>
          {lent.map((a) => `${a.account_id} (owned by ${a.owner_tenant})`).join(', ')}{' '}
          &mdash; broken the same way, and <strong>not yours to fix</strong>.
          Replacing a credential, refreshing and changing state stay with the
          owning tenant, so those rows carry no controls and the routes answer{' '}
          <em>404, no such account in this tenant&rsquo;s pool</em> here. Nothing
          of yours will be assigned to{' '}
          {lent.length === 1 ? 'it' : 'them'} until{' '}
          {lent.length === 1 ? 'its owner signs in' : 'their owners sign in'}{' '}
          again, so the thing to do is ask{' '}
          {soleLentOwner !== null ? (
            <strong>{soleLentOwner}</strong>
          ) : (
            'the owners named above'
          )}
          .
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// The pool table -- exactly the five columns `cs status` prints
// ---------------------------------------------------------------------------

function Pool({
  board,
  accounts,
  ui,
  patch,
  reload,
}: {
  board: AccountsBoard
  accounts: Account[]
  ui: Persisted
  patch: Patch
  reload: () => void
}) {
  // REAUTH_REQUIRED rows start open: that is the one state where the row's
  // controls are the reason for visiting the screen. `ui.open` holds only the
  // rows somebody has explicitly opened or closed, so the default can change
  // under an operator (an account breaking while they look at it) without
  // overriding a choice they made.
  const isOpen = (a: Account) => ui.open[a.account_id] ?? needsAHuman(a)
  const toggle = (a: Account) =>
    patch((p) => ({ ...p, open: { ...p.open, [a.account_id]: !isOpen(a) } }))

  // One clock for the whole render. Reading Date.now() per cell would let two
  // cells in the same row disagree by a second, which is exactly the kind of
  // flicker that makes a number look untrustworthy when it is not.
  const now = Date.now()

  if (accounts.length === 0) {
    return (
      <section className="section panel">
        <h2>The pool</h2>
        <div className="state" role="status">
          <h3>No accounts registered</h3>
          <p>
            The read succeeded and returned nothing &mdash; this is a real zero,
            not a failed query. Until an account exists, every task whose runner
            profile names a subscription provider parks on a missing credential
            rather than failing, which costs nothing but also runs nothing.
            Register the first one below.
          </p>
        </div>
      </section>
    )
  }

  return (
    <section className="section panel">
      <h2>
        The pool
        <span className="count-chip">
          {accounts.length} account{accounts.length === 1 ? '' : 's'}
        </span>
      </h2>
      <div className="table-wrap">
        <table className="pools accounts">
          <thead>
            <tr>
              <th scope="col">ACCOUNT</th>
              <th scope="col" className="n">5H</th>
              <th scope="col" className="n">7D</th>
              <th scope="col" className="n">CLEARS</th>
              <th scope="col">STATE</th>
            </tr>
          </thead>
          <tbody>
            {accounts.map((a) => (
              <PoolRows
                key={a.account_id}
                account={a}
                board={board}
                now={now}
                open={isOpen(a)}
                onToggle={() => toggle(a)}
                ui={ui}
                patch={patch}
                reload={reload}
              />
            ))}
          </tbody>
        </table>
      </div>
      <Warnings accounts={accounts} scope={board.page.tenant_id} now={now} />
      <p className="provenance">
        {accounts.length} rows returned &middot; 5H and 7D are the provider&rsquo;s
        own windows, reported by workers &middot; a figure marked ~ is projected,
        not measured
      </p>
    </section>
  )
}

function PoolRows({
  account,
  board,
  now,
  open,
  onToggle,
  ui,
  patch,
  reload,
}: {
  account: Account
  board: AccountsBoard
  now: number
  open: boolean
  onToggle: () => void
  ui: Persisted
  patch: Patch
  reload: () => void
}) {
  const tone = accountTone(account.state)
  const five = readingOf(account, FIVE_HOUR)
  const seven = readingOf(account, SEVEN_DAY)

  return (
    <>
      <tr className={tone === 'bad' ? 'over' : tone === 'paused' ? 'paused' : undefined}>
        <th scope="row" className="pool-name">
          <button type="button" className="acct-open" aria-expanded={open} onClick={onToggle}>
            <span className="acct-caret" aria-hidden>
              {open ? '▾' : '▸'}
            </span>
            {/* The LABEL is the name `cs status` prints; the full id is what
                gets pasted into a command. Both are here, neither transformed. */}
            {account.label}
          </button>
          <span className="raw">{account.account_id}</span>
        </th>
        <WindowCell reading={five} window="five-hour" />
        <WindowCell reading={seven} window="seven-day" />
        <ClearsCell account={account} now={now} />
        <td>
          <span className={`tag acct-state ${tone}`}>{account.state}</span>
          <StateNote account={account} />
        </td>
      </tr>
      {open && (
        <tr className="acct-detail-row">
          <td colSpan={5}>
            <Detail
              account={account}
              board={board}
              now={now}
              ui={ui}
              patch={patch}
              reload={reload}
            />
          </td>
        </tr>
      )}
    </>
  )
}

/** Why a reading is what it is, in the cell's own tooltip. Never decoration. */
function readingTitle(r: AccountReading, window: string): string {
  switch (r.kind) {
    case 'never':
      return 'No reading has ever arrived for this account, so its utilisation is unknown. Unknown is not zero.'
    case 'absent':
      return `The last reading carried no ${window} window, so this is unmeasured. Unmeasured is not zero: the provider defines which windows it reports, and a new one appearing does not need a change here.`
    case 'reset':
      return `This window passed its reset ${timeAgo(r.resetsAt)}, so the figure describes the window before it and says nothing about now. Marked ~ for that reason.`
    case 'stale':
      return `Last read ${timeAgo(r.observedAt)}, older than the 30 minutes a reading is trusted for. These accounts are also used by a person at a laptop, so utilisation can move without the platform seeing any of it. Marked ~: projected, not measured.`
    case 'live':
      return `Read ${timeAgo(r.observedAt)}, within the window a reading is trusted for.`
  }
}

function WindowCell({ reading, window }: { reading: AccountReading; window: string }) {
  const title = readingTitle(reading, window)

  if (reading.kind === 'never' || reading.kind === 'absent') {
    return (
      <td className="n acct-window acct-unmeasured" title={title}>
        {/* AN EM DASH AND NO BAR. Drawing an empty five-cell bar here would be
            pixel-for-pixel identical to a measured 0%, which is the one
            confusion this column must never allow. */}
        <span className="acct-pct">&mdash;</span>
        <span className="acct-why">not measured</span>
      </td>
    )
  }

  const projected = isProjected(reading)
  return (
    <td className={`n acct-window${projected ? ' acct-projected' : ''}`} title={title}>
      <span className="acct-pct">
        {projected && <span className="acct-tilde">~</span>}
        {Math.round(reading.pct)}%
      </span>
      <Bar pct={reading.pct} projected={projected} />
    </td>
  )
}

/**
 * The five-cell bar, as `miniBar` draws it in claudeswitch: round to the
 * nearest fifth, clamp, five cells either filled or not.
 *
 * `aria-hidden`, because the percentage beside it is the same fact and a screen
 * reader announcing five geometric shapes learns nothing. Colour is never the
 * only signal here either: a projected bar is grey AND its figure carries `~`.
 *
 * NO AMBER BAND, and that is a decision rather than an omission: any threshold
 * between "fine" and "getting full" would be a number invented in this file,
 * and the platform's own assign floor lives in quota-broker where nothing
 * checks that a TypeScript copy still matches it. The one treatment here that
 * is a fact rather than a judgement is a window that is fully spent.
 */
function Bar({ pct, projected }: { pct: number; projected: boolean }) {
  const filled = barFilled(pct)
  const spent = !projected && pct >= 100
  return (
    <span
      className={`acct-bar${projected ? ' acct-projected' : ''}${spent ? ' spent' : ''}`}
      aria-hidden
    >
      {Array.from({ length: BAR_CELLS }, (_, i) => (
        <i key={i} className={i < filled ? 'on' : ''} />
      ))}
    </span>
  )
}

/**
 * Time until the BINDING window resets -- the one that will actually stop you.
 *
 * Not the five-hour and not an average: an account at 5% on its five-hour and
 * 90% on its weekly is stopped by the weekly, and refilling the five-hour does
 * nothing for it.
 */
function ClearsCell({ account, now }: { account: Account; now: number }) {
  const binding = bindingWindow(account)
  if (binding === null) {
    return (
      <td
        className="n acct-unmeasured"
        title="No window reading, so there is no reset instant to count down to. This is an absence of information, not a window that never clears."
      >
        &mdash;
      </td>
    )
  }
  const reading = readingOf(account, binding.key)
  const windowName = binding.key.replace(/_/g, '-')

  // A binding window that has ALREADY reset does not have a countdown; it has
  // happened. "~now" would be arithmetic pretending to be an answer, and it
  // reads as "about to clear" when the truth is the opposite -- it cleared, and
  // what is missing is a reading taken since.
  if (binding.window.reset) {
    return (
      <td
        className="n acct-projected"
        title={`The ${windowName} window, the binding one, passed its reset ${timeAgo(binding.window.resets_at)}. It has cleared; no reading taken since has arrived, so the figures on this row still describe the window before it.`}
      >
        cleared
      </td>
    )
  }
  // The INSTANT is the provider's and counting down to it is arithmetic; what
  // is projected is the CHOICE of which window binds, when that choice came
  // from a figure that is no longer current. Marked, and the tooltip says which
  // part is uncertain.
  const uncertain = isProjected(reading)
  return (
    <td
      className={`n${uncertain ? ' acct-projected' : ''}`}
      title={
        uncertain
          ? `Counts down to the ${windowName} window's reset, which the provider gave as an exact instant. Marked ~ because which window binds was decided from a figure that is no longer current.`
          : `The ${windowName} window is the binding one, the one that will refuse first, and this is when it resets.`
      }
    >
      {uncertain && <span className="acct-tilde">~</span>}
      {clearsIn(binding.window.resets_at, now)}
    </td>
  )
}

/** The second line under a state chip, when there is a real one to write. */
function StateNote({ account }: { account: Account }) {
  if (account.reason) return <span className="acct-why">{account.reason}</span>
  const binding = bindingWindow(account)
  if (binding && binding.window.reset) {
    // `cs status` words this "window reset - re-reading", and it means the
    // figures on this row describe a window that has already refilled.
    return <span className="acct-why">window reset, awaiting a new reading</span>
  }
  if (account.observed_at === null) return <span className="acct-why">never observed</span>
  if (account.stale) {
    return <span className="acct-why">reading is {timeAgo(account.observed_at)}</span>
  }
  return null
}

/**
 * The block `cs status` prints under its table: everything a reader should act
 * on, off the row it belongs to rather than crowding it.
 */
function Warnings({
  accounts,
  scope,
  now,
}: {
  accounts: Account[]
  scope: string | null
  now: number
}) {
  const lines: string[] = []
  for (const a of accounts) {
    if (needsAHuman(a)) {
      // The instruction only goes to the person who can carry it out. A lent
      // row has no paste control and no state control by design, so sending a
      // borrower there is sending them to a dead end.
      lines.push(
        isOwned(a, scope)
          ? `${a.account_id} needs a sign-in. Paste a fresh credential into its row, then put its state back.`
          : `${a.account_id} needs a sign-in, and ${a.owner_tenant} owns it. Replacing its credential and changing its state belong to that tenant -- those routes answer 404 here -- so its row has no controls and this one is fixed by its owner, not from this screen.`,
      )
      continue
    }
    if (a.observed_at === null) {
      // HEADROOM IS NOT THE WHOLE GATE. An unobserved account is treated as
      // having room -- a new account has to be assignable before it can report
      // anything -- but ASSIGNABLE_STATES is {AVAILABLE} alone, so a paused or
      // draining account with all the headroom in the world is still not
      // assignable. Saying "still assignable" beside a PAUSED chip is a claim
      // about the platform that the platform contradicts.
      lines.push(
        a.state === 'AVAILABLE'
          ? `${a.account_id} has never been observed: no worker has reported a rate-limit reading for it yet. It is still assignable, because the broker treats an unobserved account as having room on purpose -- a new account has to be assignable before it can report anything.`
          : `${a.account_id} has never been observed: no worker has reported a rate-limit reading for it yet. Its headroom is treated as full for that reason, but it is in ${a.state}, and AVAILABLE is the only state a new agent may be started on -- so nothing will be assigned to it until its state changes.`,
      )
      continue
    }
    if (a.stale) {
      lines.push(
        `${a.account_id} reading is ${timeAgo(a.observed_at)}, older than the 30 minutes a reading is trusted for, so its figures are shown projected.`,
      )
    }
    const binding = bindingWindow(a)
    if (binding && !binding.window.reset) {
      const ms = new Date(binding.window.resets_at).getTime() - now
      if (Number.isFinite(ms) && ms < 0) {
        lines.push(
          `${a.account_id} has a reset instant in the past that the platform did not mark as reset, so this browser's clock and the platform's disagree. Treat CLEARS on that row as approximate.`,
        )
      }
    }
  }
  if (lines.length === 0) return null
  return (
    <ul className="acct-warnings">
      {lines.map((l) => (
        <li key={l}>{l}</li>
      ))}
    </ul>
  )
}

// ---------------------------------------------------------------------------
// One account, opened
// ---------------------------------------------------------------------------

function Detail({
  account,
  board,
  now,
  ui,
  patch,
  reload,
}: {
  account: Account
  board: AccountsBoard
  now: number
  ui: Persisted
  patch: Patch
  reload: () => void
}) {
  // `tenant_id` is echoed from the verified token by routes/accounts.py, so it
  // is the caller's tenant and nothing else. `isOwned` is the one definition of
  // this test, shared with the banner and the warnings list so that what points
  // at a row and what the row offers cannot disagree.
  const owned = isOwned(account, board.page.tenant_id)

  return (
    <div className="acct-detail">
      <dl className="kv acct-facts">
        <dt>Account id</dt>
        <dd className="mono">{account.account_id}</dd>
        <dt>Owner</dt>
        <dd className="mono">
          {account.owner_tenant}
          {!owned && <span className="acct-why">lent to you; not yours to change</span>}
        </dd>
        <dt>Provider</dt>
        <dd className="mono">{account.provider}</dd>
        <dt>Agents on it</dt>
        <dd>
          {account.assigned}
          <span className="acct-why">
            advisory; the lease is the authoritative record of who holds what
          </span>
        </dd>
        <dt>Last reading</dt>
        <dd>
          {account.observed_at === null ? (
            <>
              never
              <span className="acct-why">
                no worker has reported a rate-limit reading for this account
              </span>
            </>
          ) : (
            <>
              {timeAgo(account.observed_at)}
              {account.stale && (
                <span className="acct-why">past the 30 minutes a reading is trusted for</span>
              )}
            </>
          )}
        </dd>
      </dl>

      <AllWindows account={account} now={now} />
      {owned ? (
        <>
          <RefreshControl account={account} ui={ui} patch={patch} reload={reload} />
          <Lending account={account} board={board} reload={reload} />
          <StateControls account={account} reload={reload} />
          <Reauth account={account} ui={ui} patch={patch} reload={reload} />
          <Remove account={account} reload={reload} />
        </>
      ) : (
        <Borrowed account={account} />
      )}
    </div>
  )
}

/**
 * A lent account, with no controls and the reason in place of them.
 *
 * `GET /v1/accounts` returns what a tenant may RUN on -- owned AND lent -- and
 * that is right for the list. It is wrong as an authorisation set for a write,
 * so every mutating route in routes/accounts.py resolves the account and
 * requires ownership. Rendering the buttons anyway would be a control that
 * silently 404s, which is worse than no control: the operator would conclude
 * the account had vanished rather than that it was never theirs to change.
 */
function Borrowed({ account }: { account: Account }) {
  return (
    <div className="acct-action">
      <h4>Lent to you</h4>
      <p className="muted small">
        <strong>{account.owner_tenant}</strong> owns this account and has lent it
        to you. Your agents can run on it, and pausing, draining, re-lending,
        refreshing and removing it stay with its owner. Those routes answer{' '}
        <em>404, no such account in this tenant&rsquo;s pool</em> here &mdash; the
        same answer a label that does not exist gets, so that asking cannot
        confirm somebody else&rsquo;s account names.
      </p>
    </div>
  )
}

/**
 * Every window the provider reported, not only the two with columns.
 *
 * `Account.windows` is keyed by the provider's own window names precisely so a
 * new one can appear without a schema change. A screen that reads exactly two
 * keys and silently drops the rest would make that design invisible -- and the
 * first new window would be invisible with it.
 */
function AllWindows({ account, now }: { account: Account; now: number }) {
  const keys = Object.keys(account.windows).sort()
  const extra = keys.filter((k) => k !== FIVE_HOUR && k !== SEVEN_DAY)
  if (keys.length === 0) {
    return (
      <p className="muted small">
        The provider has reported no windows for this account, so 5H, 7D and
        CLEARS are all unmeasured above rather than zero.
      </p>
    )
  }
  if (extra.length === 0) return null
  return (
    <div className="acct-extra-windows">
      <h4>Windows with no column</h4>
      <p className="muted small">
        The provider defines its own window names and may add one at any time.
        These arrived in the reading and have no column of their own, so they are
        listed rather than dropped.
      </p>
      {extra.map((k) => {
        const r = readingOf(account, k)
        const w = account.windows[k]
        return (
          <div className="split-row" key={k}>
            <span className="sr-name">{k}</span>
            <span className="sr-n">
              {r.kind === 'never' || r.kind === 'absent'
                ? '—'
                : `${isProjected(r) ? '~' : ''}${Math.round(r.pct)}%`}
            </span>
            <ExtraClears name={k.replace(/_/g, '-')} w={w} now={now} />
          </div>
        )
      })}
    </div>
  )
}

/**
 * The clears figure for a window with no column, under the same rule CLEARS
 * uses: a window that has ALREADY reset does not get a countdown.
 *
 * `humaniseUntil` returns `now` for any duration at or below zero, and `now`
 * reads as "about to clear" when the truth is the opposite -- it cleared, and
 * what is missing is a reading taken since. These are exactly the windows a
 * provider can add without a schema change, so this is the block where the
 * first such window will appear, and it has to be right here too rather than
 * only in the column.
 */
function ExtraClears({
  name,
  w,
  now,
}: {
  name: string
  w: AccountWindow | undefined
  now: number
}) {
  if (!w) {
    return (
      <span
        className="sr-by acct-unmeasured"
        title={`The provider reported no ${name} window in the last reading, so there is no reset instant to count down to.`}
      >
        —
      </span>
    )
  }
  if (w.reset) {
    return (
      <span
        className="sr-by acct-projected"
        title={`The ${name} window passed its reset ${timeAgo(w.resets_at)}. It has cleared; no reading taken since has arrived, so the percentage beside it still describes the window before it.`}
      >
        cleared
      </span>
    )
  }
  return (
    <span
      className="sr-by"
      title={`When the ${name} window resets, as the provider gave it.`}
    >
      {clearsIn(w.resets_at, now)}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Refresh
// ---------------------------------------------------------------------------

type RefreshState =
  | { kind: 'idle' | 'sending' }
  | { kind: 'answered'; result: RefreshResult }
  | { kind: 'failed'; error: ApiError }

/**
 * What each of the outcomes in quota_broker/credentials.py actually means for
 * the person who pressed the button.
 *
 * `ok` is NOT `refreshed`. Two of these exchanged nothing and are still
 * complete successes -- `still_valid` means the stored token is fine and the
 * pod-facing secret already carries it, which is the commonest answer on a
 * healthy account and must not be painted as a failure.
 */
function refreshVerdict(r: RefreshResult): { ok: boolean; heading: string; body: ReactNode } {
  switch (r.reason) {
    case 'refreshed':
      return {
        ok: true,
        heading: 'Exchanged',
        body: (
          <>
            A new pair was obtained and written: the refresh half first, then the
            access token the pods mount.{' '}
            <strong>The previous access token is now revoked</strong> &mdash; that
            is what exchanging one does, and it is why nothing outside
            quota-broker may ever do it.
          </>
        ),
      }
    case 'published':
      return {
        ok: true,
        heading: 'Published',
        body: (
          <>
            The stored token was still valid, but the secret a pod mounts did not
            carry it. It does now. This is the case that silently breaks every
            task for an account whose credential is working perfectly, so seeing
            it here means something was genuinely fixed.
          </>
        ),
      }
    case 'still_valid':
      return {
        ok: true,
        heading: 'Nothing to do',
        body: (
          <>
            The stored token is still valid and the pod-facing secret already
            carries it, so nothing was exchanged and nothing was written. This is
            the healthy answer, not a failure &mdash; an exchange only happens
            inside the last three hours of the access token&rsquo;s life.
          </>
        ),
      }
    case 'reauth_required':
      return {
        ok: false,
        heading: 'The refresh token is gone',
        body: (
          <>
            The token endpoint refused the exchange. Only a person can fix this,
            so the broker has stopped trying and moved this account to
            REAUTH_REQUIRED. Paste a fresh credential below, then put the state
            back &mdash; re-registering deliberately does not do that for you.
          </>
        ),
      }
    case 'unreadable':
      return {
        ok: false,
        heading: 'The stored credential could not be parsed',
        body: (
          <>
            What is in Secret Manager is not a credential this can read, so there
            is nothing to exchange. The account has been moved to REAUTH_REQUIRED.
            Paste the keychain item again, verbatim and unedited.
          </>
        ),
      }
    case 'no_refresh_credential':
      return {
        ok: false,
        heading: 'There is no refresh half to exchange',
        body: (
          <>
            No <code>-refresh</code> secret exists for this account, so there is
            no half to exchange. That is the shape a{' '}
            <code>claude setup-token</code> value has &mdash; one long-lived
            access token and nothing beside it &mdash; and it cannot be kept
            alive by anything. Register this label again with the full keychain
            item, which is a pair.
          </>
        ),
      }
    case 'store_unavailable':
      return {
        ok: false,
        heading: 'Secret Manager did not answer',
        body: (
          <>
            The credential was never read, so this says nothing about whether it
            is healthy &mdash; it is a failure to reach the store, not a verdict
            on the account. Nothing was written. Try again.
          </>
        ),
      }
    case 'publish_failed':
      return {
        ok: false,
        heading: 'The token is fine; writing it failed',
        body: (
          <>
            A valid access token could not be written to the secret pods mount, so
            pods are still reading whatever was there before. The credential is
            not the problem. Try again, and if it persists this is a Secret
            Manager permission or availability problem rather than an account one.
          </>
        ),
      }
    case 'refresh_failed':
      return {
        ok: false,
        heading: 'The exchange failed, and may be transient',
        body: (
          <>
            The token endpoint did not complete the exchange, and did not say the
            credential is dead. The account has deliberately NOT been moved to
            REAUTH_REQUIRED, so the sweep will try again on its next tick. If it
            keeps answering this, treat it as needing a sign-in.
          </>
        ),
      }
    default:
      return {
        ok: r.refreshed,
        heading: r.refreshed ? 'Refreshed' : 'Not refreshed',
        // An unrecognised reason is reported verbatim rather than mapped onto
        // the nearest known one. A wrong explanation is worse than none.
        body: (
          <>
            The broker answered with a reason this screen has no copy for:{' '}
            <code>{r.reason}</code>. It is shown as the broker wrote it rather
            than guessed at.
          </>
        ),
      }
  }
}

function RefreshControl({
  account,
  ui,
  patch,
  reload,
}: {
  account: Account
  ui: Persisted
  patch: Patch
  reload: () => void
}) {
  // Held above the remount boundary: the verdict is the whole point of the
  // button, and reloading the table must not take it away.
  const state = ui.refresh[account.account_id] ?? { kind: 'idle' }
  const setState = (next: RefreshState) =>
    patch((p) => ({ ...p, refresh: { ...p.refresh, [account.account_id]: next } }))

  const run = async () => {
    setState({ kind: 'sending' })
    const res = await refreshAccount(account.account_id)
    if (res.status === 'ok') {
      setState({ kind: 'answered', result: res.data.refresh })
      reload()
      return
    }
    if (res.status === 'error' || res.status === 'stale') {
      setState({ kind: 'failed', error: res.error })
      return
    }
    setState({ kind: 'idle' })
  }

  return (
    <div className="acct-action">
      <h4>Refresh now</h4>
      <p className="muted small">
        Runs the same code path the sweep runs, for this account, and reports what
        happened. It exists because a timer gives you no way to answer
        &ldquo;did that work?&rdquo;.
      </p>
      <button type="button" disabled={state.kind === 'sending'} onClick={() => void run()}>
        {state.kind === 'sending' ? 'exchanging…' : 'Refresh this account'}
      </button>

      {state.kind === 'answered' && <RefreshAnswer result={state.result} />}
      {state.kind === 'failed' && (
        <>
          <FailedPanel error={state.error} onRetry={() => void run()} />
          {/* A write is not a read. A failure after the request left the browser
              does not prove nothing happened, and here "something happened"
              means a token may already have been revoked. */}
          <p className="warn-text">
            This failed on a write. If it failed after reaching the broker, the
            credential may have been exchanged anyway and the previous access
            token revoked with it &mdash; reload this screen before pressing
            refresh again.
          </p>
        </>
      )}
    </div>
  )
}

function RefreshAnswer({ result }: { result: RefreshResult }) {
  const verdict = refreshVerdict(result)
  // An expiry already in the past is a real and alarming answer -- a published
  // token that is dead on arrival -- so it gets its own words rather than
  // "expires in now", which reads as "shortly" and means the opposite.
  const expiresAt = result.expires_at ? new Date(result.expires_at).getTime() : null
  const expired = expiresAt !== null && Number.isFinite(expiresAt) && expiresAt <= Date.now()
  return (
    <div className={`state ${verdict.ok ? 'acct-ok' : 'failed'}`} role="status">
      <h3>{verdict.heading}</h3>
      <p>{verdict.body}</p>
      <p className="checked-at">
        {/* Both raw fields, because they are what the broker actually said and
            what a log line or a bug report will be matched against. */}
        <code>refreshed={String(result.refreshed)}</code>{' '}
        <code>reason={result.reason}</code>
        {result.expires_at &&
          (expired ? (
            <>
              {' '}
              &middot;{' '}
              <strong>
                the access token it reports is ALREADY EXPIRED, so pods will fail
                on it
              </strong>
            </>
          ) : (
            <>
              {' '}
              &middot; the access token this account publishes expires in{' '}
              {clearsIn(result.expires_at, Date.now())}
            </>
          ))}
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Lending
// ---------------------------------------------------------------------------

/**
 * Which other tenants this account will serve.
 *
 * ISOLATION IS THE DEFAULT AND SHARING IS A DECISION WITH A NAME ON IT.
 * CONTRACT.md invariant 9 says a tenant's credentials must never be reachable
 * from another tenant's pod; lending narrows that to "unless its owner said so,
 * in writing, per account" rather than punching a hole in it. So this control
 * starts empty, says plainly who can reach the credential, and never offers an
 * "everyone" shortcut.
 */
function Lending({
  account,
  board,
  reload,
}: {
  account: Account
  board: AccountsBoard
  reload: () => void
}) {
  const [draft, setDraft] = useState(account.lend_to.join(', '))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [saved, setSaved] = useState(false)

  const parsed = splitTenants(draft)
  const owner = account.owner_tenant
  // Silently dropping the owner would make the saved list differ from the typed
  // one with nothing on screen to explain why, so it is called out instead.
  const includesOwner = parsed.includes(owner)
  const willSave = parsed.filter((t) => t !== owner)
  const dirty = willSave.join(' ') !== [...account.lend_to].join(' ')

  const save = async () => {
    setBusy(true)
    setError(null)
    setSaved(false)
    const res = await setAccountLending(account.account_id, willSave)
    setBusy(false)
    if (res.status === 'ok') {
      setSaved(true)
      reload()
    } else if (res.status === 'error' || res.status === 'stale') {
      setError(res.error)
    }
  }

  return (
    <div className="acct-action">
      <h4>Lending</h4>
      <p className="muted small">
        {account.lend_to.length === 0 ? (
          <>
            This account serves <strong>{owner}</strong> only. No other
            tenant&rsquo;s pod can reach its credential.
          </>
        ) : (
          <>
            Besides <strong>{owner}</strong>, this account will serve{' '}
            <strong>{account.lend_to.join(', ')}</strong>. A pod belonging to any
            of those tenants can mount its access token.
          </>
        )}
      </p>
      <span className="limit-edit">
        <input
          type="text"
          className="mono acct-wide"
          value={draft}
          disabled={busy}
          placeholder="tenant ids, comma separated; empty means no lending"
          aria-label={`Tenants ${account.account_id} is lent to`}
          list={board.tenants ? 'acct-tenant-ids' : undefined}
          onChange={(e) => {
            setDraft(e.target.value)
            setSaved(false)
          }}
        />
        <button type="button" onClick={() => void save()} disabled={!dirty || busy}>
          {busy ? 'saving…' : 'save lending'}
        </button>
        {saved && <span className="tag ok">saved</span>}
      </span>
      {includesOwner && (
        <p className="muted small">
          <code>{owner}</code> owns this account, so it is not sent as a loan: an
          account cannot be lent to itself, and the platform would drop it anyway.
        </p>
      )}
      {board.tenants === null && (
        <p className="muted small">
          There is no tenant picker here because the tenant list could not be
          read: {board.tenantsDetail} That endpoint is admin-only, so a non-admin
          genuinely cannot list tenants. Nothing is broken, and tenant ids typed
          here are still checked by the platform when work is admitted.
        </p>
      )}
      {board.tenants !== null && (
        <datalist id="acct-tenant-ids">
          {board.tenants.map((t) => (
            <option key={t.tenant_id} value={t.tenant_id} />
          ))}
        </datalist>
      )}
      {error && (
        <p className="warn-text">
          {errorHeading(error)} &mdash; {error.message} The lending list was not
          changed.
        </p>
      )}
    </div>
  )
}

function splitTenants(raw: string): string[] {
  const seen: string[] = []
  for (const part of raw.split(/[\s,]+/)) {
    const t = part.trim()
    if (t !== '' && !seen.includes(t)) seen.push(t)
  }
  return seen
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

/**
 * PAUSED, DRAINING and back.
 *
 * The three are not degrees of one thing and the copy must not let them read
 * that way: PAUSED means "no new work, keep what is running", DRAINING means
 * "get off", and AVAILABLE is the only state a NEW agent may be started on.
 * REAUTH_REQUIRED is deliberately absent from these buttons -- it is a verdict
 * the broker reaches from a failed exchange, not a state to declare by hand.
 */
function StateControls({ account, reload }: { account: Account; reload: () => void }) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState<AccountStateName | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [done, setDone] = useState<AccountStateName | null>(null)

  const move = async (to: AccountStateName) => {
    setBusy(to)
    setError(null)
    setDone(null)
    const res = await setAccountState(account.account_id, to, reason.trim())
    setBusy(null)
    if (res.status === 'ok') {
      setDone(to)
      setReason('')
      reload()
    } else if (res.status === 'error' || res.status === 'stale') {
      setError(res.error)
    }
  }

  const targets: { to: AccountStateName; why: string }[] = [
    { to: 'AVAILABLE', why: 'assignable to new agents' },
    { to: 'PAUSED', why: 'no new agents; the ones on it keep running' },
    { to: 'DRAINING', why: 'no new agents, and the ones on it are moved off' },
  ]

  return (
    <div className="acct-action">
      <h4>State</h4>
      <p className="muted small">
        The reason is stored with the change and shown in the STATE column,
        because the next person to look is usually not you.
      </p>
      <span className="limit-edit">
        <input
          type="text"
          className="acct-wide"
          value={reason}
          disabled={busy !== null}
          placeholder="why, e.g. held back for Monday's release run"
          aria-label={`Reason for changing the state of ${account.account_id}`}
          onChange={(e) => setReason(e.target.value)}
        />
      </span>
      <div className="acct-buttons">
        {targets.map((t) => (
          <button
            key={t.to}
            type="button"
            title={t.why}
            disabled={busy !== null || account.state === t.to}
            onClick={() => void move(t.to)}
          >
            {busy === t.to ? 'saving…' : `move to ${t.to}`}
          </button>
        ))}
      </div>
      {done && <p className="muted small">Moved to {done}.</p>}
      {error && (
        <p className="warn-text">
          {errorHeading(error)} &mdash; {error.message} The state was not changed.
        </p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Re-authenticate / replace the credential
// ---------------------------------------------------------------------------

type ReauthState =
  | { kind: 'idle' | 'sending' }
  /** The credential landed. `stateRestored` says whether the second step did. */
  | { kind: 'replaced'; stateRestored: boolean; stateError: ApiError | null; wasBroken: boolean }
  | { kind: 'failed'; error: ApiError }

/**
 * Paste a fresh credential over an existing account.
 *
 * TWO STEPS, VISIBLY, because the store makes them two steps. Re-registering an
 * existing label replaces the credential and deliberately PRESERVES the state
 * (accountstore.py:106-124) so that it cannot silently un-pause an account
 * somebody paused. The consequence is that an account in REAUTH_REQUIRED is
 * still in REAUTH_REQUIRED after a perfectly successful paste, and nothing will
 * be assigned to it. So the state is put back as an explicit second call, and
 * if that call fails this says so rather than reporting one green tick for two
 * operations.
 */
function Reauth({
  account,
  ui,
  patch,
  reload,
}: {
  account: Account
  ui: Persisted
  patch: Patch
  reload: () => void
}) {
  // The pasted value stays LOCAL and dies with the remount -- that is the point
  // of it. The report of what happened to it is what has to survive.
  const [credential, setCredential] = useState('')
  const state = ui.reauth[account.account_id] ?? { kind: 'idle' }
  const setState = (next: ReauthState) =>
    patch((p) => ({ ...p, reauth: { ...p.reauth, [account.account_id]: next } }))
  const broken = needsAHuman(account)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (credential.trim() === '') return
    // Taken out of component state BEFORE the request goes anywhere, so the
    // pasted secret is a local in one async call and is never in state again --
    // not while the request is in flight, and not after it answers.
    const pasted = credential
    setCredential('')
    setState({ kind: 'sending' })

    // Same label, same provider, same lending list: re-registering an existing
    // label is the replace path, and any of those omitted would be REPLACED
    // with a default rather than left alone. `owner_tenant` is not sent for the
    // reason registerAccount gives.
    const res = await registerAccount({
      label: account.label,
      provider: account.provider,
      lend_to: account.lend_to,
      credential: pasted,
    })
    if (res.status !== 'ok') {
      setState({ kind: 'failed', error: failureOf(res) })
      return
    }
    if (!broken) {
      setState({ kind: 'replaced', stateRestored: true, stateError: null, wasBroken: false })
      reload()
      return
    }
    const back = await setAccountState(
      account.account_id,
      'AVAILABLE',
      'credential replaced from the Accounts screen',
    )
    setState({
      kind: 'replaced',
      stateRestored: back.status === 'ok',
      stateError: back.status === 'error' || back.status === 'stale' ? back.error : null,
      wasBroken: true,
    })
    reload()
  }

  return (
    <form className="acct-action" onSubmit={(e) => void submit(e)}>
      <h4>{broken ? 'Re-authenticate' : 'Replace the credential'}</h4>
      <p className="muted small">
        Paste the keychain item from a machine that is signed in, verbatim, the{' '}
        <code>claudeAiOauth</code> wrapper included. It replaces the credential
        behind <code>{account.account_id}</code> and keeps the id, the readings
        and the lending list, because the label is the same.
      </p>
      <textarea
        className="mono"
        rows={4}
        value={credential}
        spellCheck={false}
        autoComplete="off"
        placeholder={'{"claudeAiOauth":{"accessToken":"...","refreshToken":"...","expiresAt":0}}'}
        aria-label={`Replacement credential for ${account.account_id}`}
        onChange={(e) => setCredential(e.target.value)}
      />
      <p className="acct-buttons">
        <button type="submit" disabled={state.kind === 'sending' || credential.trim() === ''}>
          {state.kind === 'sending' ? 'sending…' : 'Replace credential'}
        </button>
      </p>

      {state.kind === 'replaced' && (
        <div className={`state ${state.stateRestored ? 'acct-ok' : 'partial'}`} role="status">
          <h3>Credential replaced</h3>
          <p>
            The pair was parsed and written before anything else &mdash; a
            credential with no refresh token would have been refused here rather
            than at the next sweep.{' '}
            {state.wasBroken ? (
              state.stateRestored ? (
                <>
                  This account was in REAUTH_REQUIRED, so its state has been put
                  back to AVAILABLE as a second step. Press{' '}
                  <em>Refresh this account</em> above to confirm the new
                  credential exchanges.
                </>
              ) : (
                <>
                  <strong>
                    This account is STILL in REAUTH_REQUIRED and nothing will be
                    assigned to it.
                  </strong>{' '}
                  The credential is in place, but putting the state back failed:{' '}
                  {state.stateError?.message ?? 'the request did not complete.'}{' '}
                  Use <em>move to AVAILABLE</em> above.
                </>
              )
            ) : (
              <>
                The state was left exactly as it was, deliberately: replacing a
                credential must not silently un-pause an account somebody paused.
              </>
            )}
          </p>
        </div>
      )}

      {state.kind === 'failed' && (
        <>
          <FailedPanel error={state.error} onRetry={() => undefined} />
          <p className="warn-text">
            The box was cleared the moment this was sent, so the credential is not
            sitting in this page. Copy it again to retry.
          </p>
        </>
      )}
    </form>
  )
}

/** A non-ok Result turned into the one error it carries. */
function failureOf(res: Result<unknown>): ApiError {
  if (res.status === 'error' || res.status === 'stale') return res.error
  return {
    kind: 'server_error',
    httpStatus: null,
    code: null,
    message: 'The request did not complete, and the platform did not say why.',
  }
}

// ---------------------------------------------------------------------------
// Remove
// ---------------------------------------------------------------------------

/**
 * Typed confirmation, and there is deliberately no "don't ask again".
 *
 * The thing that makes this recoverable is worth saying out loud rather than
 * leaving as a surprise: the Firestore document goes, the SECRET STAYS.
 * Deleting a Secret Manager secret is irreversible and takes its version
 * history with it, so an account removed by mistake stays re-registerable under
 * the same label. Cleaning secrets up is the reconciler's job, on its own
 * schedule.
 */
function Remove({ account, reload }: { account: Account; reload: () => void }) {
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  const armed = typed === account.label

  const run = async () => {
    if (!armed) return
    setBusy(true)
    setError(null)
    const res = await removeAccount(account.account_id)
    setBusy(false)
    if (res.status === 'ok') {
      setTyped('')
      reload()
    } else if (res.status === 'error' || res.status === 'stale') {
      setError(res.error)
    }
  }

  return (
    <div className="acct-action danger">
      <h4>Remove</h4>
      <p className="muted small">
        Removes the account from the pool. The credential in Secret Manager is{' '}
        <strong>retained</strong>, not deleted, so this is reversible by
        registering the same label again and no version history is lost.
        {account.assigned > 0 && (
          <>
            {' '}
            <strong>
              {account.assigned} agent{account.assigned === 1 ? '' : 's'} currently
              hold{account.assigned === 1 ? 's' : ''} this account.
            </strong>{' '}
            Move it to DRAINING first if you want them off it before it goes.
          </>
        )}
      </p>
      <span className="limit-edit">
        <input
          type="text"
          className="mono"
          value={typed}
          disabled={busy}
          placeholder={account.label}
          aria-label={`Type ${account.label} to confirm removal`}
          onChange={(e) => setTyped(e.target.value)}
        />
        <button
          type="button"
          className="danger"
          disabled={!armed || busy}
          onClick={() => void run()}
        >
          {busy ? 'removing…' : `remove ${account.label}`}
        </button>
        <span className="client-side">type the label to arm this</span>
      </span>
      {error && (
        <p className="warn-text">
          {errorHeading(error)} &mdash; {error.message} The account was not
          removed.
        </p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Register
// ---------------------------------------------------------------------------

type RegisterState =
  | { kind: 'idle' | 'sending' }
  | { kind: 'done'; label: string }
  | { kind: 'failed'; error: ApiError }

function Register({
  board,
  ui,
  patch,
  reload,
}: {
  board: AccountsBoard
  ui: Persisted
  patch: Patch
  reload: () => void
}) {
  // THE OWNER IS THE TOKEN'S TENANT, and `tenant_id` here is that value echoed
  // by routes/accounts.py -- deliberately not taken from the broker's answer,
  // "so the page can never be told it is looking at a tenant it is not". It is
  // therefore the tenant the register route will file this under, and naming
  // anything else on screen would be a promise the API does not keep.
  const owner = board.page.tenant_id
  const [label, setLabel] = useState('')
  const [lend, setLend] = useState('')
  const [credential, setCredential] = useState('')
  // Above the remount boundary, same reason as the refresh verdict: registering
  // reloads the table, and "Registered laptop" must still be on screen after it.
  const state = ui.register
  // Returning the SAME object makes React bail out of the update, so typing in
  // the label field does not re-render the whole screen once per keystroke
  // merely to set a value that is already set.
  const setState = (next: RegisterState) =>
    patch((p) =>
      p.register.kind === 'idle' && next.kind === 'idle' ? p : { ...p, register: next },
    )

  // NOT CONFIGURED MEANS REFUSE, never accept-anything. Without a named tenant
  // this form could still submit safely -- the API files the account under the
  // verified token whatever the page thinks -- but it could not TELL the
  // operator whose pool the credential is about to land in, and a live
  // credential is not something to hand over on a maybe.
  if (owner === null) {
    return (
      <section className="section panel">
        <h2>Register an account</h2>
        <div className="state partial" role="status">
          <h3>Registration is unavailable until your tenant is named</h3>
          <p>
            An account is owned by exactly one tenant, and the API decides which
            from your verified sign-in rather than from anything typed here. This
            response did not name one, so this form cannot tell you whose pool a
            credential would land in &mdash; and that is not something to guess
            at with a live credential.
          </p>
          <p style={{ marginTop: 8 }}>
            This is a gap in what was read, not a fault in the platform. It says
            nothing about the accounts above, which loaded.
          </p>
        </div>
      </section>
    )
  }

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (label.trim() === '' || credential.trim() === '') return
    // Out of state before the request leaves, and never put back.
    const pasted = credential
    const chosen = label.trim()
    setCredential('')
    setState({ kind: 'sending' })
    // No `owner_tenant`: the route files this under the verified token and
    // reads that field only to refuse a mismatch, so sending it can only
    // produce a 403 for a disagreement that is not the operator's doing.
    const res = await registerAccount({
      label: chosen,
      provider: SUBSCRIPTION_PROVIDER,
      lend_to: splitTenants(lend).filter((t) => t !== owner),
      credential: pasted,
    })
    if (res.status === 'ok') {
      setState({ kind: 'done', label: chosen })
      setLabel('')
      setLend('')
      reload()
      return
    }
    setState({ kind: 'failed', error: failureOf(res) })
  }

  return (
    <form className="section panel" onSubmit={(e) => void submit(e)}>
      <h2>Register an account</h2>
      <p className="conjunction">
        Owned by <strong>{owner}</strong>. The credential is{' '}
        <strong>write-only</strong>: it is sent once, split into two Secret
        Manager secrets, and no route in this platform returns it. This screen
        never shows key material &mdash; not the token, and not its length.
      </p>

      {/* NOT A PICKER, AND NOT A SILENT DEFAULT EITHER. There is one kind of
          credential in this pool, so there is nothing to choose; but a value
          the request carries and the form never mentions is a decision made
          for the operator with nothing on screen admitting it. So it is shown,
          named, and said to be fixed. */}
      <span className="t-label">provider</span>
      <p className="acct-fixed mono">
        {SUBSCRIPTION_PROVIDER}
        <span className="acct-why">
          fixed, and the only kind this pool handles: a Claude subscription. The
          credential is the OAuth pair Claude Code keeps after <code>/login</code>,
          which quota-broker can exchange for a successor indefinitely &mdash;
          that is what makes &ldquo;the only manual step is the first one&rdquo;
          true. There is deliberately no API-key option here: a key that cannot
          be exchanged has nothing for the sweep to keep alive.
        </span>
      </p>

      <label className="t-label" htmlFor="acct-label">
        label
      </label>
      <input
        id="acct-label"
        className="mono acct-wide"
        value={label}
        spellCheck={false}
        autoComplete="off"
        placeholder="laptop"
        onChange={(e) => {
          setLabel(e.target.value)
          setState({ kind: 'idle' })
        }}
      />
      <p className="muted small">
        Lowercase letters, digits and dashes, starting and ending alphanumeric, at
        most 40 characters: it becomes part of a Secret Manager name and a
        Kubernetes annotation, so the platform checks it on the way in and says so
        if it does not fit. The account id will be{' '}
        <code>
          {owner}:{label.trim() || 'label'}
        </code>
        . <strong>Re-using an existing label replaces that account&rsquo;s
        credential</strong> rather than creating a second one.
      </p>

      <label className="t-label" htmlFor="acct-lend">
        lend to (optional)
      </label>
      <input
        id="acct-lend"
        className="mono acct-wide"
        value={lend}
        spellCheck={false}
        autoComplete="off"
        placeholder="tenant ids, comma separated; empty means this account serves only you"
        onChange={(e) => setLend(e.target.value)}
      />
      <p className="muted small">
        Isolation is the default. Naming a tenant here lets that tenant&rsquo;s
        pods mount this account&rsquo;s access token, which is a narrowing of
        invariant 9 rather than a hole in it. It is a decision with a name on it,
        and it can be changed per account afterwards.
      </p>

      <label className="t-label" htmlFor="acct-cred">
        subscription credential (pasted verbatim)
      </label>
      <textarea
        id="acct-cred"
        className="mono"
        rows={5}
        value={credential}
        spellCheck={false}
        autoComplete="off"
        placeholder={'{"claudeAiOauth":{"accessToken":"...","refreshToken":"...","expiresAt":0}}'}
        onChange={(e) => {
          setCredential(e.target.value)
          setState({ kind: 'idle' })
        }}
      />
      <p className="muted small">
        Paste the whole keychain item, wrapper and all, unedited. The platform
        parses it before anything is written, so a value with no refresh token is
        refused here and now rather than discovered at its first expiry. The steps
        for obtaining it are below.
      </p>

      <p className="acct-buttons">
        <button
          type="submit"
          disabled={state.kind === 'sending' || label.trim() === '' || credential.trim() === ''}
        >
          {state.kind === 'sending' ? 'registering…' : 'Register this account'}
        </button>
      </p>

      {state.kind === 'done' && (
        <div className="state acct-ok" role="status">
          <h3>Registered {state.label}</h3>
          <p>
            Two secrets were written: the pair, which only quota-broker reads, and
            the access token, which is what a pod mounts into{' '}
            <code>CLAUDE_CODE_OAUTH_TOKEN</code>. The account appears above with
            no reading yet &mdash; that is <em>unmeasured</em>, not idle, and the
            first worker to run on it will report one. Press{' '}
            <em>Refresh this account</em> in its row to confirm the credential
            exchanges before you rely on it.
          </p>
        </div>
      )}

      {state.kind === 'failed' && (
        <>
          <FailedPanel error={state.error} onRetry={() => undefined} />
          <p className="warn-text">
            The credential box was cleared the moment this was sent, so nothing is
            sitting in this page. Copy it again to retry.{' '}
            {state.error.kind === 'invalid' && (
              <>
                A 422 means the platform read the value and would not accept it,
                most often because it is a <code>claude setup-token</code> value,
                which has no refresh token. See below for why that is refused
                rather than stored.
              </>
            )}
          </p>
        </>
      )}
    </form>
  )
}

// ---------------------------------------------------------------------------
// Instructions
// ---------------------------------------------------------------------------

/**
 * Real operator instructions, in the page, because this is the screen someone
 * opens at the moment they need them and a wiki link is a second place to be
 * wrong. Every command below is one that exists.
 */
function Instructions() {
  return (
    <section className="section panel acct-instructions">
      <h2>How to run this pool</h2>

      <h3>1 &middot; Getting the credential</h3>
      <p>
        The credential is the item Claude Code itself keeps after you sign in, on
        a machine you control. Take it from there rather than minting a new one.
      </p>
      <ul>
        <li>
          <strong>macOS.</strong> It is a login-keychain item called{' '}
          <code>Claude Code-credentials</code>. Read it with{' '}
          <code>
            security find-generic-password -s &quot;Claude Code-credentials&quot;
            -w
          </code>
          , or open Keychain Access, find that item and use <em>Show password</em>.
        </li>
        <li>
          <strong>Linux.</strong> The same JSON is at{' '}
          <code>~/.claude/.credentials.json</code>.
        </li>
        <li>
          Paste <strong>the whole value</strong>, including the{' '}
          <code>{'{"claudeAiOauth": ...}'}</code> wrapper. The platform unwraps
          it. Do not reformat it, pull out single fields, or strip the wrapper: a
          hand-edited value is the commonest cause of a credential that parses on
          your machine and not here.
        </li>
        <li>
          Treat the clipboard accordingly. That value carries a live access token{' '}
          <em>and</em> the refresh token that can mint successors: paste it into
          this form and nowhere else. Not a ticket, not a chat, not a shell whose
          history persists.
        </li>
      </ul>
      <p>
        What happens to it: it is sent once and stored as two secrets.{' '}
        <code>&lt;base&gt;-refresh</code> holds the pair and is read only by
        quota-broker. <code>&lt;base&gt;</code> holds <em>only</em> the access
        token, and is what a tenant&rsquo;s pod mounts into{' '}
        <code>CLAUDE_CODE_OAUTH_TOKEN</code>. That split is the security boundary:
        a compromised pod holds a credential that expires, not one that can mint
        successors forever.
      </p>

      <h3>2 &middot; How refreshing works</h3>
      <ul>
        <li>
          <strong>
            quota-broker is the only thing that may exchange a credential.
          </strong>{' '}
          Refreshing an OAuth credential revokes the access token it replaces, so
          two components doing it concurrently brick the account. swarm-api
          proxies to the broker and never touches Secret Manager for accounts;
          this screen calls those same routes.
        </li>
        <li>
          <strong>The sweep visits every account, including idle ones.</strong> A
          refresh token that is never exchanged eventually dies, so a pool where
          three accounts are busy and two are idle is a pool where two are quietly
          rotting until the day you need them.
        </li>
        <li>
          <strong>An exchange happens inside the last three hours</strong> of the
          access token&rsquo;s life. Before that, the sweep publishes the token it
          already holds if the pod-facing secret is missing it, and otherwise does
          nothing at all. &ldquo;Nothing to do&rdquo; is the healthy answer.
        </li>
        <li>
          <strong>Refresh this account</strong> runs that exact code path now, for
          one account, and tells you which of those things happened. It exists
          because a timer cannot answer &ldquo;did that work?&rdquo;: you would
          otherwise paste a credential and wait an unknown number of minutes to
          find out whether it was any good.
        </li>
      </ul>

      <h3>3 &middot; When an account goes REAUTH_REQUIRED</h3>
      <p>
        It means the refresh token is gone or unreadable. The broker stops trying
        on purpose: retrying every five minutes spends the token endpoint&rsquo;s
        rate limit to learn the same answer. Nothing new is assigned to the
        account, and agents already on it will fail when their token expires.
      </p>
      <ol>
        <li>
          On a machine you control, sign in to Claude Code again: run{' '}
          <code>claude</code> and use <code>/login</code>.
        </li>
        <li>Read the refreshed keychain item exactly as in step 1.</li>
        <li>
          Open that account&rsquo;s row here and paste it into{' '}
          <em>Re-authenticate</em>. <strong>Keep the same label.</strong>{' '}
          Re-registering an existing label replaces the credential and keeps the
          account id, its readings and its lending list; a new label would create
          a second account and leave the broken one in the pool.
        </li>
        <li>
          <strong>Then put the state back.</strong> Re-registering deliberately
          does <em>not</em> change state &mdash; that rule is what stops it
          silently un-pausing an account somebody paused &mdash; so a
          re-authenticated account is still REAUTH_REQUIRED, and still
          unassignable, until it is moved to AVAILABLE. This screen does that as a
          visible second step and tells you if the second step fails.
        </li>
        <li>
          Press <em>Refresh this account</em>. <code>refreshed</code> or{' '}
          <code>still_valid</code> means it is alive. That is the confirmation;
          the state chip alone is not.
        </li>
      </ol>

      <h3>
        4 &middot; Why a <code>claude setup-token</code> value is refused
      </h3>
      <p>
        <code>claude setup-token</code> mints a single long-lived access token
        with <strong>no refresh token beside it</strong>. The platform parses what
        you paste before writing anything, and refuses that shape with a 422
        reading <em>the stored credential has no refresh token</em>.
      </p>
      <p>
        The refusal is the feature. A setup-token cannot be kept alive: nothing
        can exchange it, so when it expires a person has to log in again.
        Accepted here, it would look perfectly healthy &mdash; the account would
        sit at AVAILABLE, agents would be assigned to it, and at its first expiry
        every one of them would fail on an expired token with nothing on any
        screen pointing at the cause. Being refused costs you one error message
        now. Being accepted costs an outage later, at a time nobody chose.
      </p>
      <p>
        The pair Claude Code keeps in the keychain can be exchanged for a new pair
        indefinitely, which is what makes &ldquo;the only manual step is the first
        one&rdquo; true rather than aspirational. So: paste the keychain item, not
        a setup token.
      </p>
    </section>
  )
}

function Legend() {
  return (
    <section className="section legend">
      <h2>Reading this screen</h2>
      <dl>
        <dt>The columns are the ones `cs status` prints, on purpose</dt>
        <dd>
          ACCOUNT, 5H, 7D, CLEARS, STATE &mdash; same order, same five-cell bar,
          same meanings. If you read that on your laptop, this is the same line.
          Every extra fact lives in the row you open, so the table you scan never
          changes shape.
        </dd>
        <dt>
          <span className="acct-tilde">~</span> means projected, not measured
        </dt>
        <dd>
          Either the reading is older than the 30 minutes one is trusted for, or
          the window it describes has already reset. These accounts are also used
          by a person at a laptop, so utilisation moves without the platform
          seeing it. A figure without the mark is a claim that it is current.
        </dd>
        <dt>An em dash is not zero</dt>
        <dd>
          It means nothing has been measured: either no reading has ever arrived
          for the account, or the provider reported no such window. Those cells
          draw <em>no bar at all</em>, because an empty five-cell bar and a
          measured 0% are the same picture.
        </dd>
        <dt>CLEARS is the binding window, not the five-hour</dt>
        <dd>
          An account at 5% on its five-hour and 90% on its weekly is stopped by
          the weekly, and refilling the five-hour does nothing for it. CLEARS
          counts down to whichever window will refuse first.
        </dd>
        <dt>There is no amber band, deliberately</dt>
        <dd>
          Any threshold between &ldquo;fine&rdquo; and &ldquo;getting full&rdquo;
          would be a number invented in the browser. The platform&rsquo;s own
          assign floor lives in quota-broker, and nothing checks that a copy of it
          here still matches. The one treatment that is a fact rather than a
          judgement is a window that is fully spent.
        </dd>
        <dt>Four states, and they are not degrees of one thing</dt>
        <dd>
          <code>AVAILABLE</code> is the only state a <em>new</em> agent may be
          started on. <code>PAUSED</code> means no new work while the agents on it
          keep running. <code>DRAINING</code> means they are being moved off.{' '}
          <code>REAUTH_REQUIRED</code> is a verdict the broker reached, not a
          state to declare &mdash; only a person clears it.
        </dd>
        <dt>&ldquo;Agents on it&rdquo; is advisory</dt>
        <dd>
          The lease is the authoritative record of who holds what. This counter
          exists to make a listing readable, and a disagreement between it and the
          Holders screen is not rounding.
        </dd>
      </dl>
    </section>
  )
}
