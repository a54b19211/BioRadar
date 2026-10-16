import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5176,
    proxy: {
      "/api": "http://127.0.0.1:18100",
      "/health": "http://127.0.0.1:18100",
    },
  },
});
