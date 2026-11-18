import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Shipyard's web UI. The FastAPI app (shipyard/server.py) serves the build
// from shipyard/web under /static, and "/" and "/login" as the two pages.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/static/",
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "./src") },
  },
  build: {
    outDir: "../shipyard/web",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        index: path.resolve(import.meta.dirname, "index.html"),
        login: path.resolve(import.meta.dirname, "login.html"),
      },
    },
  },
  server: {
    port: 5174,
    proxy: { "/api": "http://localhost:3000" },
  },
});
