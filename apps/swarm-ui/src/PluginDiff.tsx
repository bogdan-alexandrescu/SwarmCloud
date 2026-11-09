/**
 * A MANAGED PLUGIN'S UPDATE, AS A DIFF (part of #2688; docs/design/managed-
 * plugins.md section 9.1, lane 10b). What changes from the installed version
 * to another, exactly as `GET .../installs/{install_id}/diff` serves it from
 * the two stored versions: plugin.json's fields, hooks, MCP servers, skills,
 * commands and agents, settings, network hosts, runtimes, the file count and
 * size, the digest and the upstream commit range. Nothing is fetched or
 * guessed here; a section with no change is left out.
 *
 * A change to hooks, MCP servers, hosts or a field's sensitivity is MARKED
 * (`diff.marked`): that is where a malicious update acts, and applying it
 * needs the plugin's name typed. The mark is drawn on the section it names.
 */
import type { ReactNode } from 'react'
import type { PluginAddedRemoved, PluginHook, PluginVersionDiff } from './api'
import { Chip } from './components'
import { InfoTip } from './People'

const MARK_TIP = 'Where a malicious update acts. Applying it needs the plugin’s name typed.'

export function shortSha(sha: string | null): string {
  if (sha === null) return '—'
  return sha.length > 12 ? sha.slice(0, 12) : sha
}

export function bytesWords(n: number | null): string {
  if (n === null) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`
}

/** One hook as the agent runs it: its event, its matcher and its command. */
export function HookLine({ h, mark }: { h: PluginHook; mark?: 'add' | 'del' }) {
  return (
    <li className={`pd-hook${mark === undefined ? '' : ` is-${mark}`}`}>
      <span className="pd-hook-ev">
        {mark !== undefined && <span className="pd-sign" aria-label={mark === 'add' ? 'Added' : 'Removed'}>{mark === 'add' ? '+' : '−'}</span>}
        <b>{h.event}</b>
        {h.matcher !== null && <code>{h.matcher}</code>}
      </span>
      <code className="pd-hook-cmd">{h.command}</code>
    </li>
  )
}

function Section({ title, marked, children }: { title: string; marked: boolean; children: ReactNode }) {
  return (
    <section className={`pd-dsec${marked ? ' is-marked' : ''}`} aria-label={title}>
      <h3>
        {title}
        {marked && (
          <>
            <span className="pd-mark">Marked</span>
            <InfoTip about={`${title.toLowerCase()}, marked`} tip={MARK_TIP} />
          </>
        )}
      </h3>
      {children}
    </section>
  )
}

function Names({ ar, changed = [] }: { ar: PluginAddedRemoved; changed?: readonly string[] }) {
  return (
    <ul className="pd-names" role="list">
      {ar.added.map((n) => <li key={`+${n}`} className="is-add"><span className="pd-sign" aria-label="Added">+</span><code>{n}</code></li>)}
      {ar.removed.map((n) => <li key={`-${n}`} className="is-del"><span className="pd-sign" aria-label="Removed">−</span><code>{n}</code></li>)}
      {changed.map((n) => <li key={`~${n}`} className="is-chg"><span className="pd-sign" aria-label="Changed">~</span><code>{n}</code></li>)}
    </ul>
  )
}

function some(ar: PluginAddedRemoved): boolean {
  return ar.added.length + ar.removed.length > 0
}

export function PluginDiff({ diff: d }: { diff: PluginVersionDiff }) {
  const marked = (k: string) => d.marked.includes(k)
  const hookCount = d.hooks.added.length + d.hooks.removed.length + d.hooks.changed.length
  return (
    <div className="pd-diff">
      {d.manifest.length > 0 && (
        <Section title="plugin.json" marked={false}>
          <dl className="pd-fromto">
            {d.manifest.map((m) => (
              <div key={m.field}>
                <dt>{m.field}</dt>
                <dd>
                  <del>{m.from ?? '—'}</del>
                  <span aria-hidden>→</span>
                  <ins>{m.to ?? '—'}</ins>
                </dd>
              </div>
            ))}
          </dl>
        </Section>
      )}
      {hookCount > 0 && (
        <Section title="Hooks" marked={marked('hooks')}>
          <ul className="pd-hooks" role="list">
            {d.hooks.added.map((h, i) => <HookLine key={`a${i}`} h={h} mark="add" />)}
            {d.hooks.removed.map((h, i) => <HookLine key={`r${i}`} h={h} mark="del" />)}
            {d.hooks.changed.map((c, i) => (
              <li key={`c${i}`} className="pd-hookpair">
                <ul role="list" className="pd-hooks">
                  <HookLine h={c.from} mark="del" />
                  <HookLine h={c.to} mark="add" />
                </ul>
              </li>
            ))}
          </ul>
        </Section>
      )}
      {(some(d.mcp_servers) || d.mcp_servers.changed.length > 0) && (
        <Section title="MCP servers" marked={marked('mcp_servers')}>
          <Names ar={d.mcp_servers} changed={d.mcp_servers.changed} />
        </Section>
      )}
      {some(d.hosts) && (
        <Section title="Network hosts" marked={marked('hosts')}>
          <Names ar={d.hosts} />
        </Section>
      )}
      {(some(d.settings) || d.settings.retyped.length + d.settings.newly_sensitive.length > 0) && (
        <Section title="Settings" marked={marked('sensitive')}>
          <Names ar={d.settings} />
          {d.settings.retyped.length > 0 && (
            <p className="pd-line"><span className="pd-k">Retyped</span>{d.settings.retyped.map((k) => <code key={k}>{k}</code>)}</p>
          )}
          {d.settings.newly_sensitive.length > 0 && (
            <p className="pd-line"><span className="pd-k">Now secret</span>{d.settings.newly_sensitive.map((k) => <code key={k}>{k}</code>)}</p>
          )}
        </Section>
      )}
      {(['skills', 'commands', 'agents'] as const).filter((k) => some(d[k])).map((k) => (
        <Section key={k} title={k[0]!.toUpperCase() + k.slice(1)} marked={false}>
          <Names ar={d[k]} />
        </Section>
      ))}
      {d.runtimes !== null && (
        <Section title="Runtimes" marked={false}>
          <p className="pd-line">
            {d.runtimes.from.length === 0 ? '—' : d.runtimes.from.map((r) => <Chip key={`f${r}`}>{r}</Chip>)}
            <span aria-hidden>→</span>
            {d.runtimes.to.length === 0 ? '—' : d.runtimes.to.map((r) => <Chip key={`t${r}`}>{r}</Chip>)}
          </p>
        </Section>
      )}
      <dl className="pd-fromto pd-copy" aria-label="The pinned copy">
        <div>
          <dt>Files</dt>
          <dd>{d.files.from ?? '—'} <span aria-hidden>→</span> {d.files.to ?? '—'} · {bytesWords(d.unpacked_bytes.from)} <span aria-hidden>→</span> {bytesWords(d.unpacked_bytes.to)}</dd>
        </div>
        <div>
          <dt>sha256</dt>
          <dd><code title={d.runnable_sha256.from ?? undefined}>{shortSha(d.runnable_sha256.from)}</code> <span aria-hidden>→</span> <code title={d.runnable_sha256.to ?? undefined}>{shortSha(d.runnable_sha256.to)}</code></dd>
        </div>
        {(d.source.from_commit !== null || d.source.to_commit !== null) && (
          <div>
            <dt>Commit</dt>
            <dd>
              <code>{shortSha(d.source.from_commit)}</code> <span aria-hidden>→</span> <code>{shortSha(d.source.to_commit)}</code>
              {d.source.compare !== null && <a href={d.source.compare} target="_blank" rel="noreferrer">Compare</a>}
            </dd>
          </div>
        )}
      </dl>
    </div>
  )
}
