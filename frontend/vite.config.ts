import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { resolve } from "pathe";

const BASE_URL = "http://localhost:8000";

export default defineConfig({
  resolve: {
    alias: {
      $lib: resolve("./src/lib"),
    },
  },
  plugins: [tailwindcss(), svelte()],
  publicDir: "static",
  server: {
    port: 3000,
    proxy: {
      "/api": {
        target: BASE_URL,
        changeOrigin: true,
      },
      "/static": {
        target: BASE_URL,
        changeOrigin: true,
      },
      "/avatars": {
        target: BASE_URL,
        changeOrigin: true,
      },
    },
  },
});
