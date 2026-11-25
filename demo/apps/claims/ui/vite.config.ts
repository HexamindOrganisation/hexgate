import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Claimdesk's web UI. The FastAPI app (claimdesk/server.py) serves the build
// from claimdesk/web under /static, and "/" and "/login" as the two pages.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/static/",
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "./src") },
  },
  build: {
    outDir: "../claimdesk/web",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        index: path.resolve(import.meta.dirname, "index.html"),
        login: path.resolve(import.meta.dirname, "login.html"),
      },
    },
  },
  server: {
    port: 5175,
    proxy: { "/api": "http://localhost:3001" },
  },
});
