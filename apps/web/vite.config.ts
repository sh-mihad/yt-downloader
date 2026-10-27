import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// Dev-only proxy: the backend runs on :8010 (not :8000 — that's Converter's own apps/api
// dev port; running both projects' dev servers at once would otherwise collide), the
// frontend dev server on :5173.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8010",
    },
  },
})
