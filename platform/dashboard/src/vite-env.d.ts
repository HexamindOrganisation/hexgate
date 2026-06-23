/// <reference types="vite/client" />

interface ImportMetaEnv {
  // Opt-in beta "data may be reset" banner; set in the deploy build only.
  readonly VITE_PREVIEW_BANNER?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
