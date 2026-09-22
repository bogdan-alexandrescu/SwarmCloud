import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  beginAccountSignIn,
  finishAccountSignIn,
  loadAccountsBoard,
  refreshAccount,
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
  callbackHostOf,
  clearsIn,
  humaniseUntil,
  isProjected,
  needsAHuman,
  neverAssigned,
  pastedCodeHint,
  readingOf,
  unreadableFor,
  type Account,
  type AccountAuthorization,
  type AccountReading,
  type AccountStateName,
  type AccountWindow,
  type RefreshResult,
} from './types'

/**
 * THE ONE PROVIDER, AND THE ONE CREDENTIAL KIND -- stated, never picked.
 *
 * This pool holds Claude subscription credentials: the OAuth pair a sign-in
 * produces, which quota-broker can exchange for a successor indefinitely.
 * There is no API-key path here and no credential-kind selector, because there
 * is nothing to choose between -- and a hardcoded value with no words around
 * it is worse than a picker, since the operator cannot even tell that a
 * decision was made for them. So the form SAYS what it sends.
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
 * A SIGN-IN IS THE SHARPEST CASE OF THAT. It spans a trip to another tab, in
 * another application, and back. What has to survive is not a verdict but a
 * live server-side record -- the `state` identifying the pending sign-in and
 * the URL that reopens the login page. Losing those to a remount makes a person
 * sign in again having done nothing wrong. So sign-ins live here, keyed by what
 * they are for: one for the add form, one per account for signing in again.
 *
 * The genuinely transient things -- a half-typed reason, a dirty lending field,
 * an unsent code -- deliberately do NOT, because they should not survive the
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
  /** Signing in again to an account that already exists, keyed by account id. */
  reauth: Record<string, SignIn>
  /** The add-an-account sign-in. */
  signin: SignIn
}

type Patch = (fn: (p: Persisted) => Persisted) => void

const EMPTY_UI: Persisted = { open: {}, refresh: {}, reauth: {}, signin: { kind: 'idle' } }

/**
 * Capacity -> Accounts. The Claude subscriptions this platform runs agents on.
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
 * button here is a call to a broker route, addressed to swarm-api, which
 * proxies all of them -- the list, the register, the refresh, the lending, the
 * state, and both sign-in routes. That last pair was missing for one deploy and
 * the browser got a 404 from a path nobody had built; `SignInFailure` below is
 * what a 404 or 405 from them still reaches, because a proxy that exists today
 * can be absent in an older deployment and the screen should say which.
 * Nothing in this file reads a secret, exchanges a
 * token, retries a refresh on its own initiative, or renders key material --
 * not even its length. It does not hold the PKCE verifier either: that stays
 * server-side, keyed by `state`, because a verifier the client holds is a PKCE
 * flow that proves nothing.
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
   * with it the refresh verdict, the sign-in in progress and the report of what
   * a sign-in did, all of which are rendered by `children` and all of which are
   * the answer to something somebody just did.
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
       * children, so an empty pool would hide the add form -- the single
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

/**
 * How many documents the store could not turn into an account.
 *
 * `?? 0` because an older API serves neither field, and a page that crashed on
 * a missing count would be a worse failure than the one this reports. Zero is
 * also what the screen says nothing about, so an old deployment reads exactly
 * as it did before rather than claiming a shortfall it cannot measure.
 */
function shortfall(board: AccountsBoard): number {
  return board.page.unreadable_document_count ?? 0
}

function SummaryLine({ board }: { board: AccountsBoard }) {
  const n = board.page.accounts.length
  const scope = board.page.tenant_id
  const missing = shortfall(board)
  const broken = board.page.accounts.filter(needsAHuman)
  // Split, because they are two different jobs for two different people: one is
  // "open the row and sign in", the other is "this is not yours to fix".
  const mine = broken.filter((a) => isOwned(a, scope)).length
  const lent = broken.length - mine
  return (
    <>
      {n} account{n === 1 ? '' : 's'} &middot;{' '}
      {scope === null ? 'every tenant' : `${scope} — owned and lent to it`}
      {/* BEFORE anything derived from the rows, because it is the sentence
          that says how much of the pool the rows are. A count of accounts is
          not a fact when documents were dropped reaching it. */}
      {missing > 0 && (
        <>
          {' '}
          &middot;{' '}
          <strong>
            {missing} document{missing === 1 ? '' : 's'} unreadable, so this list is short
          </strong>
        </>
      )}
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
      <AddAccount board={board} accounts={accounts} ui={ui} patch={patch} reload={reload} />
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
 * and is not theirs to fix: its row shows no sign-in control, no state control
 * and no refresh button, because those routes answer 404 for a tenant that does
 * not own the account. Telling every borrower to "open the row and sign in"
 * sends them to a row that deliberately refuses, so the two cases are separated
 * here and only one of them is an instruction.
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
          Open the row and press <em>Sign in again</em>: one trip to a Claude
          login page, one short code, and this screen puts the state back for you.
        </div>
      )}
      {lent.length > 0 && (
        <div>
          {lent.map((a) => `${a.account_id} (owned by ${a.owner_tenant})`).join(', ')}{' '}
          &mdash; broken the same way, and <strong>not yours to fix</strong>.
          Signing in again, refreshing and changing state stay with the owning
          tenant, so those rows carry no controls and the routes answer{' '}
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
            rather than failing, which costs nothing but also runs nothing. Add
            the first one below: it is a sign-in, and it takes about a minute.
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
                readAt={board.readAt}
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
      <Warnings
        accounts={accounts}
        scope={board.page.tenant_id}
        now={now}
        readAt={board.readAt}
        unreadableDocuments={board.page.unreadable_documents ?? []}
        unreadableDocumentCount={shortfall(board)}
      />
      <p className="provenance">
        {accounts.length} rows returned
        {shortfall(board) > 0 && (
          <>
            {' '}
            of {accounts.length + shortfall(board)} documents &mdash;{' '}
            {shortfall(board)} could not be read
          </>
        )}{' '}
        &middot; 5H and 7D are the provider&rsquo;s own windows, reported by
        workers &middot; a figure marked ~ is projected, not measured
      </p>
    </section>
  )
}

function PoolRows({
  account,
  board,
  now,
  readAt,
  open,
  onToggle,
  ui,
  patch,
  reload,
}: {
  account: Account
  board: AccountsBoard
  now: number
  /** When the board these rows came from was read. See `AccountsBoard.readAt`. */
  readAt: number
  open: boolean
  onToggle: () => void
  ui: Persisted
  patch: Patch
  reload: () => void
}) {
  const tone = accountTone(account.state)
  const five = readingOf(account, FIVE_HOUR)
  const seven = readingOf(account, SEVEN_DAY)
  // THE ROW THAT READS HEALTHIEST AND SERVES NOBODY. `choose()` skips an
  // account this tenant has reported unreadable, so its window, its headroom
  // and its state all describe an account that will not be handed out here --
  // and every one of those cells says so confidently. Marked like a row that
  // cannot serve, because that is what it is.
  const unusable = unreadableFor(account, board.page.tenant_id)

  return (
    <>
      <tr
        className={
          tone === 'bad' || unusable ? 'over' : tone === 'paused' ? 'paused' : undefined
        }
      >
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
        <ClearsCell account={account} now={now} readAt={readAt} />
        <td>
          <span className={`tag acct-state ${tone}`}>{account.state}</span>
          {/* BEFORE the state's own note, and not instead of it. The state is
              still AVAILABLE and that is still true: the account is fine, the
              pool simply will not hand it to THIS tenant while the report
              stands. Two facts, and the one that decides whether work runs
              here is this one. */}
          {unusable && (
            <span
              className="acct-why acct-unusable"
              title={`This tenant reported it could not read this account's secret, and the broker's own assignment rule (accounts.choose) is skipping it for this tenant because of that. It is not skipping it for anyone else. The report is forgotten thirty minutes after it was made, so this clears by itself if the cause was a freshly onboarded account; if it persists, the missing piece is a roles/secretmanager.secretAccessor grant on the secret for this tenant's worker service account.`}
            >
              the pool is skipping this account for {board.page.tenant_id}
            </span>
          )}
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
            confusion this column must never allow. `ctl-em` is the shared
            treatment for an absent measurement, so this cell and every other
            screen's em dash are the same class of thing rather than the same
            character by coincidence. */}
        <span className="acct-pct ctl-em">&mdash;</span>
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
function ClearsCell({
  account,
  now,
  readAt,
}: {
  account: Account
  now: number
  readAt: number
}) {
  const binding = bindingWindow(account)
  if (binding === null) {
    return (
      <td
        className="n acct-unmeasured ctl-em"
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
  // THE INSTANT HAS PASSED AND THIS BOARD DID NOT CALL IT RESET. Two different
  // things put a row here and NOTHING ON THIS SCREEN MEASURES WHICH: the window
  // may have cleared in the time between the read and this render, or the
  // instant and this browser's clock may simply not agree. `reset` was computed
  // server-side when the board was serialised; `now` is this browser's clock
  // at render; there is no second reading of the platform's clock to difference
  // them against. So the tooltip reports the two things that WERE measured --
  // the instant is behind us, and the board is this old -- and names no cause.
  //
  // `~now`, AND THE MARK IS NOT OPTIONAL. "now" here is a countdown that has
  // run out, not a claim that anything cleared, and this row reached this
  // branch precisely because no reading confirms either. The provenance line
  // under the table and the legend both tell the reader that a figure marked ~
  // is projected rather than measured, and `.acct-projected` is grey with an
  // amber `~` so that the distinction survives a printout or a photograph --
  // grey alone is the failure that treatment exists to prevent. Before this
  // branch existed such a row fell through to the one below and was marked;
  // dropping the mark here made the same cell quietly assert a reading.
  const resetsAt = new Date(binding.window.resets_at).getTime()
  if (Number.isFinite(resetsAt) && resetsAt <= now) {
    return (
      <td
        className="n acct-projected"
        title={`The ${windowName} window is the binding one, and the reset instant the platform gave for it is already in the past. The board this row came from was read ${timeAgo(readAt)} and had not marked the window reset. This page does not compare its clock with the platform's, so it cannot tell you whether the window has cleared since that read -- reload, and the platform answers.`}
      >
        <span className="acct-tilde">~</span>
        now
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
  readAt,
  unreadableDocuments,
  unreadableDocumentCount,
}: {
  accounts: Account[]
  scope: string | null
  now: number
  /** When the board was read. The age of a figure is part of the figure. */
  readAt: number
  /** Documents the store could not parse, narrowed to what this caller may see. */
  unreadableDocuments: string[]
  /** How many there were in total. Never smaller than the list above. */
  unreadableDocumentCount: number
}) {
  const lines: string[] = []

  // FIRST, because it is the only line that is about the LIST rather than
  // about a row in it. Every other warning here, and every figure above it,
  // was computed over accounts that were read; this says how many were not.
  if (unreadableDocumentCount > 0) {
    lines.push(
      unreadableDocuments.length > 0
        ? `${unreadableDocumentCount} account document${unreadableDocumentCount === 1 ? '' : 's'} could not be read, so ${unreadableDocumentCount === 1 ? 'it is' : 'they are'} missing from this table and from every count on this screen. ${unreadableDocuments.length === unreadableDocumentCount ? '' : `${unreadableDocuments.length} of them belong${unreadableDocuments.length === 1 ? 's' : ''} to this tenant: `}${unreadableDocuments.join(', ')}. The broker logs the parse error against each document id.`
        : `${unreadableDocumentCount} account document${unreadableDocumentCount === 1 ? '' : 's'} could not be read, so ${unreadableDocumentCount === 1 ? 'it is' : 'they are'} missing from this table and from every count on this screen. None belongs to this tenant, so their ids are not shown here -- the broker logs the parse error against each one.`,
    )
  }

  // EVERY account registered and not one ever handed out. Per-row this is just
  // a new account; across the whole pool it is the one symptom of workers that
  // cannot reach the broker at all -- no QUOTA_BROKER_URL, or a missing
  // run.invoker grant -- which otherwise looks exactly like a quiet week. The
  // broker logs this from its sweep; nothing showed it to a person.
  if (accounts.length > 0 && accounts.every(neverAssigned)) {
    lines.push(
      `No account in this pool has ever been assigned to an agent. An idle pool and a pool nothing can reach look identical on this screen, and this is the shape of the second: check that the scheduler sets QUOTA_BROKER_URL on the jobs it dispatches, and that each tenant's worker service account holds roles/run.invoker on swarm-quota-broker. Until a worker asks, every figure above describes accounts nothing is using.`,
    )
  }

  for (const a of accounts) {
    // BEFORE `needsAHuman`, because this one is invisible without it. A
    // REAUTH_REQUIRED row already carries a red chip and a reason; an account
    // the pool is skipping for this tenant carries AVAILABLE, a full window
    // and no explanation of why nothing runs on it.
    if (unreadableFor(a, scope)) {
      lines.push(
        `${a.account_id} is being skipped for ${scope}: this tenant reported it could not read the account's secret, and accounts.choose() excludes it on that report -- for this tenant only, so its owner and any other borrower are unaffected. Its headroom above is real and is not available here. The report is forgotten thirty minutes after it was made; if it keeps coming back, the missing piece is roles/secretmanager.secretAccessor on that secret for this tenant's worker service account.`,
      )
    }
    if (needsAHuman(a)) {
      // The instruction only goes to the person who can carry it out. A lent
      // row has no sign-in control and no state control by design, so sending a
      // borrower there is sending them to a dead end.
      lines.push(
        isOwned(a, scope)
          ? `${a.account_id} needs a sign-in. Open its row, press Sign in again, and this screen puts its state back afterwards.`
          : `${a.account_id} needs a sign-in, and ${a.owner_tenant} owns it. Signing in again and changing its state belong to that tenant -- those routes answer 404 here -- so its row has no controls and this one is fixed by its owner, not from this screen.`,
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
        // WHAT IS MEASURED HERE AND WHAT IS NOT. Measured: the instant the
        // platform gave for this window is now behind us, and the board that
        // said the window had not reset was read `readAt` ago. NOT measured:
        // any difference between this browser's clock and the platform's --
        // there is no second reading of the platform's clock to difference
        // against, and `reset` was computed when the board was serialised
        // rather than now. This line used to name clock skew as the cause;
        // ordinary elapsed time on a board that only reloads on a mutation
        // produces the same condition with perfectly synchronised clocks, so
        // it now names no cause at all and says what to do instead.
        lines.push(
          `${a.account_id}: the ${binding.key.replace(/_/g, '-')} window's reset instant has passed, and the board this row came from -- read ${timeAgo(readAt)} -- had not marked that window reset. Nothing here compares the two clocks, so this page cannot tell you whether it has cleared since that read: reload, and the platform answers. Until then CLEARS on that row is a countdown that has run out, not a reading.`,
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
        {/* NEVER IS NOT RECENTLY. An account registered and never handed to an
            agent is indistinguishable, everywhere else on this screen, from a
            healthy one nobody happened to need -- and it is also the per-row
            shape of a pool no worker can reach. The broker serves the instant
            precisely so this row can tell the two apart. */}
        <dt>Last assigned</dt>
        <dd>
          {neverAssigned(account) ? (
            <>
              never
              <span className="acct-why">
                no agent has ever been handed this account; on its own that is
                simply a new account, and across the whole pool it is the shape
                of workers that cannot reach the broker
              </span>
            </>
          ) : (
            timeAgo(account.last_assigned_at as string)
          )}
        </dd>
        {/* Only when there is something to say. An empty list here every time
            would train the eye to skip the one row that matters. */}
        {(account.unreadable_by ?? []).length > 0 && (
          <>
            <dt>Reported unreadable by</dt>
            <dd className="mono">
              {account.unreadable_by.join(', ')}
              <span className="acct-why">
                {(account.unreadable_now ?? []).length > 0
                  ? `the pool is skipping this account right now for ${account.unreadable_now.join(', ')} -- for those tenants only, and the report is forgotten thirty minutes after it was made`
                  : 'every one of those reports has aged out, so the pool is skipping this account for nobody; the record is kept because a report that keeps coming back is a missing secretAccessor grant rather than an onboarding delay'}
              </span>
            </dd>
          </>
        )}
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
        refreshing and signing in again stay with its owner. Those routes answer{' '}
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
        className="sr-by acct-unmeasured ctl-em"
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
 *
 * FOUR OF THEM END IN THE SAME INSTRUCTION -- sign in again -- and they still
 * get four different explanations, because the thing that went wrong differs
 * and the next person to read this screen needs to know which. Collapsing them
 * into one sentence is exactly the mistake this codebase keeps paying for.
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
    case 'published_unverified':
      return {
        ok: true,
        heading: 'Published, but nothing confirmed it was needed',
        body: (
          <>
            The stored token was still valid and was written to the secret a pod
            mounts &mdash; without the broker being able to read that secret
            first, so it could not tell whether the write was necessary. The
            credential is fine and pods have the right token. What is wrong is
            the broker&rsquo;s IAM: it needs{' '}
            <code>roles/secretmanager.secretAccessor</code> on this
            account&rsquo;s secret. Until it has that, the usage poller cannot
            read the token either, so this account&rsquo;s headroom stays at
            whatever it was last seen to be.
          </>
        ),
      }
    case 'unverified_skipped':
      return {
        ok: false,
        heading: 'Nothing could confirm what the secret holds, so nothing was written',
        body: (
          <>
            The stored token is still valid, but the broker could read neither the
            secret a pod mounts nor its own record of what it last published
            there. With no evidence in either direction it declined to write &mdash;
            deliberately, because writing on a failed read is what added ~1,700
            identical versions to every account secret in September. The
            credential itself is fine. Two things to check: the broker needs{' '}
            <code>roles/secretmanager.secretAccessor</code> on this
            account&rsquo;s secret, and its Firestore{' '}
            <code>credential_publications</code> collection has to be reachable.
            Until one of them works, this account&rsquo;s pod-facing secret is
            only rewritten when the credential actually rotates.
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
            REAUTH_REQUIRED. Press <em>Sign in again</em> below &mdash; it is the
            same sign-in that added the account, and this screen puts the state
            back afterwards as a visible second step.
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
            is nothing to exchange &mdash; a different fault from a refresh token
            the endpoint rejected. The account has been moved to REAUTH_REQUIRED.
            Press <em>Sign in again</em> below: a fresh sign-in writes the shape
            the broker produces, which is the shape it can read.
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
            nothing to exchange and nothing was rejected. That is the shape a
            lone long-lived access token has &mdash; one token, nothing beside it
            &mdash; and it cannot be kept alive by anything. Press{' '}
            <em>Sign in again</em> below: a sign-in always yields a pair.
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
  const dirty = willSave.join(' ') !== [...account.lend_to].join(' ')

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
// The sign-in -- the only way a credential gets into this pool
// ---------------------------------------------------------------------------
//
// WHAT USED TO BE HERE, and why it is not coming back. A textarea, and
// instructions to run `security find-generic-password -s "Claude
// Code-credentials" -w`, copy the JSON it printed and paste it "verbatim, the
// wrapper included". It was a bad experience and it was also fragile: the
// commonest failure was a hand-edited value that parsed on the operator's
// machine and not here, reported as a 422 about a missing refresh token, which
// named the symptom rather than the cause. There is no paste box left in this
// file and no fallback to one -- a keychain field kept "just in case" is how
// the flow that was removed comes back.
//
// TWO STEPS, BECAUSE THE PROVIDER'S OAUTH CLIENT ALLOWS EXACTLY ONE REDIRECT
// TARGET: its own callback page, which DISPLAYS a code. A third-party
// application cannot register `https://swarm.saga.xyz/callback`, so the browser
// cannot come back here and the code on that page is what closes the loop. The
// screen says that out loud rather than leaving the paste looking like an
// oversight somebody will try to "fix" later.

/**
 * One sign-in, as the five things that can be true of it.
 *
 * `open` IS THE STATE THAT MATTERS. It holds a record that exists on the
 * SERVER -- a pending sign-in keyed by `state`, holding the PKCE verifier, the
 * label and the lending list -- so it is not a UI mode, it is a claim about
 * something real that will expire. That is why it carries `startedAt`: the
 * deadline is measured from the moment the server issued it, not from a render.
 *
 * `open` also survives a FAILED exchange, on purpose: the broker deletes the
 * pending record only once an account exists, so a mistyped or stale code costs
 * one paste rather than the whole sign-in.
 */
type SignIn =
  | { kind: 'idle' }
  | { kind: 'starting' }
  | { kind: 'start_failed'; error: ApiError }
  | {
      kind: 'open'
      label: string
      lendTo: string[]
      auth: AccountAuthorization
      /** When the server issued it. The deadline is measured from here. */
      startedAt: number
      /** What happened when the sign-in page was asked to open, if it was. */
      opened: 'not_yet' | 'opened' | 'blocked'
      sending: boolean
      /**
       * The last exchange refusal, if there was one.
       *
       * The sign-in is STILL `open` as far as this page is concerned, which is
       * not the same as the platform still holding it: two of the four
       * refusals mean the pending record is gone. `exchangeRefusal` tells them
       * apart, and it is what decides whether the paste field stays live.
       */
      failed: ApiError | null
    }
  | {
      kind: 'done'
      account: Account
      expiresAt: string | null
      /** Only for a sign-in that had to put a state back. Null otherwise. */
      restore: { ok: true } | { ok: false; error: ApiError | null } | null
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

/**
 * The one failure this screen has to NAME rather than describe.
 *
 * The sign-in routes are ones this page is certain it asked for at the right
 * path, so a 404 or a 405 from them is not "you typed something wrong" and it
 * is not "the platform is down" -- it is an API that does not serve the sign-in
 * yet. A 405 in particular is what an API whose only route at that path is a
 * DELETE answers, and bare "HTTP 405" sends an operator looking at their own
 * request. `classify` in fetch.ts cannot know that, because it is per-status
 * and this is per-route; so it is decided here, where the route is known.
 */
function isRouteMissing(e: ApiError): boolean {
  return e.httpStatus === 404 || e.httpStatus === 405
}

/**
 * WHICH OF THE EXCHANGE'S REFUSALS THIS IS. The test is not "did it fail" but
 * "WHAT IS TRUE NOW", and the answers are opposites of each other.
 *
 * `sign_in_gone` used to hold two of these at once, and the single paragraph
 * it printed asserted "Nothing was created and nothing was changed" for both.
 * That sentence is right for one of them and false for the other in the way
 * that costs most: `finish_account_authorization` in quota_broker/main.py
 * deletes the pending record on exactly two paths -- for age BEFORE anything
 * is redeemed, and immediately AFTER an account has been written by
 * `_provision_and_register`. An absent record is
 * therefore, most often, a sign-in that SUCCEEDED -- so the reader is told an
 * account does not exist while it sits in the pool, and on a re-auth while the
 * credential they were replacing has already been replaced, which revokes the
 * one it replaced. The reader's next action differs completely, and that is
 * the only reason to tell them anything at all.
 *
 * The buckets, and what each one leaves behind:
 *
 *  - `route_missing` -- 404/405. The request never reached a handler.
 *  - `accepted_unnamed` -- the platform answered 201 and named no account.
 *    Synthesised in `finishAccountSignIn`; see `httpStatus === 201` below.
 *    The sign-in is OVER and an account has probably been created.
 *  - `no_answer` -- `fetch` itself failed, so `httpStatus` is null. NOBODY
 *    REFUSED ANYTHING: the exchange may have been completed in full and only
 *    the answer lost. Unknowable from here, and said so.
 *  - `sign_in_expired` -- "took too long". The record was deleted for age
 *    before the token endpoint was called, so NOTHING WAS CREATED. Terminal.
 *  - `sign_in_absent` -- "expired or was already completed". The record is not
 *    there; the commonest reason is that it was redeemed. Terminal, and the
 *    opposite of the one above in consequence.
 *  - `other_sign_in` -- refused BEFORE the record is read, so it is untouched.
 *  - `code_refused` -- the token endpoint rejected the code, before the delete
 *    that only runs once an account exists. Still open.
 *
 * WHAT A FIXTURE CAN REACH, exactly: `fixtureFinishSignIn` produces
 * `other_sign_in` (paste a code whose `#state` is another's), `sign_in_absent`
 * (NOT reachable from a fixture: both fixture deletes close the panel first,
 * so there is no second paste -- kept in the type because the BROKER raises it
 * whenever a pending sign-in is gone, which is the ordinary case for a code
 * redeemed in another tab), `code_refused` (a code under six characters),
 * `sign_in_expired` (paste `expired`) and `accepted_unnamed` (paste
 * `unnamed`). `route_missing` and `no_answer` are transport failures a fixture
 * does not have; they are the two this list cannot promise, and this sentence
 * says which rather than claiming all of them.
 *
 * MATCHED ON THE BROKER'S OWN WORDING, AND POSITIVELY. Substring matching is
 * fragile, so an unrecognised message falls through to `unknown`, which
 * promises nothing -- rather than being forced into the bucket that happens to
 * be commonest, which is how "paste again" ended up printed over a sign-in
 * that had already ended. `classify` in fetch.ts makes the same trade for the
 * three unrelated 403s.
 */
type Refusal =
  | 'route_missing'
  | 'accepted_unnamed'
  | 'no_answer'
  | 'sign_in_expired'
  | 'sign_in_absent'
  | 'other_sign_in'
  | 'code_refused'
  | 'unknown'

function exchangeRefusal(e: ApiError): Refusal {
  if (isRouteMissing(e)) return 'route_missing'
  // AN EXACT MARKER RATHER THAN A SENTENCE. `classify` in fetch.ts only builds
  // an ApiError for a response that was NOT ok, so no refusal can carry a 201;
  // the usual source is `finishAccountSignIn`'s own synthesis for a 201 whose
  // body named no account. Matching its prose instead would put the page one
  // copy edit away from telling somebody to paste a code that was redeemed.
  //
  // IT IS NOT THE ONLY SOURCE, and an earlier version of this comment said it
  // was. `write()` in fetch.ts has a second path that never reaches `classify`:
  // an `ok` response whose body is not JSON becomes a synthesised
  // `session_expired` carrying the real status. This route answers 201, so an
  // intermediary answering HTML in front of swarm-api lands here with a
  // message that says the change was NOT saved -- under a heading saying the
  // platform accepted it. The message is therefore checked first, below, and
  // only a 201 that does not carry that signature is treated as unnamed.
  if (e.httpStatus === 201 && !e.message.toLowerCase().includes('not saved')) {
    return 'accepted_unnamed'
  }
  // `httpStatus === null` is `fetch` rejecting -- DNS, TLS, offline, a proxy
  // that timed out. Tested BEFORE the message, because the message is then the
  // browser's own and nothing in it is the platform speaking.
  if (e.httpStatus === null) return 'no_answer'
  const m = e.message.toLowerCase()
  if (m.includes('took too long')) return 'sign_in_expired'
  if (m.includes('already completed')) return 'sign_in_absent'
  if (m.includes('different sign-in')) return 'other_sign_in'
  if (m.includes('code was not accepted')) return 'code_refused'
  return 'unknown'
}

/**
 * Whether this refusal ended the sign-in for good.
 *
 * ONE DEFINITION, because three controls turn on it -- the paste field, the
 * button that reopens the authorize URL, and the link that hands the same URL
 * out as text. Two of them used to be gated here and the third was not, so an
 * operator could copy a dead link, sign in with it somewhere else and come
 * back to a disabled field.
 *
 * `no_answer` is deliberately NOT here: nothing measured says the record is
 * gone, and disabling a control on a guess is the same fault pointed the other
 * way.
 */
function signInIsOver(refusal: Refusal | null): boolean {
  return (
    refusal === 'sign_in_expired' ||
    refusal === 'sign_in_absent' ||
    refusal === 'accepted_unnamed'
  )
}

/**
 * What to do about a refused exchange, which is a different sentence for each
 * refusal because what is TRUE after each of them is different.
 *
 * TWO RULES, and they are the same defect from two sides. It may never tell
 * somebody to paste again into a sign-in the platform has already finished
 * with; and it may never contradict the platform's own message, which
 * `SignInFailure` has just rendered immediately above it. "Pasting again costs
 * nothing" printed under "do not sign in again yet" is both at once, and it
 * shipped once already.
 */
function ExchangeAdvice({
  refusal,
  mode,
  onStartOver,
  reload,
}: {
  refusal: Refusal
  /** Which control opened this sign-in, so the advice names the right one. */
  mode: 'add' | 'reauth'
  onStartOver: () => void
  /**
   * Re-reads the pool. OFFERED RATHER THAN DESCRIBED: three of these refusals
   * end with "look at the pool before you do anything else", and a sentence
   * telling somebody to find a control is worse than the control.
   *
   * WHAT IT COSTS, stated because an earlier version of this comment claimed
   * the opposite. The sign-in panel itself survives -- `ui` lives in
   * `AccountsScreen`, above the remount -- but `reload` bumps the nonce used
   * as `key` on `<Screen>`, so the subtree below it unmounts and AddAccount's
   * `label` and `lend` fields, which are plain local state, are cleared. Three
   * of the refusals that offer this button advise checking exactly those two
   * fields. So the copy beside it must tell the reader to look BEFORE pressing
   * it, and must not promise the form is still there afterwards.
   */
  reload: () => void
}) {
  // A missing route is a deployment fault and `SignInFailure` has just said so
  // at length. Adding "paste again" under it would be advice about a request
  // that never reached a handler.
  if (refusal === 'route_missing') return null

  if (refusal === 'accepted_unnamed') {
    return (
      <>
        <p className="warn-text">
          <strong>Do not sign in again until you have looked.</strong> The
          message above is the platform saying yes: 201 is the answer it gives
          once the account has been written, and it deletes the pending record
          on that same path. What is missing from the answer is the account's
          name, not the account.
        </p>
        <p className="muted small">
          {mode === 'add' ? (
            <>
              Reload the pool and look for the label you typed. If it is there,
              this worked and there is nothing left to do &mdash; open its row
              to see its state. If it is not, <em>start over</em> asks for a
              fresh sign-in; the platform kept no copy of the label or the
              lending list, so read both off the form before you press
                anything here — reloading the pool clears them.
            </>
          ) : (
            <>
              Reload the pool and open this account&rsquo;s row.{' '}
              <strong>
                Treat the credential as already replaced until you have looked
              </strong>
              : a replacement revokes the one before it, so this is not a
              failure to repeat blindly. A replacement keeps the id, the
              readings and the lending list, so the row will look much as it
              did. If it is still asking for a sign-in, <em>start over</em>{' '}
              begins a fresh one.
            </>
          )}
        </p>
        <p className="acct-buttons">
          <button type="button" onClick={reload}>
            reload the pool
          </button>
          <button type="button" onClick={onStartOver}>
            start over
          </button>
        </p>
      </>
    )
  }

  if (refusal === 'no_answer') {
    return (
      <>
        <p className="warn-text">
          <strong>Nothing refused this &mdash; nothing answered it.</strong> The
          request did not complete, so this page never learned what the platform
          did with it. It may have been redeemed in full and only the answer
          lost; it may never have arrived. Nothing here measures which, so
          nothing here will tell you which.
        </p>
        <p className="muted small">
          Reload the pool and look{' '}
          {mode === 'add' ? 'for the label you typed' : "at this account's row"}{' '}
          first, because that is measurable and this is not. If{' '}
          {mode === 'add' ? 'it is not there' : 'it still needs a sign-in'}, the
          paste field above is still live and the code on the callback page is
          still the right one to paste &mdash; if the exchange did go through,
          pasting it says so rather than doing it twice.
        </p>
        <p className="acct-buttons">
          <button type="button" onClick={reload}>
            reload the pool
          </button>
        </p>
      </>
    )
  }

  if (refusal === 'sign_in_expired') {
    return (
      <>
        <p className="warn-text">
          <strong>
            This sign-in was held past its deadline, and pasting cannot reopen
            it.
          </strong>{' '}
          The platform deletes a pending record once it is too old, and it does
          that <em>before</em> it tries to redeem anything &mdash; which is why
          this one can be blunt: <strong>nothing was created and nothing was
          changed</strong>. What is missing is the record the code is checked
          against, not the code, so a fresh code from the same page is refused
          in exactly the same way.
        </p>
        <p className="muted small">
          <em>start over</em> above asks the platform for a new sign-in; the
          button below does the same thing.{' '}
          {mode === 'add'
            ? 'The platform kept no copy of the label or the lending list, so read both off the form before you press anything here \u2014 reloading the pool clears them.'
            : 'The account in the row is untouched — its credential and its state are exactly as they were before this sign-in was started.'}
        </p>
        <p className="acct-buttons">
          <button type="button" onClick={onStartOver}>
            start over
          </button>
        </p>
      </>
    )
  }

  if (refusal === 'sign_in_absent') {
    return (
      <>
        <p className="warn-text">
          <strong>This sign-in is over, and it may well have worked.</strong>{' '}
          The platform is not holding a record for it any more, and the ordinary
          reason a record is missing is that it was <em>redeemed</em>: the
          delete that does not raise is the one immediately after an account has
          been written. Pasting cannot reopen it &mdash; a code is now checked
          against nothing &mdash; and this page cannot tell you from here
          whether it completed.{' '}
          <strong>So this is not &ldquo;nothing happened&rdquo;</strong>, which
          is what this panel used to say.
        </p>
        <p className="muted small">
          {mode === 'add' ? (
            <>
              Reload the pool and look for the label before you start another
              sign-in. If it is there, this worked. If it is not, <em>start
              over</em> asks for a fresh one &mdash; the platform kept no copy
              of the label or the lending list, so read both off the form
              before you press anything here &mdash; reloading the pool clears
              them.
            </>
          ) : (
            <>
              Reload the pool and open this account&rsquo;s row.{' '}
              <strong>
                Treat the credential as possibly already replaced
              </strong>
              : a replacement revokes the one before it, so a row that has
              stopped asking for a sign-in was fixed rather than left alone. If
              it still needs one, <em>start over</em> begins a fresh sign-in.
            </>
          )}
        </p>
        <p className="acct-buttons">
          <button type="button" onClick={reload}>
            reload the pool
          </button>
          <button type="button" onClick={onStartOver}>
            start over
          </button>
        </p>
      </>
    )
  }

  if (refusal === 'other_sign_in') {
    return (
      <p className="muted small">
        <strong>That code belongs to a different sign-in.</strong> The platform
        compares the part after the <code>#</code> with the sign-in this page
        started, and they did not match &mdash; the usual cause is a second tab,
        opened from its own{' '}
        <em>{mode === 'add' ? 'Add account' : 'Sign in again'}</em>, showing its
        own code. That
        check runs before the platform reads anything, so{' '}
        <strong>this sign-in is untouched and still open</strong>: paste the
        code from the page <em>this</em> panel opened.
      </p>
    )
  }

  if (refusal === 'code_refused') {
    return (
      <p className="muted small">
        <strong>The code was refused, and this sign-in is still open.</strong>{' '}
        The platform deletes the sign-in only once an account exists, so a code
        that was mistyped, already spent or too old costs one paste and nothing
        else. Paste again above, or press <em>Open the sign-in page again</em>{' '}
        for a fresh one &mdash; the label and the lending list are held by this
        sign-in, so nothing needs re-entering.
      </p>
    )
  }

  return (
    <>
      <p className="muted small">
        <strong>
          The platform refused the exchange, and what it said above is all it
          said.
        </strong>{' '}
        This page recognises the refusals that end a sign-in by their wording,
        and this is none of them. So it does not know whether the record is
        still there, and it does not know whether an account was written before
        whatever went wrong &mdash; and it will not tell you either way from a
        message it did not recognise.
      </p>
      <p className="muted small">
        Reload the pool and look{' '}
        {mode === 'add' ? 'for the label you typed' : "at this account's row"}{' '}
        first, because that is measurable and this is not. The paste field above
        is still live if you need it, and <em>start over</em> begins a fresh
        sign-in.
      </p>
      <p className="acct-buttons">
        <button type="button" onClick={reload}>
          reload the pool
        </button>
      </p>
    </>
  )
}

function SignInFailure({
  error,
  what,
  onRetry,
  heading,
  tone = 'failed',
}: {
  error: ApiError
  what: string
  /**
   * A retry that actually re-sends this request, or nothing.
   *
   * OMITTED FOR THE EXCHANGE, deliberately: the code that failed was taken out
   * of the field before the request left, so there is nothing left to re-send
   * and a retry would post an empty paste. Every failure panel on this screen
   * used to render `FailedPanel`'s "Try again" wired to `() => undefined` --
   * the most prominent control on the panel, producing no request, no spinner
   * and no message, which is indistinguishable from a platform that swallowed
   * the retry.
   */
  onRetry?: () => void
  /**
   * Overrides `errorHeading`, for the one failure that is not one.
   *
   * A 201 whose body named no account arrives here as a `server_error`, whose
   * heading is "The API failed on this request" -- printed directly above a
   * message that begins "The platform accepted the sign-in". The heading and
   * the tone are the loudest things on the panel, so leaving them to the kind
   * would contradict the body text in the largest type on the screen.
   */
  heading?: string
  /** `partial` for a request that did something, `failed` for one that did not. */
  tone?: 'failed' | 'partial'
}) {
  if (isRouteMissing(error)) {
    return (
      <div className="ctl-empty is-partial" role="status">
        <h3>This deployment&rsquo;s API does not serve the sign-in yet</h3>
        <p>
          The API answered <strong>HTTP {error.httpStatus}</strong> for{' '}
          <code>{what}</code> itself. That is what an API which has not been
          given the sign-in route answers &mdash; not an answer about your
          request, and not one about your account. Both steps exist in this
          repository &mdash; quota-broker serves them and swarm-api proxies
          them &mdash; so the API that answered this is not the one this page
          was built against.
        </p>
        <p>
          Nothing was created and nothing was changed. The pool above loaded, so
          your sign-in to this platform is fine and the account routes are
          reachable &mdash; it is this one route that is missing.
        </p>
        <span className="ctl-empty-foot">
          {error.code ? `${error.code} · ` : ''}
          {error.message}
        </span>
      </div>
    )
  }
  if (onRetry) return <FailedPanel error={error} onRetry={onRetry} />
  // NO `FailedPanel` WITHOUT A RETRY. It renders "Try again" for every error
  // kind but four, so the only way to show a failure with no retry is to show
  // it here. The route and the status are printed because this failure is
  // about one request rather than about the screen, and the caller renders the
  // controls that DO something directly under it.
  return (
    <div className={`state ${tone}`} role="status">
      <h3>{heading ?? errorHeading(error)}</h3>
      <p>{error.message}</p>
      <p className="checked-at">
        {what}
        {error.httpStatus !== null ? ` · HTTP ${error.httpStatus}` : ''}
        {error.code ? ` · ${error.code}` : ''}
      </p>
    </div>
  )
}

/**
 * How long the platform says it will hold this sign-in, counted down.
 *
 * TWO CLOCKS, AND THEY ARE NOT THE SAME ONE. The platform holds its pending
 * record for `expires_in_seconds`, which it reported, so that one can be
 * counted down honestly. The CODE on the callback page is the provider's and
 * expires on its own schedule, which this platform does not know and this
 * screen therefore puts no number on. One measured figure covering both would
 * be it lending its credibility to a guess.
 */
function Deadline({ auth, startedAt }: { auth: AccountAuthorization; startedAt: number }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  if (auth.expires_in_seconds === null) {
    return (
      <p className="muted small">
        The platform did not say how long it holds this sign-in, so there is no
        countdown here rather than a plausible one. Finish it promptly:{' '}
        <strong>the code is single-use and expires quickly</strong>.
      </p>
    )
  }
  const left = startedAt + auth.expires_in_seconds * 1000 - now
  if (left <= 0) {
    return (
      <p className="warn-text">
        The platform said it would hold this sign-in for{' '}
        {humaniseUntil(auth.expires_in_seconds * 1000)}, and that has passed on
        this browser&rsquo;s clock. A code pasted now will most likely be refused.
        The field is still live because the platform owns the clock and this one
        may be ahead of it &mdash; and being refused costs nothing.
      </p>
    )
  }
  return (
    <p className="muted small">
      This platform holds the sign-in for another{' '}
      <strong>{humaniseUntil(left)}</strong>. The code itself is{' '}
      <strong>single-use and expires sooner than that</strong>, on
      Anthropic&rsquo;s schedule rather than ours &mdash; so paste it when you
      see it rather than leaving the tab open.
    </p>
  )
}

/**
 * The warning that stops somebody adding the same account twice.
 *
 * WHICH ORGANISATION YOU SIGN IN AS COMES FROM THE COOKIE JAR, and nothing in
 * the request overrides it: there is no account hint this platform could send
 * that would change it. A private window SHARES that jar, so it is not enough
 * -- it signs you in as the same organisation again and you get a second row
 * holding the same subscription, with nothing on any screen explaining why the
 * pool did not really grow.
 *
 * It is shown BEFORE the sign-in page is opened, because once it has been
 * opened in the wrong browser the advice is useless.
 */
function DifferentBrowserNote({ mode }: { mode: 'add' | 'reauth' }) {
  return (
    // `warn`, not `bad`: nothing has failed, and nothing will fail loudly
    // either -- that is the problem. Signing in as the wrong organisation
    // succeeds, so this has to be read BEFORE the button rather than diagnosed
    // after it. `role="note"` rather than `status` because it is static text
    // that was always here, not the result of something the person just did.
    <div className="banner warn" role="note">
      <strong>A different account needs a different browser application</strong>
      <div>
        {mode === 'add' ? (
          <>
            Which organisation you sign in as comes from the cookies this browser
            already holds, and nothing in this request overrides it.{' '}
            <strong>A private window is not enough</strong> &mdash; it shares the
            same cookie jar, so it signs you in as the same organisation and you
            end up with a second row holding an account you already had. To add a
            genuinely different account, copy the link below and open it in a{' '}
            <em>different browser application</em> &mdash; Chrome beside Safari
            beside Firefox &mdash; or sign out of Claude in this one first.
          </>
        ) : (
          <>
            This replaces the credential behind this account with whichever
            organisation <em>this browser</em> is currently signed in as. That
            comes from the cookie jar and nothing in the request overrides it,
            and <strong>a private window shares that jar</strong> rather than
            clearing it. If this browser is signed in as a different organisation
            than the one this account is for, copy the link below and open it in
            a different browser application instead &mdash; otherwise this label
            quietly starts pointing at the wrong subscription.
          </>
        )}
      </div>
    </div>
  )
}

/**
 * Open the sign-in page, and know whether it opened.
 *
 * SYNCHRONOUS, INSIDE THE CLICK. That is why the URL is fetched in an earlier
 * step and opened in this one rather than both on one press: a `window.open`
 * in a promise continuation is the pattern popup blockers exist to stop, and a
 * sign-in that silently does not open is indistinguishable from a platform that
 * is broken.
 *
 * `noopener` is deliberately NOT in the feature string, because with it
 * `window.open` returns null unconditionally and the one thing worth knowing --
 * whether a tab actually opened -- is lost. The opener is severed immediately
 * instead. The trade is sound: the tab is being pointed at the provider's own
 * sign-in page, so even in the window between the two there is nothing
 * untrusted on the other end of the reference.
 */
function openSignInPage(url: string): 'opened' | 'blocked' {
  const w = window.open(url, '_blank')
  if (!w) return 'blocked'
  try {
    w.opener = null
  } catch {
    // A cross-origin refusal. The tab is open, which is what was asked for.
  }
  return 'opened'
}

/**
 * The link, always visible and always copyable.
 *
 * NOT A CONVENIENCE. Adding a second account requires opening this URL in a
 * different browser application, and the only way to do that is to have the
 * URL as text. So it is rendered whether or not the button worked, and it is
 * rendered before anyone has had a chance to press the wrong one.
 *
 * ONLY WHILE THE SIGN-IN IS LIVE, though, and the caller enforces that. This
 * is a control that hands out the same `authorize_url` the reopen button does,
 * so a refusal that disables that button has to withdraw this too -- and the
 * clipboard-failure sentence below promises that nothing is wrong with the
 * sign-in, which is only true where `signInIsOver` is false.
 */
function SignInLink({ url }: { url: string }) {
  const [copied, setCopied] = useState<'no' | 'yes' | 'unavailable'>('no')

  const copy = async () => {
    // `navigator.clipboard` is undefined outside a secure context and can
    // reject when the document is not focused. Either way the URL is on screen
    // and selectable, so the failure is reported rather than swallowed -- a
    // button that says "copied" when nothing was copied is the small version
    // of the bug this whole app is about.
    try {
      await navigator.clipboard.writeText(url)
      setCopied('yes')
    } catch {
      setCopied('unavailable')
    }
  }

  return (
    <>
      <div className="acct-buttons">
        <button type="button" onClick={() => void copy()}>
          {copied === 'yes' ? 'link copied' : 'copy the sign-in link'}
        </button>
      </div>
      {copied === 'unavailable' && (
        <p className="muted small">
          This browser would not give the page access to the clipboard, which it
          refuses outside a secure context and when the window is not focused.
          Select the link below and copy it by hand &mdash; it is the same value,
          and nothing is wrong with the sign-in.
        </p>
      )}
      <p className="acct-fixed" style={{ wordBreak: 'break-all' }}>
        {url}
      </p>
    </>
  )
}

/**
 * Steps two and three: open the page, then paste what it shows.
 *
 * Shared by the add form and by a row's sign-in-again, because they are the
 * same two calls with the same failure modes; only the label and the lending
 * list differ, and both were decided before this component is reached. A second
 * copy of this would drift on the thing least likely to be noticed -- which of
 * the refusals leaves the sign-in open.
 */
function SignInSteps({
  at,
  mode,
  set,
  onExchanged,
  reload,
}: {
  at: Extract<SignIn, { kind: 'open' }>
  mode: 'add' | 'reauth'
  set: (next: SignIn) => void
  /** Called with the account the platform registered. The caller owns what
      happens next -- which, for a broken account, is putting its state back. */
  onExchanged: (account: Account, expiresAt: string | null) => void | Promise<void>
  /** Re-reads the pool, for the advice that ends in "go and look". */
  reload: () => void
}) {
  const [code, setCode] = useState('')
  const host = callbackHostOf(at.auth.authorize_url)
  const hint = pastedCodeHint(code)
  // WHAT THE LAST REFUSAL WAS, and whether it ended the sign-in. `dead` gates
  // the THREE controls that cannot work any more, all of which are the same
  // dead `state` wearing different clothes: the paste field, because the record
  // its code would be checked against is gone; `Open the sign-in page again`,
  // because it reopens THIS `authorize_url` carrying THAT `state`; and the
  // link, which hands that same URL out as text for another browser.
  //
  // THE LINK WAS THE ONE THAT WAS MISSED. Gating two of the three left an
  // operator free to copy it, sign in somewhere else, come back with a code and
  // find the field disabled -- a dead end the page invited them into, which is
  // worse than the unbounded loop gating was added to stop.
  const refusal = at.failed ? exchangeRefusal(at.failed) : null
  const dead = signInIsOver(refusal)
  // ONE WORDING FOR WHY, printed by the reopen button's tooltip and by the
  // paragraph that replaces the link -- the two places with room for a
  // sentence. The paste field's placeholder is a few words instead, because a
  // placeholder is; it is the only other statement of this, and it says the
  // same thing in them.
  const deadWhy =
    refusal === 'accepted_unnamed'
      ? 'The platform has already accepted this sign-in, so it is no longer holding a record for it. Look for the account in the pool before signing in again.'
      : 'The platform is no longer holding this sign-in, so this page would hand you a code nothing here can redeem. Start over instead.'

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (code.trim() === '') return
    // Out of the field before the request leaves. Not because a single-use code
    // is a credential -- it is not -- but because leaving it there invites a
    // second press that spends a code the platform has already consumed.
    const pasted = code
    setCode('')
    set({ ...at, sending: true, failed: null })
    const res = await finishAccountSignIn({ state: at.auth.state, code: pasted })
    if (res.status !== 'ok') {
      // STILL `open`, AND THE INSTRUCTION UNDER IT IS WHAT MAKES THAT MEAN
      // ANYTHING. Some refusals leave the pending record in place -- a
      // mismatched paste is refused before it is read, a token-endpoint refusal
      // happens before `ref.delete()`, and a request nothing answered may not
      // have arrived at all -- so dropping back to `idle` would throw away a
      // sign-in that is still redeemable. The rest are terminal, `signInIsOver`
      // is where that is decided, and `dead` above is what stops this screen
      // inviting a paste into one of them forever.
      set({ ...at, sending: false, failed: failureOf(res) })
      return
    }
    await onExchanged(res.data.account, res.data.expires_at)
  }

  return (
    <>
      <div className="acct-action">
        <h4>2 &middot; Sign in to Claude</h4>
        <DifferentBrowserNote mode={mode} />
        <p className="muted small">
          This opens Claude&rsquo;s own sign-in page in a new tab. You sign in
          there exactly as you normally would &mdash; this platform never sees
          your password &mdash; and the page you land on afterwards is
          {host ? (
            <>
              {' '}
              <code>{host}</code>, which is
            </>
          ) : (
            <> the one Claude sends you to, which is</>
          )}{' '}
          Anthropic&rsquo;s, not ours.
        </p>
        {/* BOTH DISABLED WHILE A CODE IS IN FLIGHT, and `start over` is the one
            that matters: abandoning a sign-in whose exchange is already on its
            way to the platform would leave an account registered and this page
            claiming nothing happened. The other is disabled for the same
            reason in miniature -- these handlers write from the state this
            render captured, so firing one mid-request would roll the request
            back on screen while it was still running. */}
        <div className="acct-buttons">
          <button
            type="button"
            disabled={at.sending || dead}
            title={dead ? deadWhy : undefined}
            onClick={() => set({ ...at, opened: openSignInPage(at.auth.authorize_url) })}
          >
            {at.opened === 'not_yet' ? 'Sign in to Claude ↗' : 'Open the sign-in page again ↗'}
          </button>
          <button
            type="button"
            disabled={at.sending}
            onClick={() => set({ kind: 'idle' })}
            title="Forget this sign-in on this page and start a new one"
          >
            start over
          </button>
        </div>
        {/* `!dead` for the second half of the sentence: the popup was still
            blocked, but "the sign-in is still open" stopped being true the
            moment the platform said it had finished with it. */}
        {at.opened === 'blocked' && !dead && (
          <p className="warn-text">
            This browser refused to open the tab, which is what a popup blocker
            or an extension does. Nothing failed on the platform and the sign-in
            is still open &mdash; use the link below instead.
          </p>
        )}
        {dead ? (
          /* WITHDRAWN, NOT ANNOTATED. A sentence under a live "copy the
             sign-in link" button saying the link will not work is exactly the
             shape this screen keeps being wrong about: the control still
             works, it just produces a dead end two browsers away from here,
             where nothing explains it. */
          <p className="muted small">
            <strong>The sign-in link has been withdrawn.</strong> It carries
            the same <code>state</code> that the reopen button above is disabled
            for, and it is the one control that can be carried to another
            browser, where none of this is on screen. {deadWhy}
          </p>
        ) : (
          <SignInLink url={at.auth.authorize_url} />
        )}
      </div>

      <form className="acct-action" onSubmit={(e) => void submit(e)}>
        <h4>3 &middot; Paste the code that page shows you</h4>
        <p className="muted small">
          When you have signed in, Claude prints a short code on the page.{' '}
          <strong>
            It shows the code, then a <code>#</code>, then a long string.
          </strong>{' '}
          Paste <em>all of it</em> &mdash; the platform splits it, and uses the
          part after the <code>#</code> to check the paste belongs to{' '}
          <em>this</em> sign-in rather than another tab&rsquo;s. Pasting only the
          part before the <code>#</code> works too.
        </p>
        {/* NOT WHEN THE SIGN-IN IS OVER. This counts down the platform's hold
            on a pending record that no longer exists, and its expired branch
            says the paste field is still live -- which is exactly what `dead`
            has just stopped being true. */}
        {!dead && <Deadline auth={at.auth} startedAt={at.startedAt} />}
        <input
          type="text"
          className="mono acct-wide"
          value={code}
          spellCheck={false}
          autoComplete="off"
          disabled={at.sending || dead}
          placeholder={
            dead
              ? refusal === 'accepted_unnamed'
                ? 'the platform already accepted this sign-in'
                : 'this sign-in has ended — start over'
              : 'paste the code here'
          }
          aria-label="The code the Claude callback page displayed"
          onChange={(e) => setCode(e.target.value)}
        />
        {hint && <p className="muted small">{hint}</p>}
        <p className="acct-buttons">
          <button type="submit" disabled={at.sending || dead || code.trim() === ''}>
            {at.sending
              ? 'redeeming…'
              : mode === 'add'
                ? 'Add this account'
                : 'Replace the credential'}
          </button>
        </p>
        {at.failed && refusal !== null && (
          <>
            {/* NO RETRY BUTTON ON THIS ONE. The code was taken out of the field
                before the request left, so there is nothing to re-send; what
                follows names the controls that do work, per refusal. */}
            <SignInFailure
              error={at.failed}
              what="POST /v1/accounts/exchange"
              /* A 201 is not a failure, and the panel may not shout that it
                 is over a message saying the platform accepted the sign-in. */
              heading={
                refusal === 'accepted_unnamed'
                  ? 'The platform accepted this sign-in without naming the account'
                  : undefined
              }
              tone={refusal === 'accepted_unnamed' ? 'partial' : 'failed'}
            />
            <ExchangeAdvice
              refusal={refusal}
              mode={mode}
              onStartOver={() => set({ kind: 'idle' })}
              reload={reload}
            />
          </>
        )}
      </form>
    </>
  )
}

/**
 * PUTTING THE STATE BACK IS A SECOND CALL, and it is the same second call
 * wherever the sign-in was started from.
 *
 * Registering an existing label REPLACES the credential and deliberately
 * PRESERVES the state (accountstore.py:110-125), so that a credential
 * replacement cannot silently un-pause an account somebody paused. The
 * consequence is that an account in REAUTH_REQUIRED is STILL in
 * REAUTH_REQUIRED after a perfectly successful sign-in, and nothing will be
 * assigned to it.
 *
 * THE ADD FORM REACHES THAT CASE AS READILY AS THE ROW DOES -- signing in
 * under a label that already exists is one of the ways to fix such an account,
 * and the form says so -- so it may not hardcode "nothing to put back" and
 * then render the result green. That is this repository's defining bug reached
 * from the screen's primary control.
 *
 * THE EVIDENCE IS THE REGISTERED DOCUMENT, not the row this page was looking
 * at before: the exchange response carries the account the store wrote, so its
 * `state` is the one that decides. Null out means there was nothing to
 * restore, which is a different thing from a restore that failed, and
 * `SignInDone` renders the two differently again.
 */
async function restoreStateIfNeeded(
  registered: Account,
): Promise<Extract<SignIn, { kind: 'done' }>['restore']> {
  if (!needsAHuman(registered)) return null
  const back = await setAccountState(
    registered.account_id,
    'AVAILABLE',
    'signed in again from the Accounts screen',
  )
  return back.status === 'ok'
    ? { ok: true }
    : {
        ok: false,
        error: back.status === 'error' || back.status === 'stale' ? back.error : null,
      }
}

// ---------------------------------------------------------------------------
// Add an account
// ---------------------------------------------------------------------------

function AddAccount({
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
  // THE OWNER IS THE TOKEN'S TENANT, and `tenant_id` here is that value echoed
  // by routes/accounts.py -- deliberately not taken from the broker's answer,
  // "so the page can never be told it is looking at a tenant it is not". It is
  // therefore the tenant the sign-in will file this under, and naming anything
  // else on screen would be a promise the API does not keep.
  const owner = board.page.tenant_id
  const [label, setLabel] = useState('')
  const [lend, setLend] = useState('')
  const state = ui.signin
  const set = (next: SignIn) => patch((p) => ({ ...p, signin: next }))

  // NOT CONFIGURED MEANS REFUSE, never accept-anything. Without a named tenant
  // this form could still submit safely -- the API files the account under the
  // verified token whatever the page thinks -- but it could not TELL the
  // operator whose pool the account is about to land in, and a sign-in is not
  // something to spend on a maybe.
  if (owner === null) {
    return (
      <section className="section panel">
        <h2>Add an account</h2>
        <div className="ctl-empty is-partial" role="status">
          <h3>Adding an account is unavailable until your tenant is named</h3>
          <p>
            An account is owned by exactly one tenant, and the API decides which
            from your verified sign-in rather than from anything typed here. This
            response did not name one, so this form cannot tell you whose pool an
            account would land in &mdash; and that is not something to guess at.
          </p>
          <p>
            This is a gap in what was read, not a fault in the platform. It says
            nothing about the accounts above, which loaded.
          </p>
        </div>
      </section>
    )
  }

  const trimmed = label.trim()
  // Owned labels only: a LENT account shares the screen and is not in this
  // tenant's namespace, so warning that its name is taken would be wrong.
  const replacing = accounts.some((a) => a.owner_tenant === owner && a.label === trimmed)

  // NO EVENT ARGUMENT, so the failure panel can call it too. A "Try again"
  // that cannot re-send the request it is offered for is worse than no button.
  //
  // AN EMPTY LABEL IS REFUSED HERE AND OFFERED NOWHERE. The platform checks the
  // label before it hands back a sign-in link, so an empty one is a refusal
  // this page can see coming and returning early is right. What was wrong was
  // the failure panel offering "Try again" wired to this anyway: the most
  // prominent control on the panel, producing no request, no spinner and no
  // message, which is indistinguishable from a platform that swallowed it.
  // BOTH callers now gate on the same `trimmed`, and this line is the backstop
  // rather than the rule.
  const start = async () => {
    if (trimmed === '') return
    const lendTo = splitTenants(lend).filter((t) => t !== owner)
    set({ kind: 'starting' })
    const res = await beginAccountSignIn({
      // THE LABEL AND THE LENDING LIST, AND NOTHING ELSE. The pending record
      // the broker writes is what decides where the account lands when the code
      // comes back minutes later, and the tenant on it is the one swarm-api
      // resolves from the VERIFIED TOKEN. `AccountSignInStart` has no
      // `owner_tenant` field and no `provider` field and forbids extra keys, so
      // sending either -- which this call used to do, because the broker's own
      // route takes them -- is a 422 from validation instead of a sign-in.
      // `owner` is that same resolved tenant echoed back by routes/accounts.py,
      // so what this form names and what the record files under cannot
      // disagree; it is displayed, not transmitted.
      label: trimmed,
      lend_to: lendTo,
    })
    if (res.status !== 'ok') {
      set({ kind: 'start_failed', error: failureOf(res) })
      return
    }
    set({
      kind: 'open',
      label: trimmed,
      lendTo,
      auth: res.data,
      startedAt: Date.now(),
      opened: 'not_yet',
      sending: false,
      failed: null,
    })
  }

  return (
    <section className="section panel">
      <h2>Add an account</h2>
      <p className="conjunction">
        Owned by <strong>{owner}</strong>. This is a sign-in, not a paste: you
        open Claude&rsquo;s own login page, sign in as you normally would, and
        copy back one short code. This platform never sees your password and
        never asks you for a keychain item. What it receives is stored{' '}
        <strong>write-only</strong> &mdash; split into two Secret Manager
        secrets, returned by no route here, and never rendered on this screen,
        not even as a length.
      </p>

      {state.kind === 'done' && (
        <SignInDone
          state={state}
          onAgain={() => {
            set({ kind: 'idle' })
            setLabel('')
            setLend('')
          }}
        />
      )}

      {state.kind === 'open' ? (
        <>
          <div className="acct-action">
            <h4>1 &middot; Named</h4>
            <p className="muted small">
              This sign-in is reserved for{' '}
              <code>
                {owner}:{state.label}
              </code>
              {state.lendTo.length > 0 ? (
                <>
                  , lent to <strong>{state.lendTo.join(', ')}</strong>
                </>
              ) : (
                <>, lent to nobody</>
              )}
              . The platform is holding those alongside the sign-in, so they
              cannot be changed from here without starting a new one &mdash;{' '}
              <em>start over</em> below does exactly that.
            </p>
          </div>
          <SignInSteps
            at={state}
            mode="add"
            set={set}
            reload={reload}
            onExchanged={async (account, expiresAt) => {
              // `restore: null` was hardcoded here, which reported a green
              // "Signed in" for an account the store had handed back still in
              // REAUTH_REQUIRED -- unassignable, with nothing on screen saying
              // so. The registered account says whether there is anything to
              // put back; the row's `Sign in again` has always done this and
              // the primary control has to do the same.
              const restore = await restoreStateIfNeeded(account)
              set({ kind: 'done', account, expiresAt, restore })
              setLabel('')
              setLend('')
              reload()
            }}
          />
        </>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            void start()
          }}
        >
          {/* NOT A PICKER, AND NOT A SILENT DEFAULT EITHER. There is one kind of
              credential in this pool, so there is nothing to choose; but a value
              the request carries and the form never mentions is a decision made
              for the operator with nothing on screen admitting it. So it is
              shown, named, and said to be fixed. */}
          <span className="t-label">provider</span>
          <p className="acct-fixed mono">
            {SUBSCRIPTION_PROVIDER}
            <span className="acct-why">
              fixed, and the only kind this pool handles: a Claude subscription.
              The sign-in yields the OAuth pair quota-broker can exchange for a
              successor indefinitely &mdash; that is what makes &ldquo;the only
              manual step is the first one&rdquo; true. There is deliberately no
              API-key option: a key that cannot be exchanged has nothing for the
              sweep to keep alive.
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
            onChange={(e) => setLabel(e.target.value)}
          />
          <p className="muted small">
            A name for this subscription, for you. The account id will be{' '}
            <code>
              {owner}:{trimmed || 'label'}
            </code>
            . Lowercase letters, digits and dashes, starting and ending
            alphanumeric, at most 40 characters &mdash; it becomes part of a
            Secret Manager name and a Kubernetes annotation. The platform checks
            it <strong>before it hands back a sign-in link</strong>, so a name it
            cannot use costs you a message rather than a wasted login. That check
            is the platform&rsquo;s own and is not repeated here, because a
            second copy of the rule would eventually refuse a name the platform
            would have taken.
          </p>
          {replacing && (
            <p className="warn-text">
              <strong>{trimmed} already exists in this pool.</strong> Signing in
              under that label REPLACES its credential rather than adding a second
              account &mdash; the id, the readings, the lending list and the state
              are all kept. If you meant another subscription, give it a different
              name. If you meant to fix that one, its own row has{' '}
              <em>Sign in again</em>, which also puts its state back afterwards.
            </p>
          )}

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
            Isolation is the default. Naming a tenant here lets that
            tenant&rsquo;s pods mount this account&rsquo;s access token, which is
            a narrowing of invariant 9 rather than a hole in it. It is a decision
            with a name on it, and it can be changed per account afterwards.
          </p>

          <p className="acct-buttons">
            <button type="submit" disabled={state.kind === 'starting' || trimmed === ''}>
              {state.kind === 'starting' ? 'asking for a sign-in link…' : 'Start the sign-in'}
            </button>
          </p>
          {state.kind === 'start_failed' && (
            <>
              {/* THE RETRY IS NOT OFFERED WITHOUT SOMETHING TO RETRY WITH. The
                  label field sits directly above this panel and stays editable
                  while it is open, so it can be empty by the time anybody reads
                  it -- and `start` refuses an empty label, which made this
                  button a silent no-op: no request, no spinner, no message.
                  Withdrawing the button and naming the empty field is the same
                  rule the submit button below follows by being disabled: a
                  control that cannot act does not look like one that can. */}
              <SignInFailure
                error={state.error}
                what="POST /v1/accounts/authorize"
                onRetry={trimmed === '' ? undefined : () => void start()}
              />
              {trimmed === '' && (
                <p className="warn-text">
                  There is no <em>Try again</em> on this failure because the{' '}
                  <strong>label field above is empty</strong>, and the platform
                  checks the label before it hands back a sign-in link. Type the
                  name this account should have and press{' '}
                  <em>Start the sign-in</em>.
                </p>
              )}
            </>
          )}
        </form>
      )}
    </section>
  )
}

/**
 * The receipt.
 *
 * IT SAYS WHAT EXPIRES AND WHAT DOES NOT, because an expiry in eight hours
 * otherwise reads as "do this again tonight" and the whole promise of this pool
 * is that you sign in once. The ACCESS half expires; the pair does not, because
 * the sweep exchanges it for a successor before it can.
 */
function SignInDone({
  state,
  onAgain,
}: {
  state: Extract<SignIn, { kind: 'done' }>
  onAgain: () => void
}) {
  const restore = state.restore
  const partial = restore !== null && !restore.ok
  return (
    <div className={`state ${partial ? 'partial' : 'acct-ok'}`} role="status">
      <h3>
        {partial ? 'Signed in, but the state was not put back' : 'Signed in'}{' '}
        &mdash; {state.account.account_id}
      </h3>
      <p>
        Two secrets were written: the pair, which only quota-broker reads, and
        the access token, which is what a pod mounts into{' '}
        <code>CLAUDE_CODE_OAUTH_TOKEN</code>.{' '}
        {state.expiresAt ? (
          <>
            That access token expires in{' '}
            <strong>{clearsIn(state.expiresAt, Date.now())}</strong>, and that is
            not a deadline for you: the sweep exchanges the refresh half for a
            successor before it lapses, which is exactly what makes this a
            one-time sign-in.
          </>
        ) : (
          <>
            The platform did not report when the access token expires, so there
            is no figure here rather than a guessed one. That says nothing about
            whether the credential is good &mdash; <em>Refresh this account</em>{' '}
            in its row asks the broker directly.
          </>
        )}
      </p>
      {restore !== null &&
        (restore.ok ? (
          <p>
            This account was in REAUTH_REQUIRED, and signing in deliberately does
            not clear that by itself &mdash; the same rule that stops a
            credential replacement silently un-pausing an account somebody
            paused. So its state was put back to AVAILABLE as a second, separate
            call, and that call succeeded.
          </p>
        ) : (
          <p>
            <strong>
              This account is STILL in REAUTH_REQUIRED, so nothing will be
              assigned to it.
            </strong>{' '}
            The credential is in place and is good; what failed was the second
            call that puts the state back:{' '}
            {restore.error?.message ?? 'the request did not complete.'} Use{' '}
            <em>move to AVAILABLE</em> in the row above &mdash; nothing needs
            signing in again.
          </p>
        ))}
      {/* WHAT THE POOL WILL SHOW FOR IT, READ OFF THE ACCOUNT THE EXCHANGE
          RETURNED. This panel is shared by the add form and by every
          `Sign in again`/`Replace the credential`, and re-registering carries
          `windows`, `observed_at`, `holds` and `assigned` through
          (accountstore.py:110-125). An unconditional "no reading yet" is
          therefore false for every re-authentication and for every add under a
          label that already exists -- and the table one panel above would be
          showing live 5H/7D figures for the same account at the same moment.
          The fact was in hand and was never consulted. */}
      {state.account.observed_at === null ? (
        <p className="checked-at">
          It appears in the pool with no reading yet &mdash; that is{' '}
          <em>unmeasured</em>, not idle. The first worker to run on it reports
          one.
        </p>
      ) : (
        <p className="checked-at">
          Its readings were kept: the last one arrived{' '}
          {timeAgo(state.account.observed_at)}
          {state.account.stale
            ? ', which is older than the 30 minutes a reading is trusted for, so the table above marks its figures ~'
            : ''}
          . Signing in replaces the credential and nothing else &mdash; the id,
          the readings and the lending list are the same ones that table is
          showing.
        </p>
      )}
      {/* NOT AVAILABLE, AND NOTHING PUT IT BACK. `restore === null` means there
          was nothing to restore -- this account is not in REAUTH_REQUIRED --
          which is not the same as "it will take work now". PAUSED and DRAINING
          are preserved through a re-registration on purpose, and AVAILABLE is
          the only member of ASSIGNABLE_STATES, so a green receipt over a
          paused account would be a success claimed for something that will not
          run. */}
      {restore === null && state.account.state !== 'AVAILABLE' && (
        <p className="warn-text">
          <strong>
            It is in {state.account.state}, and AVAILABLE is the only state an
            agent is started on.
          </strong>{' '}
          The credential is in place; the state was left exactly as it was,
          which is deliberate &mdash; replacing a credential does not un-pause
          an account somebody paused. Its row has <em>move to AVAILABLE</em> for
          when it should take work again.
        </p>
      )}
      <p className="acct-buttons">
        <button type="button" onClick={onAgain}>
          done
        </button>
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Sign in again, on a row that already exists
// ---------------------------------------------------------------------------

/**
 * The same sign-in, aimed at a label that is already in the pool.
 *
 * TWO STEPS, VISIBLY, because the store makes them two steps. Registering an
 * existing label replaces the credential and deliberately PRESERVES the state
 * (accountstore.py:106-124) so that it cannot silently un-pause an account
 * somebody paused. The consequence is that an account in REAUTH_REQUIRED is
 * still in REAUTH_REQUIRED after a perfectly successful sign-in, and nothing
 * will be assigned to it. So the state is put back as an explicit second call,
 * and if that call fails this says so rather than reporting one green tick for
 * two operations.
 *
 * THE LENDING LIST IS SENT BACK AS IT IS. Re-registering a label REPLACES
 * `lend_to` rather than merging it, so omitting it here would quietly revoke
 * every loan the account had as a side effect of fixing its credential.
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
  const state = ui.reauth[account.account_id] ?? { kind: 'idle' }
  const set = (next: SignIn) =>
    patch((p) => ({ ...p, reauth: { ...p.reauth, [account.account_id]: next } }))
  const broken = needsAHuman(account)

  // A CONTROL THAT WOULD REWRITE A FIELD IT CANNOT SEND. The sign-in swarm-api
  // performs produces a `SUBSCRIPTION_PROVIDER` credential and its request body
  // has no provider field, so re-registering a row that says anything else
  // would file the account under a provider nobody chose on this screen -- the
  // silent rewrite the removed `provider: account.provider` argument existed to
  // prevent. Refused and named, rather than offered and surprising.
  if (account.provider !== SUBSCRIPTION_PROVIDER) {
    return (
      <div className="acct-action">
        <h4>{broken ? 'Sign in again' : 'Replace the credential'}</h4>
        <div className="ctl-empty is-partial" role="status">
          <h3>This screen cannot sign in for this account</h3>
          <p>
            Its provider is <code>{account.provider}</code>, and the sign-in
            this platform performs produces a{' '}
            <code>{SUBSCRIPTION_PROVIDER}</code> credential &mdash; the request
            carries no provider, so there is no way to ask it for the other one.
            Signing in here would replace the credential and leave the row
            describing something it is not.
          </p>
          <p>
            Nothing about the account has changed, and nothing here failed. Its
            readings, its lending list and its state are the ones the row above
            shows.
          </p>
        </div>
      </div>
    )
  }

  const start = async () => {
    set({ kind: 'starting' })
    const res = await beginAccountSignIn({
      // NEITHER THE TENANT NOR THE PROVIDER IS OURS TO SEND. swarm-api resolves
      // the tenant from the verified token -- and this control renders only on
      // a row this tenant owns -- and it fixes the provider itself;
      // `AccountSignInStart` has a field for neither and forbids extra keys, so
      // sending `account.owner_tenant` and `account.provider`, which this call
      // used to do, is a 422 from validation rather than a sign-in. The
      // provider that used to be sent here is now checked above instead: a row
      // carrying anything else is refused rather than quietly re-registered.
      label: account.label,
      lend_to: account.lend_to,
    })
    if (res.status !== 'ok') {
      set({ kind: 'start_failed', error: failureOf(res) })
      return
    }
    set({
      kind: 'open',
      label: account.label,
      lendTo: account.lend_to,
      auth: res.data,
      startedAt: Date.now(),
      opened: 'not_yet',
      sending: false,
      failed: null,
    })
  }

  const finish = async (registered: Account, expiresAt: string | null) => {
    // The REGISTERED account decides, not `broken` -- which was read off the
    // row before the sign-in and can disagree with what the store wrote.
    const restore = await restoreStateIfNeeded(registered)
    set({ kind: 'done', account: registered, expiresAt, restore })
    reload()
  }

  return (
    <div className="acct-action">
      <h4>{broken ? 'Sign in again' : 'Replace the credential'}</h4>
      <p className="muted small">
        {broken ? (
          <>
            The refresh token behind <code>{account.account_id}</code> is gone or
            unreadable, and only a person can replace it. This is the same
            sign-in that adds an account: a login page and one short code. The
            id, the readings and the lending list are all kept, because the label
            is the same.
          </>
        ) : (
          <>
            Replaces the credential behind <code>{account.account_id}</code> with
            a fresh one, keeping the id, the readings, the lending list and the
            state. Nothing here needs doing on a healthy account &mdash; the
            sweep keeps it alive on its own.
          </>
        )}
      </p>

      {state.kind === 'done' && (
        <SignInDone state={state} onAgain={() => set({ kind: 'idle' })} />
      )}

      {state.kind === 'open' ? (
        <SignInSteps
          at={state}
          mode="reauth"
          set={set}
          onExchanged={finish}
          reload={reload}
        />
      ) : (
        <>
          <div className="acct-buttons">
            <button
              type="button"
              disabled={state.kind === 'starting'}
              onClick={() => void start()}
            >
              {state.kind === 'starting'
                ? 'asking for a sign-in link…'
                : broken
                  ? 'Sign in again'
                  : 'Start a replacement sign-in'}
            </button>
          </div>
          {state.kind === 'start_failed' && (
            <SignInFailure
              error={state.error}
              what="POST /v1/accounts/authorize"
              onRetry={() => void start()}
            />
          )}
        </>
      )}
    </div>
  )
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
        <strong>retained</strong>, not deleted, so this is reversible by signing
        in again under the same label and no version history is lost.
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
// Instructions
// ---------------------------------------------------------------------------

/**
 * Real operator instructions, in the page, because this is the screen someone
 * opens at the moment they need them and a wiki link is a second place to be
 * wrong. Every step below is one this screen performs.
 */
function Instructions() {
  return (
    <section className="section panel acct-instructions">
      <h2>How to run this pool</h2>

      <h3>1 &middot; Adding an account is a sign-in</h3>
      <p>
        Name it, press <em>Start the sign-in</em>, open the page it gives you,
        sign in to Claude as you normally would, and paste back the short code
        that page shows. That is the whole procedure. There is no keychain item
        to find, no JSON to preserve, no wrapper to keep intact, and nothing to
        run on your laptop.
      </p>
      <ul>
        <li>
          <strong>Why a code is still pasted.</strong> Anthropic&rsquo;s OAuth
          client accepts exactly one redirect target &mdash; its own callback
          page, which <em>displays</em> a code. A third-party application cannot
          register <code>https://swarm.saga.xyz/callback</code>, so your browser
          cannot be sent back here, and the code on that page is what closes the
          loop. One short string is as close to &ldquo;just a button&rdquo; as
          this can get.
        </li>
        <li>
          <strong>Paste the whole thing.</strong> The page shows the code, then a{' '}
          <code>#</code>, then a long string. The platform splits it, and uses
          the part after the <code>#</code> to check the paste belongs to the
          sign-in <em>this</em> page started rather than another tab&rsquo;s
          &mdash; two tabs open is how a credential lands on the wrong account.
          Pasting only the part before the <code>#</code> works too.
        </li>
        <li>
          <strong>Codes are single-use and expire quickly.</strong> A refused
          code almost always means it was already redeemed or that too much time
          passed. Press <em>Open the sign-in page again</em> for a new one: the
          sign-in itself stays open, so the label and the lending list do not
          need re-entering.
        </li>
        <li>
          <strong>
            A second account needs a different browser application, not a private
            window.
          </strong>{' '}
          Which organisation you sign in as comes from the cookies the browser
          already holds, and nothing in the request overrides it. A private
          window shares the same cookie jar, so it signs you in as the same
          organisation and you get a second row for a subscription you already
          had, with nothing explaining why the pool did not really grow. Copy the
          sign-in link and open it in another browser application, or sign out of
          Claude in this one first.
        </li>
        <li>
          <strong>Nothing secret passes through this page.</strong> The PKCE
          verifier stays on the server, keyed by the sign-in &mdash; a verifier
          the browser holds is a PKCE flow that proves nothing. What comes back
          is an account and an expiry: never key material, and never its length.
        </li>
      </ul>
      <p>
        What the platform does with what it receives: two secrets.{' '}
        <code>&lt;base&gt;-refresh</code> holds the pair and is read only by
        quota-broker. <code>&lt;base&gt;</code> holds <em>only</em> the access
        token, and is what a tenant&rsquo;s pod mounts into{' '}
        <code>CLAUDE_CODE_OAUTH_TOKEN</code>. That split is the security
        boundary: a compromised pod holds a credential that expires, not one that
        can mint successors forever.
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
          because a timer cannot answer &ldquo;did that work?&rdquo;.
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
          Open that account&rsquo;s row here and press <em>Sign in again</em>. It
          is the same sign-in as adding an account, aimed at the label already in
          the pool, so the id, the readings and the lending list are kept. A new
          label would instead create a second account and leave the broken one
          sitting there.
        </li>
        <li>
          <strong>Check which organisation this browser is signed in as.</strong>{' '}
          The sign-in replaces the credential with whoever this browser is
          currently logged in as, and nothing in the request overrides that. If
          it is not the subscription this label is for, copy the link into a
          different browser application first.
        </li>
        <li>
          <strong>
            The state is put back for you, as a visible second step.
          </strong>{' '}
          Signing in deliberately does <em>not</em> change state on its own
          &mdash; that rule is what stops it silently un-pausing an account
          somebody paused &mdash; so this screen makes a second call to move the
          account back to AVAILABLE, and says plainly if that second call fails.
          An account left in REAUTH_REQUIRED is unassignable no matter how good
          its new credential is.
        </li>
        <li>
          Press <em>Refresh this account</em> if you want independent
          confirmation. <code>refreshed</code> or <code>still_valid</code> means
          it is alive. The state chip alone is not that confirmation.
        </li>
      </ol>

      <h3>4 &middot; Why a credential with no refresh token is refused</h3>
      <p>
        A sign-in normally yields a pair: an access token, and the refresh token
        that mints its successors. If the token endpoint ever answers with an
        access token and nothing beside it, the platform refuses it and stores
        nothing, with a 422 saying the credential could not be kept alive. The
        same refusal is what a <code>claude setup-token</code> value used to get
        when this screen still took pastes.
      </p>
      <p>
        The refusal is the feature. Such a credential cannot be exchanged, so
        when it expires a person has to log in again. Accepted, it would look
        perfectly healthy &mdash; the account would sit at AVAILABLE, agents
        would be assigned to it, and at its first expiry every one of them would
        fail on an expired token with nothing on any screen pointing at the
        cause. Being refused costs you one error message now. Being accepted
        costs an outage later, at a time nobody chose.
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
        <dt>A row can read AVAILABLE and still serve nobody here</dt>
        <dd>
          When a tenant reports that it cannot read an account&rsquo;s secret,
          the broker&rsquo;s own assignment rule skips that account{' '}
          <em>for that tenant</em> and for nobody else. Its state stays
          AVAILABLE, its windows stay real, and its headroom is genuinely
          there &mdash; just not for you. Such a row says so under the state
          chip, because without that it is the healthiest-looking row on the
          screen and nothing runs on it. The report is forgotten thirty minutes
          after it was made; one that keeps returning is a missing{' '}
          <code>secretAccessor</code> grant, not an onboarding delay.
        </dd>
        <dt>&ldquo;Never assigned&rdquo; on every row is not a quiet week</dt>
        <dd>
          It is what a pool no worker can reach looks like: accounts registered,
          nothing ever asking for one. On a single row it means nothing
          &mdash; a new account has not been used yet. Across the whole pool it
          points at <code>QUOTA_BROKER_URL</code> missing from dispatched jobs,
          or a worker service account without{' '}
          <code>roles/run.invoker</code> on the broker.
        </dd>
        <dt>A count of accounts is only a fact if every document was read</dt>
        <dd>
          The store skips an account document it cannot parse so that one bad
          document does not hide the fleet. It now reports how many it skipped,
          and this screen says so above the table &mdash; because a row count,
          a headroom figure and a &ldquo;none need attention&rdquo; are each
          computed over what was read, and silently wrong otherwise.
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
        <dt>Adding a second account needs a second browser application</dt>
        <dd>
          Not a private window. The organisation you sign in as comes from the
          browser&rsquo;s cookie jar, a private window shares that jar, and
          nothing this platform sends overrides it. Without a different browser
          application you will add the same subscription twice and have no way to
          see why the pool did not grow.
        </dd>
        <dt>Four states, and they are not degrees of one thing</dt>
        <dd>
          <code>AVAILABLE</code> is the only state a <em>new</em> agent may be
          started on. <code>PAUSED</code> means no new work while the agents on it
          keep running. <code>DRAINING</code> means they are being moved off.{' '}
          <code>REAUTH_REQUIRED</code> is a verdict the broker reached, not a
          state to declare &mdash; only a person clears it, and signing in again
          is how.
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
