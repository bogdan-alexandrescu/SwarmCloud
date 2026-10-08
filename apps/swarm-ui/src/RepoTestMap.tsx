import { useState } from 'react'
import { selectRepositoryTests } from './api'
import { Button, Card, Chip, Dash } from './components'
import {
  globParts, pathsOf, shortSha, testMapRows,
  type AlwaysTestRow, type IndexDoc, type RepoRecord, type TestSelection,
} from './RepositoriesData'
import { UrCopy, UrLink, UrRegion, graphTestsAddress, useUrRead } from './RepositoriesParts'

/**
 * ONE REPOSITORY › TEST MAP, as the picked design draws it (PICKS.md "Detail
 * B's path lookup becomes its Test map tab"; repositories.html Detail B,
 * "Which tests cover these paths?"). QA G4-08: the tab had become a substring
 * filter over the edges, which ignored the always-run tests, the suites and
 * each edge's command, and could not say which paths NO edge covers -- the
 * document has no `unmapped` key; the API's selection computes it.
 *
 * SO THE LOOKUP ASKS THE API, NOT A COPY OF ITS LOGIC. `POST
 * .../tests:select` (repoindex.py `select_tests`) is exactly what a planner
 * is told: the tests the edges pick, the always-run tests, each unmapped
 * path, and the suite to run for those. Re-deriving that here would be a
 * second selection that drifts from the first.
 *
 * Below it, the document itself: the edges grouped by source glob (a glob
 * drawn as a glob, its `/**` dimmed), then "Always run (n)" and "Suites (n)".
 */

const SELECT_ROUTE = 'POST /v1/repositories/{repo_id}/tests:select'

/** "Which tests cover these paths?": paths in, the API's `{tests, always, unmapped, fallback}` out. */
export function PathLookup({ r, doc }: { r: RepoRecord; doc: IndexDoc | null }) {
  const [typed, setTyped] = useState('')
  const [asked, setAsked] = useState<string[] | null>(null)
  const paths = pathsOf(typed)
  const at = r.index.current_sha
  return (
    <Card
      title="Which tests cover these paths?"
      className="ur-lookup"
      action={<span className="ur-mu">{at === null ? 'no index yet' : `from the test map at ${shortSha(at)}`}</span>}
    >
      <form
        className="ur-lookup-form"
        onSubmit={(e) => {
          e.preventDefault()
          if (paths.length > 0) setAsked(paths)
        }}
      >
        <textarea
          className="ur-paths"
          aria-label="Paths to look up"
          placeholder="src/api/routes/users.py, src/api/models.py"
          rows={2}
          spellCheck={false}
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
        />
        <Button kind="primary" size="sm" type="submit" disabled={paths.length === 0}>
          Find tests
        </Button>
      </form>
      <p className="ur-hint">Paths in the repository, separated by commas, spaces or new lines. The answer is what a planner is told for a change to them.</p>
      {asked !== null && <Selection key={asked.join('\u0000')} r={r} paths={asked} doc={doc} />}
    </Card>
  )
}

function Selection({ r, paths, doc }: { r: RepoRecord; paths: string[]; doc: IndexDoc | null }) {
  const read = useUrRead(() => selectRepositoryTests(r.repo_id, paths), `${r.repo_id}:select:${paths.join('\u0000')}`)
  return (
    <UrRegion state={read.state} route={SELECT_ROUTE} what="The test selection" onRetry={read.reload} lines={3}>
      {(s) => (s === null ? <p className="ur-none">The selection route answered with nothing this page can read.</p> : <SelectionTable s={s} doc={doc} />)}
    </UrRegion>
  )
}

/** Where an always-run test is declared, from the document (the selection's `always` does not carry it). */
function declaredIn(doc: IndexDoc | null, target: string): string | null {
  return doc?.always_tests.find((a) => a.target === target)?.source ?? null
}

function SelectionTable({ s, doc }: { s: TestSelection; doc: IndexDoc | null }) {
  const nothing = s.tests.length === 0 && s.always.length === 0 && s.unmapped.length === 0
  return (
    <>
      {s.index_sha !== null && <p className="ur-sub">{`Answered from the test map at ${shortSha(s.index_sha)}.`}</p>}
      {s.reason !== null && <p className="ur-hint is-warn">{s.reason}.</p>}
      {nothing ? (
        <p className="ur-none">The selection named no test and no unmapped path.</p>
      ) : (
        <table className="ur-sel">
          <thead>
            <tr>
              <th>Test</th>
              <th>Because</th>
              <th>Evidence</th>
            </tr>
          </thead>
          <tbody>
            {s.tests.map((t) => (
              <tr key={`t:${t.target}`} data-kind="test">
                <td>
                  <code>{t.target}</code>
                  {t.command !== null && <UrCopy text={t.command} label={`Copy the command for ${t.target}`} />}
                </td>
                <td className="ur-mu">{t.because.join(', ')}</td>
                <td>{t.evidence === null ? <Dash why="No evidence recorded" /> : <Chip>{t.evidence}</Chip>}</td>
              </tr>
            ))}
            {s.always.map((a) => {
              const source = declaredIn(doc, a.target)
              return (
                <tr key={`a:${a.target}`} data-kind="always">
                  <td>
                    <code>{a.target}</code>
                    {a.command !== null && <UrCopy text={a.command} label={`Copy the command for ${a.target}`} />}
                  </td>
                  <td className="ur-mu">
                    {source !== null ? `always run · declared in ${source}` : `always run · ${a.because ?? 'declared by the index'}`}
                    {source !== null && a.because !== null && <small className="ur-sel-why">{a.because}</small>}
                  </td>
                  <td>
                    <Chip>declared</Chip>
                  </td>
                </tr>
              )
            })}
            {s.unmapped.map((p) => (
              <tr key={`u:${p}`} data-kind="unmapped">
                <td>
                  <Chip className="ur-chip-warn">unmapped</Chip> <code>{p}</code>
                </td>
                <td className="ur-mu">
                  {s.fallback === null ? 'no edge in the map, and the index names no suite to run instead' : "no edge in the map; run the area's suite instead"}
                </td>
                <td>
                  {s.fallback === null ? (
                    <Dash why="The index lists no test suite and no test command" />
                  ) : (
                    <span className="ur-cmd">
                      <code>{s.fallback}</code>
                      <UrCopy text={s.fallback} label={`Copy the fallback command for ${p}`} />
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {s.fallback_covers_every_unmapped_path === false && (
        <p className="ur-hint is-warn">No suite covers every unmapped path, so the fallback is the widest suite the index lists.</p>
      )}
    </>
  )
}

/** A source glob in monospace, its `/**` dimmed: a directory's every file maps to its tests. */
export function GlobCode({ glob }: { glob: string }) {
  const g = globParts(glob)
  return (
    <code className="ur-glob" title={g.tail === null ? undefined : 'Every file under this directory'}>
      {g.stem}
      {g.tail !== null && <span className="ur-glob-tail">{g.tail}</span>}
    </code>
  )
}

/**
 * The edges grouped by source glob: the glob, "tests (n)", each test with its
 * evidence chip and a copy of its command, and a link to the Graph's "Tests
 * reaching a symbol" searching the source's directory (QA G4-09).
 */
export function EdgeGroups({ r, doc, filter, go }: { r: RepoRecord; doc: IndexDoc; filter: string; go: (to: string) => void }) {
  const f = filter.trim().toLowerCase()
  // One edge is one test (QA G4-02); many edges share a source, so a row is a source with all of its tests.
  const all = testMapRows(doc.test_map)
  const rows = all.filter((t) => f === '' || t.source.toLowerCase().includes(f) || t.tests.some((x) => x.test.toLowerCase().includes(f)))
  const cut = doc.truncated.includes('test_map')
  return (
    <div className="ur-rows">
      {rows.map((t) => {
        const g = globParts(t.source)
        const dir = g.tail !== null ? g.stem : t.source.includes('/') ? t.source.slice(0, t.source.lastIndexOf('/')) : t.source
        return (
          <div className="ur-tmap-row" key={t.source}>
            <span className="ur-tmap-src">
              <GlobCode glob={t.source} />
              <UrLink className="c-link ur-tmap-sym" to={graphTestsAddress(r.repo_id, dir)} go={go}>
                Tests reaching a symbol
              </UrLink>
            </span>
            <span className="ur-tmap-n">
              {`${t.tests.length} ${t.tests.length === 1 ? 'test' : 'tests'}`}
              {g.tail !== null && (
                <small
                  title={
                    cut
                      ? "One glob for the whole directory, and the index's test map was cut at its size ceiling"
                      : 'One glob for the whole directory: every file under it maps to these tests'
                  }
                >
                  {cut ? 'directory granularity — truncated' : 'directory granularity'}
                </small>
              )}
            </span>
            <span className="ur-tests">
              {t.tests.length === 0 ? (
                <Dash why="No tests listed for this path" />
              ) : (
                t.tests.map((x) => (
                  <span className="ur-test" key={`${t.source}\u0000${x.test}`} title={x.command === null ? 'No command recorded for this test' : `Run: ${x.command}`}>
                    <code>{x.test}</code>
                    {x.evidence === null ? <Dash why="No evidence recorded" /> : <Chip>{x.evidence}</Chip>}
                    {x.command !== null && <UrCopy text={x.command} label={`Copy the command for ${x.test}`} />}
                  </span>
                ))
              )}
            </span>
          </div>
        )
      })}
      {rows.length === 0 && <p className="ur-none">{all.length === 0 ? 'The index maps no tests.' : 'No path matches the filter.'}</p>}
    </div>
  )
}

/** "Always run (n)": the document's always-run tests, each with why, where it is declared and its command. */
export function AlwaysRun({ rows }: { rows: readonly AlwaysTestRow[] }) {
  return (
    <>
      <h3 className="ur-subh">{`Always run (${rows.length})`}</h3>
      {rows.length === 0 ? (
        <p className="ur-none">The index declares no always-run test.</p>
      ) : (
        <div className="ur-rows">
          {rows.map((a) => (
            <div className="ur-always-row" key={a.target}>
              <code>{a.target}</code>
              <span className="ur-mu">
                {a.because ?? <Dash why="The index gave no reason" />}
                {a.source !== null && <small>{` · declared in ${a.source}`}</small>}
              </span>
              {a.command === null ? (
                <Dash why="No command recorded for this test" />
              ) : (
                <span className="ur-cmd">
                  <code>{a.command}</code>
                  <UrCopy text={a.command} label={`Copy the command for ${a.target}`} />
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </>
  )
}

/** "Suites (n)": the document's test layout, each suite's root, framework, needs and command. */
export function Suites({ doc }: { doc: IndexDoc }) {
  const rows = doc.test_layout
  return (
    <>
      <h3 className="ur-subh">{`Suites (${rows.length})`}</h3>
      {rows.length === 0 ? (
        <p className="ur-none">The index lists no test suite.</p>
      ) : (
        <div className="ur-rows">
          {rows.map((s) => (
            <div className="ur-suite-row" key={s.root}>
              <code>{s.root}</code>
              <span className="ur-mu">
                {s.framework ?? <Dash why="The framework was not recorded" />}
                {s.needs.length > 0 && ` · needs ${s.needs.join(', ')}`}
              </span>
              {s.command === null ? (
                <Dash why="No command recorded for this suite" />
              ) : (
                <span className="ur-cmd">
                  <code>{s.command}</code>
                  <UrCopy text={s.command} label={`Copy the command for ${s.root}`} />
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </>
  )
}
