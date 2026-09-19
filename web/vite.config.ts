import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: "dist", chunkSizeWarningLimit: 1200 },
  server: {
    port: 5173,
    proxy: { "/v1": "http://127.0.0.1:8099", "/health": "http://127.0.0.1:8099" },
  },
});
