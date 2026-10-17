import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const backend = loadEnv(mode, ".", "").VITE_BACKEND_URL || "http://127.0.0.1:8000";
  return {
    plugins: [react()],
    // three, drei and postprocessing make one large bundle that the backend serves from disk.
    build: { chunkSizeWarningLimit: 1500 },
    server: {
      port: 5173,
      proxy: {
        "/api": backend,
        "/healthz": backend,
        "/ws": { target: backend, ws: true },
      },
    },
  };
});
