import { useState, type ReactNode } from 'react'
import { queryRepositoryImpact } from './api'
import { Banner, Button, Dash, EmptyState } from './components'
import { MarkGlyph } from './marks'
import {
  fmt, lineCount, linesWord, short, stalenessPill,
  type ImpactPlan, type Staleness,
} from './RepoGraphData'
import { shortSha, type RepoRecord } from './RepositoriesData'
import { UrRadio, UrRegion, useUrRead } from './RepositoriesParts'
import './styles/repograph.css'

/**
 * ONE REPOSITORY › IMPACT, pick A (repositories.html screen 9): four columns,
 * the chain the impact query walks -- diff → changed symbols → affected
 * callers → tests to run -- with the plan's own count at the head of each and
 * the reason on every test (repo-index.md §4.3a).
 *
 * THE QUERY IS `POST /v1/repositories/{repo_id}/impact` with a pull request
 * or a commit AND NOTHING ELSE: the route refuses paths, tests or a command
 * from a caller (invariant 10), and so does this page by never building one.
 * The API reads the diff from the forge with the token that resolves for the
 * repository; no token, no diff text and no file content reaches this page.
 *
 * A SHORT ANSWER IS NEVER A COMPLETE ONE: a path cut below the confidence
 * floor is stated in a banner, a column whose list the route capped says how
 * many it did not list, and a fallback plan's tests column is the full suite
 * with the trigger that sent it there.
 */

const IMPACT_ROUTE = 'POST /v1/repositories/{repo_id}/impact'
const SHA40 = /^[0-9a-f]{40}$/

type Change = { pull_request: number } | { commit: string }

function prNumber(v: string | null): number | null {
  if (v === null || !/^[1-9][0-9]{0,9}$/.test(v.trim())) return null
  return Number(v.trim())
}

export function ImpactTab({ r, pr }: { r: RepoRecord; pr: string | null }) {
  const opened = prNumber(pr)
  const [mode, setMode] = useState<'commit' | 'pr'>(opened === null ? 'commit' : 'pr')
  const [text, setText] = useState(opened === null ? '' : String(opened))
  const [refused, setRefused] = useState<string | null>(null)
  const [asked, setAsked] = useState<Change | null>(opened === null ? null : { pull_request: opened })

  function ask() {
    if (mode === 'pr') {
      const n = prNumber(text)
      if (n === null) return setRefused('A pull request is its number, such as 57.')
      setRefused(null)
      setAsked({ pull_request: n })
    } else {
      const sha = text.trim().toLowerCase()
      if (!SHA40.test(sha)) return setRefused('A commit is named by the full 40-character sha; the route takes no short one.')
      setRefused(null)
      setAsked({ commit: sha })
    }
  }

  return (
    <section className="ri-impact" aria-label="Impact">
      <form
        className="ri-form"
        onSubmit={(e) => {
          e.preventDefault()
          ask()
        }}
      >
        <UrRadio
          label="Change"
          value={mode}
          onChange={(m) => {
            setMode(m)
            setRefused(null)
          }}
          options={[
            { key: 'commit', label: 'Commit' },
            { key: 'pr', label: 'Pull request' },
          ]}
        />
        <input
          className="ur-search ri-input"
          aria-label={mode === 'pr' ? 'Pull request number' : 'Commit sha'}
          placeholder={mode === 'pr' ? 'Pull request number' : 'Commit sha, 40 characters'}
          inputMode={mode === 'pr' ? 'numeric' : 'text'}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <Button size="sm" kind="primary" type="submit">
          Show impact
        </Button>
      </form>
      {refused !== null && <p className="ur-hint ur-bad">{refused}</p>}
      {asked === null ? (
        <p className="ur-sub">
          Name a commit on {r.default_branch ?? 'the default branch'} or a pull request: the API reads its diff from GitHub and walks the
          index's graph from the changed symbols to the tests that reach them.
        </p>
      ) : (
        <ImpactRead key={JSON.stringify(asked)} r={r} change={asked} />
      )}
    </section>
  )
}

function ImpactRead({ r, change }: { r: RepoRecord; change: Change }) {
  const read = useUrRead(() => queryRepositoryImpact(r.repo_id, change), `${r.repo_id}:impact:${JSON.stringify(change)}`)
  return (
    <UrRegion state={read.state} route={IMPACT_ROUTE} what="The impact query" onRetry={read.reload} lines={6}>
      {(p) =>
        p === null ? (
          <EmptyState kind="partial" heading="No plan to show">
            The impact route answered with nothing this page can read as a test plan.
          </EmptyState>
        ) : (
          <ImpactPlanView p={p} change={change} />
        )
      }
    </UrRegion>
  )
}

/** The freshness pill of the index a plan or graph answer was made from. */
export function RiStalePill({ s }: { s: Staleness }) {
  const pill = stalenessPill(s)
  return (
    <span className={`c-pill is-${pill.hue}`} data-mark={pill.mark} title={pill.why ?? undefined}>
      <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
        <MarkGlyph mark={pill.mark} />
      </svg>
      {pill.word}
    </span>
  )
}

/** "1 test" / "12 tests". */
function n(count: number, one: string, many = `${one}s`): string {
  return `${fmt(count)} ${count === 1 ? one : many}`
}

function testName(id: string): string {
  return short(id)
}

function ImpactPlanView({ p, change }: { p: ImpactPlan; change: Change }) {
  const title = 'pull_request' in change ? `Pull request #${change.pull_request}` : `Commit ${change.commit.slice(0, 7)}`
  const indexSha = p.index_sha === null ? null : p.index_sha.slice(0, 7)
  const full = p.selection === 'full_suite'
  const [copied, setCopied] = useState(false)

  async function copyTests() {
    try {
      await navigator.clipboard.writeText(p.tests.map((t) => t.id).join('\n'))
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  const policy =
    p.policy === null ? (
      <Dash why="No selection policy is set for this repository or its tenant" />
    ) : (
      <b>{p.policy}</b>
    )
  const fallbackWord = p.selection === null ? null : full ? 'fallback: full suite' : 'no fallback'

  return (
    <>
      <div className="ri-phead">
        <h2>{title}</h2>
        <RiStalePill s={p} />
        <span className="ur-acts">
          {!full && p.tests.length > 0 && (
            <Button size="sm" onClick={() => void copyTests()}>
              {copied ? 'Copied the test list' : 'Copy test list'}
            </Button>
          )}
        </span>
      </div>
      <p className="ri-meta">
        <span>base {p.base_sha === null ? <Dash why="The base commit was not served" /> : <b className="ur-sha">{shortSha(p.base_sha)}</b>}</span>
        <span>head {p.head_sha === null ? <Dash why="The head commit was not served" /> : <b className="ur-sha">{shortSha(p.head_sha)}</b>}</span>
        <span>depth {p.depth === null ? <Dash why="The walk's depth was not served" /> : <b>{p.depth}</b>}</span>
        <span>min confidence {p.min_confidence === null ? <Dash why="The confidence floor was not served" /> : <b>{p.min_confidence}</b>}</span>
        <span>
          policy {policy}
          {fallbackWord !== null && ` · ${fallbackWord}`}
        </span>
      </p>
      <div className="ri-flow">
        <Column head={<><em>{fmt(p.diff.length)}</em> {p.diff.length === 1 ? 'file' : 'files'} in the diff</>}
          more={p.diff_truncated ? 'GitHub listed as many files as it ever lists; the rest of the diff was not read' : null}>
          {p.diff.map((d) => (
            <div key={d.path} className="ri-it">
              <code>{d.path}</code>
              <small>
                {[d.status, d.patch ? `+${lineCount(d.added)} −${lineCount(d.removed)}` : 'no patch served'].filter(Boolean).join(' · ')}
                {d.previous_path !== null && ` · was ${d.previous_path}`}
              </small>
            </div>
          ))}
        </Column>
        <Column head={<><Fig n={p.changed_symbols} why="The changed-symbol count was not served" /> changed symbols</>} more={null}>
          {p.changed.map((c) => {
            const lines = linesWord(c)
            return (
              <div key={c.id} className="ri-it">
                <code>{short(c.id)}</code>
                <small>{[lines === null ? null : `lines ${lines}`, c.why ?? c.side].filter(Boolean).join(' · ')}</small>
              </div>
            )
          })}
          {p.unindexed.map((u) => (
            <div key={`u:${u.path}`} className="ri-it">
              <code>{u.path}</code>
              <small>{`added · not in index ${indexSha ?? '(none)'}, file level`}</small>
            </div>
          ))}
        </Column>
        <Column
          head={<><Fig n={p.affected_callers} why="The affected-caller count was not served" /> affected callers</>}
          more={p.affected_callers !== null && p.affected_callers > p.affected.length ? `${fmt(p.affected_callers - p.affected.length)} more not listed` : null}
          note={p.depth === null ? null : `depth ${p.depth} of max 6`}
        >
          {p.affected.map((a) => (
            <div key={a.id} className="ri-it">
              <code>{short(a.id)}</code>
              <small>
                {[a.depth === null ? null : `depth ${a.depth}`, [a.evidence, a.evidence === 'lsp' ? null : a.confidence].filter((x) => x !== null).join(' ')].filter(Boolean).join(' · ')}
              </small>
            </div>
          ))}
        </Column>
        <Column
          head={
            full ? (
              <><Fig n={p.selected} why="The suite's size was not served" /> tests to run</>
            ) : (
              <>
                <Fig n={p.targeted} why="The selected-test count was not served" /> tests to run
                {p.total_tests === null ? (
                  <span className="ri-of"> of <Dash why="The suite's size was not served: the graph counts no test symbols" /></span>
                ) : (
                  <span className="ri-of">{` of ${fmt(p.total_tests)}`}</span>
                )}
              </>
            )
          }
          more={null}
        >
          {full ? (
            <div className="ri-it">
              <code>the full suite</code>
              <small>because {p.fallback_triggers.length === 0 ? 'the plan named no trigger' : p.fallback_triggers.map((t) => t.reason).join('; ')}</small>
            </div>
          ) : (
            p.tests.map((t) => (
              <div key={t.id} className="ri-it">
                <code title={t.id}>{testName(t.id)}</code>
                <small>
                  because it {t.reason ?? 'was chosen (no reason served)'}
                  {t.evidence.length > 0 && ` (${t.evidence.join(', ')})`}
                </small>
              </div>
            ))
          )}
        </Column>
      </div>
      {p.low_confidence_cut.length > 0 && <CutBanner p={p} />}
      {p.node_cap_hit && (
        <Banner tone="warn" title="The walk hit its node bound">
          The callers were cut at the route's bound, so the plan may be missing tests a deeper walk would find.
        </Banner>
      )}
      {!full && p.fallback_triggers.length > 0 && (
        <p className="ur-sub">Under P3 these would send the change to the full suite: {p.fallback_triggers.map((t) => t.reason).join('; ')}.</p>
      )}
    </>
  )
}

function Fig({ n: count, why }: { n: number | null; why: string }) {
  return count === null ? <Dash why={why} /> : <em>{fmt(count)}</em>
}

const SHOWN = 4

function Column({ head, more, note = null, children }: { head: ReactNode; more: string | null; note?: string | null; children: ReactNode }) {
  const [all, setAll] = useState(false)
  const items = Array.isArray(children) ? children.flat().filter(Boolean) : [children]
  const hidden = items.length - SHOWN
  return (
    <div className="ri-col">
      <h3>{head}</h3>
      {items.length === 0 ? <p className="ur-none">None.</p> : all ? items : items.slice(0, SHOWN)}
      {!all && hidden > 0 && (
        <button type="button" className="ri-more" onClick={() => setAll(true)}>
          {`${hidden} more`}
        </button>
      )}
      {(more !== null || note !== null) && <p className="ri-more-note">{[more, note].filter(Boolean).join(' · ')}</p>}
    </div>
  )
}

function CutBanner({ p }: { p: ImpactPlan }) {
  const [open, setOpen] = useState(false)
  const cuts = p.low_confidence_cut
  const first = cuts[0]!
  const say = (c: typeof first) =>
    `${short(c.from)} → ${short(c.to)} is ${c.evidence === null ? 'an edge' : `an ${c.evidence} edge`} at ${c.confidence ?? '(confidence not served)'}`
  return (
    <div className="ri-cut">
      <Banner tone="warn" title={n(cuts.length, 'path cut', 'paths cut')}>
        {say(first)}; its path to any test falls below {p.min_confidence ?? 'the floor'}, so no test is chosen through it.
        {cuts.length > 1 && (
          <>
            {' '}
            <Button size="sm" onClick={() => setOpen(!open)}>
              {open ? 'Hide the cuts' : 'Show the cuts'}
            </Button>
          </>
        )}
        {open && (
          <ul className="ri-cuts">
            {cuts.map((c) => (
              <li key={`${c.from}>${c.to}`}>{say(c)}</li>
            ))}
          </ul>
        )}
      </Banner>
    </div>
  )
}
