import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  base: "/v2/",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../webui/static/v2",
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:5000",
      "/set-lang": "http://127.0.0.1:5000",
      "/books": "http://127.0.0.1:5000",
      "/read": "http://127.0.0.1:5000",
      "/static": "http://127.0.0.1:5000",
    },
  },
});
