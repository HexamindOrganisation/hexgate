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
  },
})
