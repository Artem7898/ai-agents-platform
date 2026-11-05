import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ command }) => ({
  plugins: [react()],
  base: command === "serve" ? "/console/" : "/static/console/",
  build: {
    outDir: "../src/console/static/console",
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 700,
  },
  server: {
    proxy: {
      "/console/api": { target: "http://127.0.0.1:8000", changeOrigin: false },
    },
  },
}));
