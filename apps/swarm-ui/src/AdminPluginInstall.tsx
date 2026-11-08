/**
 * ADMIN › PLUGINS › ADD PLUGIN (part of #2688; docs/design/managed-plugins.md
 * §2, §6.3, §17 lane 10a; the owner's cards direction of 6 November 2026).
 * A full page: the source and the overlay on the left, a live preview on the
 * right, then the intake's progress and its outcome in the same panel.
 *
 * THE SOURCE (§2.1): a GitHub repository at a tag, branch or commit with an
 * optional folder; a Claude Code marketplace entry; or one uploaded `.zip`,
 * `.tar.gz` or `.tgz`. Nothing else is offered, so nothing else is sent. A
 * GitHub or marketplace source is one `POST .../installs`: swarm-api resolves
 * the ref, fetches the archive into quarantine and starts the intake job. An
 * upload is `POST .../uploads` (an intake and a signed URL), the bytes PUT
 * straight to Cloud Storage, then `POST .../intakes/{id}/start`.
 *
 * THE OVERLAY (§6.3) is what a plugin with no `plugin.json` needs: its
 * manifest, hooks whose command is a runtime and a path inside the plugin,
 * declared settings (an environment variable or a file) and what it requires.
 * The preview draws the document exactly as it is sent, as it is typed; the
 * intake job checks it, and swarm-api never takes a command or an image from
 * it (invariant 10: the hook's command is a §5 runtime and a plugin path).
 *
 * PROGRESS AND REFUSALS (§2.3). The intake is read until it is pinned or
 * refused; nothing waits on the job server-side (invariant 4). A refusal is a
 * fixed code: the sentence drawn is this console's own for that code, never
 * text from the archive. `INTAKE_REFUSALS` holds both tables, swarm-api's
 * (`pluginstore.REFUSALS`) and the job's (`plugin_intake.refusals`), and
 * adminPlugins.test.tsx holds it equal to them.
 *
 * Once pinned, the preview is the stored version itself (`GET
 * .../installs/{id}/versions`): what the intake read, not what was typed.
 */
import { useEffect, useState, type ReactNode } from 'react'
import {
  loadPluginCatalogue,
  loadPluginIntake,
  loadPluginVersions,
  openPluginUpload,
  PLATFORM_PLUGIN_CATALOGUE,
  putPluginArchive,
  startPluginInstall,
  startPluginIntake,
  type PluginCatalogue,
  type PluginCatalogueScope,
  type PluginIntake,
  type PluginVersion,
} from './api'
import { ADMIN_PLUGINS_PATH, adminPluginPath, Badges, Mono } from './AdminPlugins'
import { Banner, Breadcrumb, Button, ButtonLink, Chip, CodeBlock, Segmented } from './components'
import type { ApiError } from './fetch'
import { InfoTip } from './People'
import { UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import './styles/admin-plugins.css'

const ROUTE = 'GET /v1/admin/plugin-catalogue'

/** How often a running intake is read again; the job's own timeout is ten minutes. */
export const INTAKE_POLL_MS = 2000

/**
 * Every refusal, by its fixed code: swarm-api's `pluginstore.REFUSALS` and
 * the intake job's `plugin_intake.refusals.REFUSALS`, word for word (the two
 * agree where they share a code). adminPlugins.test.tsx reads both Python
 * tables and fails when this one differs.
 */
export const INTAKE_REFUSALS: Readonly<Record<string, string>> = {
  // swarm-api: the request, the source, the store. Where both tables hold a
  // code (`subdir_invalid`), swarm-api's sentence is the one its intake view
  // serves, so it is the one drawn.
  settings_key_missing: 'This deployment has no settings key, so no plugin can be installed.',
  source_kind_refused: 'A plugin comes from a GitHub repository, a marketplace entry or an uploaded archive.',
  repository_invalid: 'The repository is not an owner/name GitHub repository.',
  ref_invalid: 'The ref is not a tag, a branch or a commit name.',
  subdir_invalid: 'The folder is not a relative path inside the repository.',
  ref_not_found: 'The ref does not name a commit in the repository.',
  source_unreachable: 'GitHub could not be read; nothing was installed.',
  marketplace_missing: 'The repository has no readable .claude-plugin/marketplace.json at that commit.',
  marketplace_plugin_missing: 'The marketplace does not list a plugin of that name.',
  marketplace_source_npm: 'The marketplace entry installs from npm, which this release refuses.',
  marketplace_source_pip: 'The marketplace entry installs from pip, which this release refuses.',
  marketplace_source_git: 'The marketplace entry names a bare git URL, which this release refuses.',
  marketplace_source_unknown: "The marketplace entry's source is not a path or a GitHub repository.",
  archive_too_large: 'The archive is larger than 100 MiB.',
  upload_name_invalid: 'An upload is one .zip, .tar.gz or .tgz file.',
  upload_missing: 'No uploaded archive was found for this intake.',
  overlay_too_large: 'The overlay is larger than 64 KiB.',
  plugin_name_taken: 'An installed plugin in this catalogue already has that name; update it instead.',
  plugin_removed: 'The plugin was removed from the catalogue.',
  plugin_built_in: 'A built-in plugin is updated by an image release and cannot be removed.',
  intake_start_failed: 'The intake job could not be started.',
  intake_timed_out: 'The intake job ended without a report.',
  intake_unverified: "The intake's record does not verify.",
  intake_digest_mismatch: "The staged copy's digest or size is not the one its report states.",
  report_invalid: "The intake job's report is not a valid report.",
  report_mismatch: "The intake job's report is bound to another intake.",
  report_limits_differ: "The intake job's report was made under other limits.",
  pinned_copy_differs: 'A different copy is already stored under that digest.',
  install_not_active: 'The plugin is not an active install.',
  version_not_found: "That version is not one of the plugin's versions.",
  version_is_current: 'That version is already the current one.',
  diff_changed: 'The diff changed since it was shown; read it again before applying.',
  confirmation_required: "This update changes hooks, servers, hosts or secret fields: type the plugin's name to apply it.",
  confirmation_mismatch: "The name typed is not the plugin's name.",
  // The intake job: the archive as a whole.
  archive_format_unknown: 'The archive is not a .zip or a .tar.gz file.',
  archive_corrupt: 'The archive cannot be read.',
  archive_empty: 'The archive holds no files.',
  unpacked_too_large: 'The archive unpacks to more than 500 MiB.',
  too_many_files: 'The archive holds more than 20,000 files.',
  expansion_ratio: 'The archive expands more than 100 times its packed size.',
  // One entry's name, kind and mode.
  entry_path_absolute: "An entry's path is absolute.",
  entry_path_parent: "An entry's path has a '..' segment.",
  entry_path_invalid: "An entry's path holds a backslash, a NUL, a drive letter or bytes that are not UTF-8.",
  entry_path_too_deep: "An entry's path has more than 32 segments.",
  entry_path_segment_too_long: "An entry's path has a segment longer than 255 bytes.",
  entry_duplicate: 'Two entries have the same path, ignoring case and Unicode normalisation.',
  entry_special_file: 'An entry is a device, a FIFO or a socket.',
  entry_type_unsupported: 'An entry has a type the intake does not accept.',
  entry_setuid: 'An entry has the setuid or setgid bit.',
  entry_encrypted: 'An entry is encrypted.',
  entry_under_link: "An entry's path passes through a symbolic link.",
  entry_escapes_root: "An entry's path leaves the plugin's folder.",
  link_escapes_root: "A symbolic link points outside the plugin's folder.",
  link_target_missing: 'A symbolic link points to nothing in the archive.',
  link_to_link: 'A symbolic link points to another symbolic link.',
  hardlink_escapes_root: "A hard link points outside the plugin's folder or to something that is not a file.",
  // The plugin root, its dependencies, its manifest and components.
  subdir_missing: 'The requested folder is not in the archive.',
  package_json_invalid: 'A package.json is not a JSON object.',
  dependencies_not_shipped: 'A package.json lists dependencies but no node_modules folder is beside it.',
  manifest_invalid: "The plugin's .claude-plugin/plugin.json is not a valid manifest.",
  manifest_user_config_invalid: "The plugin's userConfig has a field the intake does not accept.",
  manifest_component_unknown: 'The plugin declares a component in a shape the intake does not know.',
  manifest_path_outside_root: "The plugin's manifest names a path outside the plugin's folder.",
  manifest_path_missing: "The plugin's manifest names a path that is not in the archive.",
  hooks_invalid: "The plugin's hooks are not valid hook declarations.",
  hook_unknown: 'The plugin declares a hook event or type the intake does not accept.',
  hook_path_missing: 'A hook names a file in the plugin that is not there.',
  mcp_invalid: "The plugin's MCP servers are not valid server declarations.",
  mcp_path_missing: 'An MCP server names a file in the plugin that is not there.',
  skill_invalid: "A skill's SKILL.md cannot be read.",
  // The admin's overlay (§6.3).
  overlay_invalid: 'The overlay is not a valid overlay.',
  overlay_replaces_manifest: 'The overlay declares a manifest but the plugin ships its own.',
  overlay_replaces_hooks: 'The overlay declares hooks but the plugin ships its own.',
  overlay_conflict: 'The plugin already holds a file the overlay writes.',
  overlay_variable_denied: 'An overlay setting names an environment variable no plugin may set.',
  overlay_hook_path_missing: 'An overlay hook names a file that is not in the plugin.',
  manifest_missing: 'The plugin has no .claude-plugin/plugin.json and the overlay declares none.',
  intake_internal_error: 'The intake job failed before it finished its checks.',
}

/** The archive's PUT to the signed URL failed: a console code, since no route answered. */
export const UPLOAD_NOT_STORED = 'upload_not_stored'
const CONSOLE_REFUSALS: Readonly<Record<string, string>> = {
  [UPLOAD_NOT_STORED]: 'Cloud Storage did not take the archive; nothing was installed.',
}

/** A refusal drawn: the code and its fixed sentence. */
export interface Refusal {
  code: string | null
  sentence: string
  entry_index: number | null
}

/**
 * The sentence for a code: the console's own; else swarm-api's sentence for
 * it (fixed text the route chose, never the archive's); else the code alone.
 */
export function refusalOf(code: string | null, served: string | null, entryIndex: number | null = null): Refusal {
  const own = code === null ? undefined : (INTAKE_REFUSALS[code] ?? CONSOLE_REFUSALS[code])
  return { code, sentence: own ?? served ?? 'Refused.', entry_index: entryIndex }
}

function refusalOfError(e: ApiError): Refusal {
  return refusalOf(e.code, e.message)
}

/** The hook events and runtimes the intake accepts (plugin_intake.manifest HOOK_EVENTS, RUNTIMES). */
export const HOOK_EVENTS = [
  'PreToolUse',
  'PostToolUse',
  'PostToolUseFailure',
  'Notification',
  'UserPromptSubmit',
  'Stop',
  'SubagentStart',
  'SubagentStop',
  'PreCompact',
  'SessionStart',
  'SessionEnd',
  'PermissionRequest',
] as const
export const RUNTIMES = ['node', 'bun', 'python3', 'bash'] as const

const PLUGIN_ROOT = '${CLAUDE_PLUGIN_ROOT}/'

export type SourceKind = 'github' | 'marketplace' | 'upload'

export interface OverlayHook {
  event: string
  matcher: string
  timeout: string
  runtime: string
  path: string
  args: string
}

export interface OverlaySetting {
  key: string
  type: 'string' | 'number' | 'boolean' | 'file'
  target: 'env' | 'file'
  variable: string
  sensitive: boolean
  description: string
}

/** The overlay as the form holds it; `overlayDocument` turns it into what is sent. */
export interface OverlayForm {
  /** The source ships no `.claude-plugin/plugin.json`: the overlay declares one. */
  manifest: boolean
  name: string
  version: string
  description: string
  hooks: OverlayHook[]
  settings: OverlaySetting[]
  runtimes: string[]
  hosts: string
}

export const EMPTY_OVERLAY: OverlayForm = { manifest: false, name: '', version: '', description: '', hooks: [], settings: [], runtimes: [], hosts: '' }

/** A hook's command as §6.3 allows it: a runtime, a path inside the plugin, fixed arguments. */
export function hookCommand(h: Pick<OverlayHook, 'runtime' | 'path' | 'args'>): string {
  const path = h.path.trim().replace(/^\/+/, '')
  const args = h.args.trim().split(/\s+/).filter((a) => a !== '')
  return [h.runtime, `${PLUGIN_ROOT}${path}`, ...args].join(' ')
}

/** The overlay document sent with the install, or null when the form declares nothing. */
export function overlayDocument(f: OverlayForm): Record<string, unknown> | null {
  const doc: Record<string, unknown> = {}
  if (f.manifest) {
    const plugin: Record<string, string> = { name: f.name.trim() }
    if (f.version.trim() !== '') plugin.version = f.version.trim()
    if (f.description.trim() !== '') plugin.description = f.description.trim()
    doc.plugin = plugin
  }
  if (f.hooks.length > 0) {
    doc.hooks = f.hooks.map((h) => {
      const hook: Record<string, unknown> = { event: h.event, command: hookCommand(h) }
      if (h.matcher.trim() !== '') hook.matcher = h.matcher.trim()
      if (h.timeout.trim() !== '') hook.timeout = Number(h.timeout)
      return hook
    })
  }
  if (f.settings.length > 0) {
    doc.settings = f.settings.map((s) => {
      const setting: Record<string, unknown> = { key: s.key.trim(), type: s.type, target: s.target, variable: s.variable.trim(), sensitive: s.sensitive }
      if (s.description.trim() !== '') setting.description = s.description.trim()
      return setting
    })
  }
  const hosts = f.hosts.split(/[\s,]+/).filter((h) => h !== '')
  if (f.runtimes.length > 0 || hosts.length > 0) {
    doc.requires = { runtimes: f.runtimes.map((name) => ({ name })), hosts }
  }
  return Object.keys(doc).length === 0 ? null : doc
}

/** What blocks sending, in a few words; null when the form can go. */
export function missingInput(kind: SourceKind, src: { repository: string; ref: string; plugin: string; file: File | null }, o: OverlayForm): string | null {
  if (kind === 'upload' && src.file === null) return 'Choose an archive'
  if (kind !== 'upload' && src.repository.trim() === '') return 'Name the repository'
  if (kind !== 'upload' && src.ref.trim() === '') return 'Name a tag, branch or commit'
  if (kind === 'marketplace' && src.plugin.trim() === '') return 'Name the plugin'
  if (o.manifest && o.name.trim() === '') return 'Name the plugin in its manifest'
  if (o.hooks.some((h) => h.path.trim() === '')) return 'Give each hook a file'
  if (o.settings.some((s) => s.key.trim() === '' || s.variable.trim() === '')) return 'Give each setting a key and a variable'
  return null
}

const SOURCE_WORDS: Readonly<Record<SourceKind, string>> = { github: 'GitHub', marketplace: 'Marketplace', upload: 'Upload' }

/* ---------------------------------------------------------------- the page */

export function AdminPluginInstallScreen({
  scope = PLATFORM_PLUGIN_CATALOGUE,
  backHref = ADMIN_PLUGINS_PATH,
  pollMs = INTAKE_POLL_MS,
}: {
  scope?: PluginCatalogueScope
  backHref?: string
  pollMs?: number
}) {
  const read = useUrRead(() => loadPluginCatalogue(scope), `plugin-add-${scope.kind === 'platform' ? 'platform' : scope.tenantId}`)
  return (
    <div className="ap-page">
      <Breadcrumb
        crumbs={[
          { key: 'plugins', label: 'Plugins', href: backHref },
          { key: 'add', label: 'Add plugin', href: null },
        ]}
      />
      <PageHead title="Add plugin" />
      <UrRegion state={read.state} route={ROUTE} what="The plugin catalogue" onRetry={read.reload}>
        {(d) =>
          d.can_install ? (
            <AddPlugin scope={scope} catalogue={d} backHref={backHref} pollMs={pollMs} />
          ) : (
            <Banner tone="warn" title="Admin only" role="status" />
          )
        }
      </UrRegion>
    </div>
  )
}

function AddPlugin({ scope, catalogue, backHref, pollMs }: { scope: PluginCatalogueScope; catalogue: PluginCatalogue; backHref: string; pollMs: number }) {
  const [kind, setKind] = useState<SourceKind>('github')
  const [repository, setRepository] = useState('')
  const [ref, setRef] = useState('')
  const [subdir, setSubdir] = useState('')
  const [plugin, setPlugin] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [overlay, setOverlay] = useState<OverlayForm>(EMPTY_OVERLAY)
  const [sending, setSending] = useState(false)
  const [intake, setIntake] = useState<PluginIntake | null>(null)
  const [refusal, setRefusal] = useState<Refusal | null>(null)
  const [pinned, setPinned] = useState<PluginVersion | null>(null)

  const doc = overlayDocument(overlay)
  const missing = missingInput(kind, { repository, ref, plugin, file }, overlay)
  const limit = catalogue.limits?.max_packed_bytes ?? null
  const tooLarge = kind === 'upload' && file !== null && limit !== null && file.size > limit
  const started = sending || intake !== null || refusal !== null
  const ended = refusal !== null || intake?.state === 'pinned' || intake?.state === 'refused'

  // A running intake is read again until it ends; the read itself finishes it once its report is in.
  useEffect(() => {
    if (intake === null || intake.state !== 'running') return
    let live = true
    const t = setTimeout(() => {
      void loadPluginIntake(scope, intake.intake_id).then((r) => {
        if (!live) return
        if (r.status === 'ok' || r.status === 'stale') {
          if (r.data !== null) setIntake(r.data)
        } else if (r.status === 'error') {
          setRefusal(refusalOfError(r.error))
        }
      })
    }, pollMs)
    return () => {
      live = false
      clearTimeout(t)
    }
  }, [intake, pollMs, scope])

  // A refused intake is drawn by its code; a pinned one by its stored version.
  useEffect(() => {
    if (intake?.state === 'refused' && intake.refusal !== null) {
      setRefusal(refusalOf(intake.refusal.code, intake.refusal.sentence, intake.refusal.entry_index))
    }
    if (intake?.state !== 'pinned' || intake.install_id === null) return
    let live = true
    const versionId = intake.version_id
    void loadPluginVersions(scope, intake.install_id).then((r) => {
      if (!live || (r.status !== 'ok' && r.status !== 'stale') || r.data === null) return
      setPinned(r.data.versions.find((v) => v.version_id === versionId) ?? r.data.versions.find((v) => v.current) ?? null)
    })
    return () => {
      live = false
    }
  }, [intake, scope])

  async function send() {
    setSending(true)
    setRefusal(null)
    setIntake(null)
    setPinned(null)
    try {
      if (kind === 'upload') {
        if (file === null) return
        const opened = await openPluginUpload(scope, { name: file.name, size: file.size }, subdir.trim(), doc)
        if (opened.status === 'error') return setRefusal(refusalOfError(opened.error))
        if ((opened.status !== 'ok' && opened.status !== 'stale') || opened.data === null) return setRefusal(refusalOf(null, null))
        setIntake(opened.data.intake)
        if (!(await putPluginArchive(opened.data.upload, file))) return setRefusal(refusalOf(UPLOAD_NOT_STORED, null))
        const run = await startPluginIntake(scope, opened.data.intake.intake_id)
        if (run.status === 'error') return setRefusal(refusalOfError(run.error))
        if ((run.status === 'ok' || run.status === 'stale') && run.data !== null) setIntake(run.data)
        return
      }
      const r = await startPluginInstall(scope, { kind, repository: repository.trim(), ref: ref.trim(), subdir: subdir.trim(), plugin: kind === 'marketplace' ? plugin.trim() : null }, doc)
      if (r.status === 'error') return setRefusal(refusalOfError(r.error))
      if ((r.status === 'ok' || r.status === 'stale') && r.data !== null) setIntake(r.data)
    } finally {
      setSending(false)
    }
  }

  function again() {
    setIntake(null)
    setRefusal(null)
    setPinned(null)
  }

  const locked = started && !ended
  return (
    <>
      <div className="ap-add">
        <section className="ap-panel" aria-label="Source">
          <div className="ap-field">
            <span className="ap-lbl">Source</span>
            <Segmented<SourceKind>
              label="Source"
              value={kind}
              onChange={(k) => !locked && setKind(k)}
              disabled={locked}
              options={(['github', 'marketplace', 'upload'] as const).map((k) => ({ key: k, label: SOURCE_WORDS[k] }))}
            />
          </div>
          {kind !== 'upload' && (
            <>
              <Field id="ap-repo" label={kind === 'marketplace' ? 'Marketplace' : 'Repository'} tip={kind === 'marketplace' ? 'An owner/name repository holding .claude-plugin/marketplace.json.' : 'An owner/name GitHub repository. A tag or branch is resolved once to a commit, and the copy is pinned by its sha256.'}>
                <input id="ap-repo" className="c-inp is-mono" value={repository} disabled={locked} placeholder="owner/name" autoComplete="off" spellCheck={false} onChange={(e) => setRepository(e.target.value)} />
              </Field>
              <div className="ap-row2">
                <Field id="ap-ref" label="Tag, branch or commit">
                  <input id="ap-ref" className="c-inp is-mono" value={ref} disabled={locked} autoComplete="off" spellCheck={false} onChange={(e) => setRef(e.target.value)} />
                </Field>
                {kind === 'marketplace' ? (
                  <Field id="ap-entry" label="Plugin" tip="The entry's name in marketplace.json. An entry that installs from npm, pip or a bare git URL is refused.">
                    <input id="ap-entry" className="c-inp is-mono" value={plugin} disabled={locked} autoComplete="off" spellCheck={false} onChange={(e) => setPlugin(e.target.value)} />
                  </Field>
                ) : (
                  <Field id="ap-subdir" label="Folder" optional>
                    <input id="ap-subdir" className="c-inp is-mono" value={subdir} disabled={locked} autoComplete="off" spellCheck={false} onChange={(e) => setSubdir(e.target.value)} />
                  </Field>
                )}
              </div>
            </>
          )}
          {kind === 'upload' && (
            <>
              <Field id="ap-file" label="Archive" tip={`One .zip, .tar.gz or .tgz${limit === null ? '' : `, at most ${Math.round(limit / 1048576)} MiB`}. It goes straight to storage and is checked there, never unpacked by the API.`}>
                <input id="ap-file" type="file" className="ap-file" accept=".zip,.tar.gz,.tgz" disabled={locked} onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
              </Field>
              {tooLarge && <span className="ap-err" role="alert">{INTAKE_REFUSALS.archive_too_large}</span>}
              <Field id="ap-subdir" label="Folder" optional>
                <input id="ap-subdir" className="c-inp is-mono" value={subdir} disabled={locked} autoComplete="off" spellCheck={false} onChange={(e) => setSubdir(e.target.value)} />
              </Field>
            </>
          )}
          <OverlayEditor value={overlay} onChange={setOverlay} disabled={locked} />
        </section>
        <section className="ap-panel ap-preview" aria-label="Preview" aria-busy={locked || undefined}>
          {started ? (
            <IntakeProgress kind={kind} sending={sending} intake={intake} refusal={refusal} />
          ) : null}
          {refusal !== null ? (
            <RefusalBanner refusal={refusal} />
          ) : pinned !== null ? (
            <PinnedPreview v={pinned} />
          ) : (
            <LivePreview kind={kind} source={{ repository, ref, subdir, plugin, file }} doc={doc} />
          )}
        </section>
      </div>
      <div className="ap-installbar">
        <span className="ap-small ap-dim">{missing ?? (tooLarge ? null : `${SOURCE_WORDS[kind]}${doc === null ? '' : ' · overlay'}`)}</span>
        <span className="ap-acts">
          {ended ? (
            <>
              {pinned !== null && intake?.install_id != null && (
                <ButtonLink kind="secondary" href={adminPluginPath(intake.install_id)}>
                  Open
                </ButtonLink>
              )}
              <Button kind={pinned === null ? 'primary' : 'ghost'} onClick={again}>
                {pinned === null ? 'Try again' : 'Add another'}
              </Button>
            </>
          ) : (
            <>
              <a className="c-btn is-ghost" href={backHref}>
                Cancel
              </a>
              <Button kind="primary" busy={locked ? 'Installing…' : false} disabled={missing !== null || tooLarge} onClick={() => void send()}>
                Install
              </Button>
            </>
          )}
        </span>
      </div>
    </>
  )
}

function Field({ id, label, tip, optional = false, children }: { id: string; label: string; tip?: string; optional?: boolean; children: ReactNode }) {
  return (
    <div className="ap-field">
      <span className="ap-lbl">
        <label htmlFor={id}>{label}</label>
        {optional && <span className="ap-dim">optional</span>}
        {tip !== undefined && <InfoTip about={label} tip={tip} />}
      </span>
      {children}
    </div>
  )
}

/* ---------------------------------------------------------------- the overlay */

function OverlayEditor({ value, onChange, disabled }: { value: OverlayForm; onChange: (o: OverlayForm) => void; disabled: boolean }) {
  const set = (patch: Partial<OverlayForm>) => onChange({ ...value, ...patch })
  const hook = (i: number, patch: Partial<OverlayHook>) => set({ hooks: value.hooks.map((h, j) => (j === i ? { ...h, ...patch } : h)) })
  const setting = (i: number, patch: Partial<OverlaySetting>) => set({ settings: value.settings.map((s, j) => (j === i ? { ...s, ...patch } : s)) })
  return (
    <fieldset className="ap-overlay" disabled={disabled}>
      <legend className="ap-lbl">
        Overlay
        <InfoTip about="the overlay" tip="For a plugin with no plugin.json, or one that reads environment variables and files: its manifest, hooks, declared settings and what it needs. It adds to what the plugin ships and never replaces its hooks." />
      </legend>
      <label className="ap-optin">
        <input type="checkbox" checked={value.manifest} onChange={(e) => set({ manifest: e.target.checked })} />
        No plugin.json
      </label>
      {value.manifest && (
        <div className="ap-row3">
          <Field id="ap-o-name" label="Name">
            <input id="ap-o-name" className="c-inp is-mono" value={value.name} autoComplete="off" spellCheck={false} onChange={(e) => set({ name: e.target.value })} />
          </Field>
          <Field id="ap-o-version" label="Version" optional>
            <input id="ap-o-version" className="c-inp is-mono" value={value.version} autoComplete="off" spellCheck={false} onChange={(e) => set({ version: e.target.value })} />
          </Field>
          <Field id="ap-o-desc" label="Description" optional>
            <input id="ap-o-desc" className="c-inp" value={value.description} autoComplete="off" onChange={(e) => set({ description: e.target.value })} />
          </Field>
        </div>
      )}

      <div className="ap-sub">
        <span className="ap-lbl">
          Hooks <em>{value.hooks.length}</em>
          <InfoTip about="overlay hooks" tip="Only for a plugin that ships no hooks: a runtime, a file inside the plugin and fixed arguments, never a shell line." />
        </span>
        {value.hooks.map((h, i) => (
          <div key={i} className="ap-item" role="group" aria-label={`Hook ${i + 1}`}>
            <select className="c-inp" aria-label="Event" value={h.event} onChange={(e) => hook(i, { event: e.target.value })}>
              {HOOK_EVENTS.map((ev) => (
                <option key={ev} value={ev}>
                  {ev}
                </option>
              ))}
            </select>
            <input className="c-inp is-mono" aria-label="Matcher" placeholder="matcher" value={h.matcher} onChange={(e) => hook(i, { matcher: e.target.value })} />
            <select className="c-inp" aria-label="Runtime" value={h.runtime} onChange={(e) => hook(i, { runtime: e.target.value })}>
              {RUNTIMES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
            <input className="c-inp is-mono" aria-label="File in the plugin" placeholder="bin/hook.mjs" value={h.path} onChange={(e) => hook(i, { path: e.target.value })} />
            <input className="c-inp is-mono" aria-label="Arguments" placeholder="args" value={h.args} onChange={(e) => hook(i, { args: e.target.value })} />
            <input className="c-inp is-num" aria-label="Timeout in seconds" type="number" min={1} max={600} placeholder="s" value={h.timeout} onChange={(e) => hook(i, { timeout: e.target.value })} />
            <Button kind="ghost" size="sm" onClick={() => set({ hooks: value.hooks.filter((_h, j) => j !== i) })}>
              Remove
            </Button>
          </div>
        ))}
        <Button kind="ghost" size="sm" onClick={() => set({ hooks: [...value.hooks, { event: 'PreToolUse', matcher: '', timeout: '', runtime: 'node', path: '', args: '' }] })}>
          Add hook
        </Button>
      </div>

      <div className="ap-sub">
        <span className="ap-lbl">
          Settings <em>{value.settings.length}</em>
          <InfoTip about="declared settings" tip="Each workspace sets the value. An environment variable, or a file whose path goes in the variable; a sensitive one is kept in Secret Manager." />
        </span>
        {value.settings.map((s, i) => (
          <div key={i} className="ap-item" role="group" aria-label={`Setting ${i + 1}`}>
            <input className="c-inp is-mono" aria-label="Key" placeholder="key" value={s.key} onChange={(e) => setting(i, { key: e.target.value })} />
            <select className="c-inp" aria-label="Type" value={s.type} onChange={(e) => {
              const type = e.target.value as OverlaySetting['type']
              setting(i, { type, target: type === 'file' ? 'file' : 'env' })
            }}>
              {(['string', 'number', 'boolean', 'file'] as const).map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
            <input className="c-inp is-mono" aria-label="Environment variable" placeholder="VARIABLE" value={s.variable} onChange={(e) => setting(i, { variable: e.target.value })} />
            <input className="c-inp" aria-label="Description" placeholder="description" value={s.description} onChange={(e) => setting(i, { description: e.target.value })} />
            <label className="ap-optin">
              <input type="checkbox" checked={s.sensitive} onChange={(e) => setting(i, { sensitive: e.target.checked })} />
              Sensitive
            </label>
            <Button kind="ghost" size="sm" onClick={() => set({ settings: value.settings.filter((_s, j) => j !== i) })}>
              Remove
            </Button>
          </div>
        ))}
        <Button kind="ghost" size="sm" onClick={() => set({ settings: [...value.settings, { key: '', type: 'string', target: 'env', variable: '', sensitive: false, description: '' }] })}>
          Add setting
        </Button>
      </div>

      <div className="ap-sub">
        <span className="ap-lbl">
          Needs
          <InfoTip about="what it needs" tip="The runtimes its hooks run on and the hosts it reaches; a step whose runner profile lacks a runtime is refused." />
        </span>
        <div className="ap-chips" role="group" aria-label="Runtimes">
          {RUNTIMES.map((r) => (
            <label key={r} className="ap-optin">
              <input type="checkbox" checked={value.runtimes.includes(r)} onChange={(e) => set({ runtimes: e.target.checked ? [...value.runtimes, r] : value.runtimes.filter((x) => x !== r) })} />
              {r}
            </label>
          ))}
        </div>
        <input className="c-inp is-mono" aria-label="Hosts" placeholder="api.example.com" value={value.hosts} onChange={(e) => set({ hosts: e.target.value })} />
      </div>
    </fieldset>
  )
}

/* ---------------------------------------------------------------- the preview */

function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="ap-facts">
      {rows.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  )
}

function HookRow({ event, matcher, command }: { event: string; matcher: string | null; command: string }) {
  return (
    <li className="ap-hook">
      <span className="ap-hook-ev">
        <b>{event}</b>
        {matcher !== null && matcher !== '' && <code>{matcher}</code>}
      </span>
      <code className="ap-hook-cmd">{command}</code>
    </li>
  )
}

/** What will be sent, drawn as it is typed: the source, and the overlay document word for word. */
function LivePreview({ kind, source, doc }: { kind: SourceKind; source: { repository: string; ref: string; subdir: string; plugin: string; file: File | null }; doc: Record<string, unknown> | null }) {
  const plugin = (doc?.plugin ?? null) as { name?: string; version?: string; description?: string } | null
  const hooks = (Array.isArray(doc?.hooks) ? doc.hooks : []) as { event: string; matcher?: string; command: string }[]
  const where =
    kind === 'upload'
      ? source.file === null
        ? '—'
        : `${source.file.name} · ${Math.max(1, Math.round(source.file.size / 1024))} KiB`
      : `${source.repository.trim() || '—'}@${source.ref.trim() || '—'}${kind === 'marketplace' ? ` · ${source.plugin.trim() || '—'}` : source.subdir.trim() === '' ? '' : ` · ${source.subdir.trim()}`}`
  return (
    <div className="ap-man" aria-label="Live preview">
      <header className="ap-man-h">
        <Mono name={plugin?.name || (kind === 'upload' ? source.file?.name ?? '?' : source.repository.split('/').pop() || '?')} scope="platform" />
        <span className="ap-card-id">
          <b>{plugin?.name || '—'}</b>
          <code>{plugin?.version ?? '—'}</code>
        </span>
        <span className="ap-small ap-dim">{plugin === null ? 'plugin.json from the source' : 'plugin.json from the overlay'}</span>
      </header>
      <Facts rows={[[SOURCE_WORDS[kind], <code className="ap-wrap">{where}</code>]]} />
      {hooks.length > 0 && (
        <div className="ap-man-sec">
          <h3>
            Hooks <em>{hooks.length}</em>
          </h3>
          <ul className="ap-hooks" role="list">
            {hooks.map((h, i) => (
              <HookRow key={i} event={h.event} matcher={h.matcher ?? null} command={h.command} />
            ))}
          </ul>
        </div>
      )}
      {doc !== null && <CodeBlock title="swarm-overlay.json" lang="json" text={JSON.stringify(doc, null, 2)} />}
    </div>
  )
}

/** The stored version: what the intake read and pinned. */
function PinnedPreview({ v }: { v: PluginVersion }) {
  const m = v.manifest
  const text = (k: string) => (typeof m[k] === 'string' ? (m[k] as string) : null)
  const name = text('name') ?? '—'
  const c = v.components
  return (
    <div className="ap-man" aria-label="Pinned version">
      <header className="ap-man-h">
        <Mono name={name} scope="platform" />
        <span className="ap-card-id">
          <b>{name}</b>
          <code>{v.version ?? text('version') ?? '—'}</code>
        </span>
        {text('license') !== null && <Chip>{text('license')!}</Chip>}
        <span className="ap-tick is-ok">Pinned</span>
      </header>
      {text('description') !== null && <span className="ap-card-desc">{text('description')}</span>}
      <Badges c={{ skills: c.skills.length, commands: c.commands.length, agents: c.agents.length, hooks: c.hooks.length, mcp_servers: c.mcp_servers.length }} />
      {c.hooks.length > 0 && (
        <div className="ap-man-sec">
          <h3>
            Hooks <em>{c.hooks.length}</em>
          </h3>
          <ul className="ap-hooks" role="list">
            {c.hooks.map((h, i) => (
              <HookRow key={i} event={h.event} matcher={h.matcher} command={h.command} />
            ))}
          </ul>
        </div>
      )}
      <Facts
        rows={[
          ...(c.mcp_servers.length > 0 ? ([['MCP', <>{c.mcp_servers.map((s) => <Chip key={s.name}>{s.name}</Chip>)}</>]] as [string, ReactNode][]) : []),
          ...(c.skills.length > 0 ? ([['Skills', <>{c.skills.map((s) => <Chip key={s.name}>{s.name}</Chip>)}</>]] as [string, ReactNode][]) : []),
          ['Needs', v.runtimes.length + v.hosts.length === 0 ? '—' : <>{v.runtimes.map((r) => <Chip key={r}>{r}</Chip>)}{v.hosts.map((h) => <Chip key={h}>net: {h}</Chip>)}</>],
          [
            'Pinned',
            <span className="ap-pin">
              {v.source?.commit != null && v.source.commit !== 'upload' && (
                <span>
                  commit <code>{v.source.commit.slice(0, 12)}</code>
                </span>
              )}
              {v.runnable_sha256 !== null && (
                <span>
                  sha256 <code title={v.runnable_sha256}>{v.runnable_sha256.slice(0, 12)}…{v.runnable_sha256.slice(-4)}</code>
                </span>
              )}
              {v.files !== null && <span>{v.files} files</span>}
            </span>,
          ],
        ]}
      />
    </div>
  )
}

/* ---------------------------------------------------------------- progress and refusals */

type StepState = 'done' | 'now' | 'bad' | 'todo'

/** The intake's steps and where it is: fetch or upload, the job's checks, the pin. */
export function intakeSteps(kind: SourceKind, sending: boolean, intake: PluginIntake | null, refused: boolean): [string, StepState][] {
  const first = kind === 'upload' ? 'Upload' : 'Resolve and fetch'
  const state = intake?.state ?? null
  // Where it stands: 0 sending or awaiting the upload, 1 the job checking, 2 pinned.
  const at = state === 'pinned' ? 3 : state === 'running' || state === 'refused' ? 1 : 0
  const mark = (i: number): StepState => (refused && i === Math.min(at, 2) ? 'bad' : i < at ? 'done' : i === at && (sending || state !== null) ? 'now' : 'todo')
  return [
    [first, mark(0)],
    ['Check', mark(1)],
    ['Pin', mark(2)],
  ]
}

function IntakeProgress({ kind, sending, intake, refusal }: { kind: SourceKind; sending: boolean; intake: PluginIntake | null; refusal: Refusal | null }) {
  const steps = intakeSteps(kind, sending, intake, refusal !== null)
  return (
    <ol className="ap-steps" aria-label="Intake">
      {steps.map(([label, s]) => (
        <li key={label} className={`is-${s}`} aria-current={s === 'now' ? 'step' : undefined}>
          {label}
        </li>
      ))}
    </ol>
  )
}

function RefusalBanner({ refusal }: { refusal: Refusal }) {
  return (
    <Banner tone="bad" title="Not installed">
      <span className="ap-refusal">
        <span>{refusal.sentence}</span>
        {refusal.code !== null && <code>{refusal.code}</code>}
        {refusal.entry_index !== null && <span className="ap-dim">entry {refusal.entry_index}</span>}
      </span>
    </Banner>
  )
}
