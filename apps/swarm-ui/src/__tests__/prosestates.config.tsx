// The config section's click-only states (issue 439; the harness and the rule
// are in prosestates.tsx).

import { fireEvent } from '@testing-library/react'

import type { ProseState } from './prosestates'
import { click } from './prosestates.shell'
import { clickDrawn, drawn, foldDrawn, hover, press } from './prosestates.overview'
import { mountGroupRoles, mountPluginDetail, mountPushTest, mountSetupStrip, mountStepPlugins, mountTokenCreated } from './prosestates.config.mounts'

const WS = 'config/workspace'
const TOKENS = 'config/repositories?page=tokens'

// The sentences GitTokens.tsx moved behind a token entry's (i)s, word for word.
const PAT_TIP = 'For when Connect GitHub above is not an option. A personal access token does not renew itself: you store a new one before it expires.'
const ENTRY_TIP = 'SwarmCloud names the secret that will hold the token; the name is not a credential.'
const NO_ENTRY_TIP = "Create the token entry, or press Rotate on a token above, to see its secret's name and the command that stores its value."
const STORE_TIP = 'Store the token from your terminal; the command reads it from stdin.'
const CHECK_TIP =
  "SwarmCloud sees the new version, keeps the last 4 characters, checks the token's permissions and shows them here. The token itself is never shown here: it goes from your terminal to Secret Manager."
/** ReachableRepositories.tsx's Push test ask, for the write grant the mount serves. */
const PUSH_TIP =
  "SwarmCloud creates one branch, swarmcloud/onboarding-check-<nonce>, at the head of example-org/example-service's default branch, as you, and deletes it straight away. It is the only check that writes to GitHub."

export const CONFIG: readonly ProseState[] = [
  {
    key: 'config/github-how',
    group: 'config',
    files: ['Access.tsx'],
    shows: 'details.ac-rule-how[open]',
    walk: { open: 'config/github', then: () => click('details.ac-rule-how > summary') },
  },
  {
    key: 'config/tokens-pat',
    group: 'config',
    files: ['GitTokens.tsx'],
    shows: '#ur-reg',
    moved: [PAT_TIP, ENTRY_TIP, NO_ENTRY_TIP],
    lines: ['No token entry yet'],
    walk: { open: TOKENS, then: () => clickDrawn('.ur-discl-btn') },
  },
  {
    key: 'config/workspace-people-github',
    group: 'config',
    files: ['WorkspacePage.tsx'],
    shows: 'details.wpg-fold[open]',
    moved: ['Each member connects as themselves. An admin sees states and names, never a value, and cannot reconnect for someone else.'],
    walk: { open: WS, persona: 'platform-admin', then: async () => { await press('main', 'People'); await foldDrawn('main', 'Members') } },
  },
  {
    key: 'config/workspace-roles-why',
    group: 'config',
    files: ['WorkspacePage.tsx'],
    shows: 'details.wpg-why[open]',
    moved: [
      'Admin is given person by person, so nobody gains it by being added to the Google group.',
      'A platform admin is admin in every group workspace; a personal workspace’s person is its fixed admin.',
    ],
    mount: mountGroupRoles,
    act: async () => { await press('main', 'Roles'); await foldDrawn('main', 'Why the default') },
  },
  {
    key: 'config/repository-detail',
    group: 'config',
    files: ['RepositoriesDetail.tsx'],
    shows: '.ur-detail',
    walk: { open: 'config/repositories', then: async () => { await press('.ur-cacts', 'Open'); await drawn('.ur-detail') } },
  },
  {
    key: 'config/repository-cache',
    group: 'config',
    files: ['RepoGraphCache.tsx'],
    shows: '.ur-cache-body',
    walk: { open: 'config/repositories', then: () => clickDrawn('.ur-cache-toggle') },
  },
  {
    key: 'config/tokens-created',
    group: 'config',
    files: ['GitTokens.tsx'],
    shows: '#ur-reg:has(.ur-secret)',
    moved: [STORE_TIP, CHECK_TIP],
    mount: mountTokenCreated,
    act: async () => { await press('main', '▸ Use a personal'); await press('#ur-reg', 'Create the token entry') },
  },
  {
    key: 'config/tokens-create-refused',
    group: 'config',
    files: ['GitTokens.tsx'],
    shows: '#ur-reg .c-banner',
    moved: ['Not served yet: this API does not take this request yet.'],
    lines: ['Not served yet'],
    walk: { open: TOKENS, then: async () => { await press('main', '▸ Use a personal'); await press('#ur-reg', 'Create the token entry') } },
  },
  {
    key: 'config/tokens-team',
    group: 'config',
    files: ['GitTokens.tsx'],
    shows: '#ur-reg-team',
    moved: [ENTRY_TIP, NO_ENTRY_TIP],
    lines: ['No token entry yet'],
    walk: { open: TOKENS, persona: 'platform-admin', then: () => press('main', '▸ Workspace token') },
  },
  {
    key: 'config/github-push-test',
    group: 'config',
    files: ['ReachableRepositories.tsx'],
    shows: '.ac-fails',
    moved: [PUSH_TIP],
    lines: ['Writes one test branch, then deletes it'],
    mount: mountPushTest,
    act: () => press('body', 'Push test'),
  },
  {
    key: 'config/github-push-test-refused',
    group: 'config',
    files: ['ReachableRepositories.tsx'],
    shows: '.ac-fails',
    data: ['Another check is running on this grant.'],
    mount: mountPushTest,
    act: async () => { await press('body', 'Push test'); await press('.ac-fails', 'Run push test') },
  },
  {
    key: 'config/setup-strip-steps',
    group: 'config',
    files: ['ConfigSetup.tsx'],
    shows: '[aria-label="Workspace setup steps"]',
    mount: mountSetupStrip,
    act: () => press('.cfg-strip', 'Show steps'),
  },
  {
    key: 'admin/new-workspace',
    group: 'config',
    files: ['NewWorkspace.tsx'],
    shows: '[role="dialog"]',
    moved: [
      'name@your-domain gets its own tenant, worker account, storage prefix and namespace. It is approved at once, because you are an admin, and is ready when they first sign in. It runs nothing until a Claude account is lent to them or they add their own.',
    ],
    walk: { open: 'admin/tenants', persona: 'platform-admin', then: () => press('main', 'New workspace') },
  },
  {
    key: 'overview/setup-guide-tip',
    group: 'config',
    files: ['Onboarding.tsx'],
    shows: '.c-tip:not([hidden])',
    walk: { open: 'overview/now', then: () => hover('main') },
  },
  {
    key: 'config/plugin-remove',
    group: 'config',
    files: ['PluginDetail.tsx'],
    shows: '[role="dialog"]',
    mount: mountPluginDetail,
    act: () => press('body', 'Remove'),
  },
  {
    key: 'config/plugin-remove-refused',
    group: 'config',
    files: ['PluginDetail.tsx'],
    shows: '[role="dialog"] [role="alert"]',
    data: ['example-plugin is used by nightly-check.'],
    mount: mountPluginDetail,
    act: async () => {
      await press('body', 'Remove')
      fireEvent.change(await drawn('[role="dialog"] input'), { target: { value: 'example-plugin' } })
      await press('[role="dialog"]', 'Remove')
    },
  },
  {
    key: 'config/plugin-update',
    group: 'config',
    files: ['PluginDetail.tsx', 'PluginDiff.tsx'],
    shows: '[role="dialog"]',
    mount: mountPluginDetail,
    act: async () => { await press('.pd-tabs', 'Versions'); await press('body', 'Review the update'); await press('body', 'Update to') },
  },
  {
    key: 'config/step-plugin-sheet',
    group: 'config',
    files: ['PluginSheet.tsx'],
    shows: '.psh-sheet',
    moved: [
      'Only plugins this workspace allows can be picked (Config › Plugins).',
      'answers what has a recommended answer itself; anything else waits for a person',
      'What each skill does is not served yet; the catalogue names them only.',
    ],
    mount: mountStepPlugins,
    act: () => clickDrawn('button[aria-label^="Add a plugin"]'),
  },
  {
    key: 'config/step-managed-sheet',
    group: 'config',
    files: ['PluginSheet.tsx'],
    shows: '.psh-sheet',
    mount: mountStepPlugins,
    act: () => clickDrawn('button[aria-label^="Managed plugins on"]'),
  },
  {
    key: 'config/step-managed-settings',
    group: 'config',
    files: ['PluginSheet.tsx'],
    shows: 'ul.psh-choices[aria-label$="settings for this step"]',
    mount: mountStepPlugins,
    act: async () => { await clickDrawn('button[aria-label^="Managed plugins on"]'); await clickDrawn('button[aria-label$="settings for this step"]') },
  },
  {
    key: 'config/secret-delete',
    group: 'config',
    files: ['WorkspaceSecrets.tsx'],
    shows: '[role="dialog"]',
    walk: { open: 'config/secrets', then: () => clickDrawn('button[aria-label^="Delete "]') },
  },
  {
    key: 'admin/setup-capacity',
    group: 'config',
    files: ['consolefinish/ChecklistFinish.tsx'],
    shows: 'details.cfc-more[open]',
    walk: { open: 'admin/setup', persona: 'platform-admin', then: () => foldDrawn('main', 'Capacity and add-ons') },
  },
]
