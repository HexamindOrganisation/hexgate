/// <reference types="vitest/config" />
import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/v1': {
        target: 'http://localhost:8000',
        ws: true,
        changeOrigin: true,
      },
    },
  },
  test: {
    // jsdom — RTL needs a DOM. We don't want a real browser for unit tests.
    environment: 'jsdom',
    // Shared setup file: @testing-library/jest-dom matchers,
    // localStorage stub between tests, etc.
    setupFiles: ['./src/test/setup.ts'],
    globals: true,
    // Mirror the dev server's proxy so msw / fetch-stubs can target /v1/...
    // without prefixing http://localhost:8000 in test code.
    css: false,
    coverage: {
      // v8 — faster than istanbul and matches Node/Chrome's engine so the
      // source maps line up with what we'd see in DevTools. Already
      // bundled with vitest, just needs the explicit provider.
      provider: 'v8',
      reporter: ['text', 'lcov'],
      // Hand-written app code only — exclude tests, type stubs, the
      // shadcn vendored primitives, and the entry point.
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.test.{ts,tsx}',
        'src/**/*.d.ts',
        'src/test/**',
        'src/main.tsx',
        'src/components/ui/**',
        'src/assets/**',
      ],
      // Threshold policy lives in codecov.yml at the repo root, not here
      // — keeps the "fail PR on coverage drop" lever in one place across
      // all three surfaces.
    },
  },
})
