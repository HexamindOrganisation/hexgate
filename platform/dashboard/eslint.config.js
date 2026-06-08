import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      // ---------------------------------------------------------------
      // Both rules below are demoted to ``warn`` so CI doesn't block on
      // patterns that are common across the codebase. They still show
      // up in IDE + ``pnpm lint`` output; tighten back to ``error``
      // once the codebase has been refactored to satisfy them.
      // ---------------------------------------------------------------

      // React 19's stricter hook lint flags every setState-in-effect as
      // an error. Many existing effects are legitimate "sync with
      // external state" patterns the React docs themselves acknowledge
      // as fine; the proper fix is per-call (keys, refs, derived state,
      // or genuine refactor), which is bigger than a CI-setup PR can
      // carry.
      'react-hooks/set-state-in-effect': 'warn',

      // Fast-refresh tooling requires component files to export only
      // components — we sometimes co-locate shared constants / helpers
      // with the component that owns them. The cost (split file) is
      // usually higher than the benefit (HMR-friendliness during dev).
      'react-refresh/only-export-components': 'warn',
    },
  },
])
