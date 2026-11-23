// The swarm-ui lint gate (part of #77).
//
// WHY A BASELINE AND NOT WARN-LEVEL RULES. Every rule below is at `error`. The
// violations main already had when this file landed are recorded in
// eslint-suppressions.json (ESLint's own suppressions file, generated with
// `npx eslint --suppress-all .`), so `npm run lint` starts green and fails on
// any error that file does not name. A warning is printed and ignored; an
// error outside the baseline stops the build. The baseline counts per file and
// per rule, so a file that has three suppressed violations of a rule fails on a
// fourth.
//
// BURNING IT DOWN. Fix violations, then `npm run lint:prune` to drop the
// entries that no longer occur: ESLint exits non-zero while the file lists
// suppressions that are not used, so the baseline can only shrink. Never
// regenerate it with `--suppress-all` to make a new violation pass.
//
// THE LINT SCRIPT'S FLAGS. Typed linting holds the whole TypeScript program in
// memory: one process over every file ran out of Node's default ~2 GiB heap
// after eight minutes. `--concurrency 2` splits the files over two workers at
// 3 GiB each (measured: about six minutes, inside an 8 GiB container); more
// workers were killed for memory there.
//
// test/eslintBaseline.test.mjs proves a violation outside the baseline fails
// and the same violation inside it passes.

import { defineConfig } from 'eslint/config'
import js from '@eslint/js'
import { configs as tseslintConfigs } from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import { importX } from 'eslint-plugin-import-x'
import { createTypeScriptImportResolver } from 'eslint-import-resolver-typescript'
import unusedImports from 'eslint-plugin-unused-imports'
import { configs as sonarjsConfigs } from 'eslint-plugin-sonarjs'
import testingLibrary from 'eslint-plugin-testing-library'
import globals from 'globals'

const TS_FILES = ['**/*.ts', '**/*.tsx']
const JS_FILES = ['**/*.js', '**/*.mjs']
const TEST_FILES = ['**/*.test.ts', '**/*.test.tsx', 'src/__tests__/**/*.{ts,tsx}']

const configs = defineConfig(
  {
    // src/generated/ is written by a generator, not by hand: lint its source.
    ignores: ['dist/**', 'node_modules/**', 'coverage/**', 'test-results/**', 'src/generated/**'],
  },
  {
    linterOptions: {
      // A disable comment that silences nothing hides the next real violation
      // on that line, so it is an error too.
      reportUnusedDisableDirectives: 'error',
    },
  },
  js.configs.recommended,
  {
    files: TS_FILES,
    extends: [tseslintConfigs.recommendedTypeChecked],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser },
      parserOptions: {
        // tsconfig.json covers src/ and tests/ and what they import. The few
        // TypeScript files outside it (the Vite configs and test/'s .tsx) are typed
        // against the same compiler options through the default project.
        projectService: {
          allowDefaultProject: ['*.ts', 'test/*.tsx'],
          defaultProject: './tsconfig.json',
        },
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
  {
    files: JS_FILES,
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.node },
    },
  },
  {
    files: [...TS_FILES, ...JS_FILES],
    plugins: {
      'react-hooks': reactHooks,
      'unused-imports': unusedImports,
    },
    extends: [importX.flatConfigs.recommended, importX.flatConfigs.typescript, sonarjsConfigs.recommended],
    settings: {
      'import-x/resolver-next': [createTypeScriptImportResolver({ project: './tsconfig.json' })],
    },
    rules: {
      // The two classic hook rules. The plugin's React Compiler rules are left
      // out: this app is not built with the compiler, so they would judge code
      // against a transform that never runs on it.
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'error',

      // unused-imports reports an unused import and an unused variable as two
      // rules, so the base and typescript-eslint copies are switched off.
      'no-unused-vars': 'off',
      '@typescript-eslint/no-unused-vars': 'off',
      'unused-imports/no-unused-imports': 'error',
      'unused-imports/no-unused-vars': [
        'error',
        { vars: 'all', varsIgnorePattern: '^_', args: 'after-used', argsIgnorePattern: '^_', caughtErrors: 'none' },
      ],

      // 1,000 lines: past it an agent can no longer read the file in one
      // window (CLAUDE.md, "Read by window"), so a file that grows past it
      // is split rather than read in pieces.
      'max-lines': ['error', { max: 1000 }],
    },
  },
  {
    files: TS_FILES,
    rules: {
      // Checks the TypeScript compiler already makes on every typecheck, and
      // the slowest rules in the set (import-x/namespace alone was a quarter
      // of the run): an unresolved import or a missing export fails `tsc -b`,
      // which covers these files and not the .mjs ones.
      'import-x/namespace': 'off',
      'import-x/named': 'off',
      'import-x/default': 'off',
      'import-x/no-named-as-default-member': 'off',
      'import-x/no-unresolved': 'off',
      // Deprecation needs every referenced symbol's declaration resolved; it
      // was the costliest rule measured, and @deprecated APIs show in the
      // editor already.
      'sonarjs/deprecation': 'off',
    },
  },
  {
    files: TEST_FILES,
    extends: [testingLibrary.configs['flat/react']],
  },
  {
    // typed rules need type information, which the .mjs files do not have.
    files: JS_FILES,
    extends: [tseslintConfigs.disableTypeChecked],
  },
)

// Every rule at error, including the ones a shared preset ships at `warn`
// (import-x's no-duplicates and no-named-as-default, among others): a warning
// would pass the gate unseen and never enter the baseline.
function atError(entry) {
  if (entry === 'warn' || entry === 1) return 'error'
  if (Array.isArray(entry) && (entry[0] === 'warn' || entry[0] === 1)) return ['error', ...entry.slice(1)]
  return entry
}

export default configs.map((config) =>
  config.rules
    ? { ...config, rules: Object.fromEntries(Object.entries(config.rules).map(([name, entry]) => [name, atError(entry)])) }
    : config,
)
