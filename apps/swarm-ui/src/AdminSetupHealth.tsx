/**
 * ADMIN › SETUP › HEALTH (DAYONE-SETUP-UI-1; part of #2688;
 * docs/design/day-one-plan.md §5.1, D27, frame B12b).
 *
 * The latest scheduled doctor run, read from `GET /v1/admin/setup/health`
 * (swarm-doctor writes it; this page only reads): when it ran, the checks it
 * ran and its findings. Each finding says what drifted, what changed (the
 * audit-log entry, when the check knows it), the repair command
 * `swarm install --repair <check>` and the Cloud console path where one
 * exists. The full check list shows each check's last result.
 *
 * "NOT CHECKED" IS SHOWN AS SUCH, NEVER AS PASSING: a check whose result is
 * anything but `passed` or `failed` is drawn as not checked and counted
 * apart, and no doctor run at all says so rather than drawing a clean page.
 */
import { useCallback, useEffect, useState } from 'react'
import { loadSetupHealth, type HealthCheck, type HealthFinding, type Result, type SetupHealth } from './api'
import { Banner, Button, Card, Chip, CodeBlock, ToneMark, type MarkTone } from './components'
import { spanWords } from './AdminSetupSteps'
import './styles/onboarding.css'

interface ResultLook {
  tone: MarkTone
  word: string
  cls: string
}

const PASSED: ResultLook = { tone: 'ok', word: 'passed', cls: 'is-done' }
const FAILED: ResultLook = { tone: 'bad', word: 'failed', cls: 'is-failed' }
const NOT_CHECKED: ResultLook = { tone: 'unknown', word: 'not checked', cls: 'is-todo' }

const resultOf = (c: HealthCheck): ResultLook => (c.result === 'passed' ? PASSED : c.result === 'failed' ? FAILED : NOT_CHECKED)

/** One finding: what drifted, what changed, the repair command and the console path. */
export function HealthFindingPanel({ finding }: { finding: HealthFinding }) {
  return (
    <section aria-label={finding.title}>
      <Banner tone="bad" title={finding.title}>
        {finding.cause ?? `The ${finding.check} check failed on the last doctor run.`}
      </Banner>
      {finding.changed !== null && (
        <p className="ur-hint">
          <b>What changed:</b> {finding.changed}
        </p>
      )}
      <CodeBlock title="Repair" lang="shell" text={finding.repair} />
      <p className="ur-hint">
        {finding.console_path !== null ? (
          <>
            <b>In the Cloud console:</b> {finding.console_path}
          </>
        ) : (
          'No console path for this one: repair it from a terminal with the command above.'
        )}
      </p>
    </section>
  )
}

/** The doctor run as it was recorded: findings first, then every check's last result. */
export function HealthPanel({ health, now, onReload, busy }: { health: SetupHealth | null; now: number; onReload?: () => void; busy?: boolean }) {
  const reload =
    onReload === undefined ? null : (
      <Button size="sm" onClick={onReload} busy={busy === true ? 'Reading' : false}>
        Read again
      </Button>
    )
  if (health === null) {
    return (
      <Card title="Health" action={reload ?? undefined}>
        <p className="ur-hint">No doctor run has been recorded yet. The scheduled doctor runs every hour once swarm install has finished; until it reports, nothing here is known to pass. In a terminal, swarm doctor runs the same checks now.</p>
      </Card>
    )
  }
  const notChecked = health.checks.filter((c) => resultOf(c) === NOT_CHECKED).length
  const at = health.run_at === null ? Number.NaN : Date.parse(health.run_at)
  const when = Number.isNaN(at) ? 'at an unrecorded time' : `${spanWords((now - at) / 1000)} ago`
  return (
    <Card
      title="Health"
      action={
        <span className="ob-acts">
          {health.findings.length > 0 ? <Chip tone="bad">{health.findings.length} drifted</Chip> : <Chip>no findings</Chip>}
          {reload}
        </span>
      }
      foot={`The checks swarm doctor runs, read-only, on the live deployment · last run ${when} by the scheduled doctor`}
    >
      {health.findings.map((f) => (
        <HealthFindingPanel key={`${f.check}:${f.title}`} finding={f} />
      ))}
      <p className="ob-summary">
        {health.checks.length} checks · {health.checks.length - notChecked} run
        {notChecked > 0 ? ` · ${notChecked} of ${health.checks.length} not checked` : ''}
      </p>
      <ul className="ob-list" aria-label="Every check">
        {health.checks.map((c) => {
          const r = resultOf(c)
          return (
            <li key={c.id} className={`ob-step ${r.cls}`}>
              <ToneMark tone={r.tone} hidden />
              <div className="ob-step-main">
                <p className="ob-step-h">
                  <b>{c.title}</b>
                  <span className="ob-state">{r.word}</span>
                </p>
                {c.detail !== null && <p className="ur-hint ob-line">{c.detail}</p>}
              </div>
            </li>
          )
        })}
      </ul>
    </Card>
  )
}

/** Admin › Setup › Health: reads the latest doctor run, and again on request. `now` fixes the clock. */
export function AdminSetupHealth({ now }: { now?: number }) {
  const [result, setResult] = useState<Result<SetupHealth | null>>(() => ({ status: 'loading', since: Date.now() }))
  const [busy, setBusy] = useState(false)
  const load = useCallback(() => {
    setBusy(true)
    void loadSetupHealth().then((r) => {
      setBusy(false)
      setResult(r)
    })
  }, [])
  useEffect(load, [load])
  const nowMs = now ?? Date.now()
  if (result.status === 'loading') {
    return (
      <Card title="Health">
        <p className="ur-hint">Reading the latest doctor run…</p>
      </Card>
    )
  }
  if (result.status === 'error') {
    return (
      <Card title="Health">
        <Banner tone={result.error.kind === 'admin_required' ? 'warn' : 'bad'} title={result.error.kind === 'admin_required' ? 'Health is for platform admins' : 'The latest doctor run could not be read'} actions={<Button size="sm" onClick={load}>Read again</Button>}>
          {result.error.message}
        </Banner>
      </Card>
    )
  }
  return <HealthPanel health={result.status === 'empty' ? null : result.data} now={nowMs} onReload={load} busy={busy} />
}
