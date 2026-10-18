import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const apiProxy = process.env.BIORADAR_API_PROXY || "http://127.0.0.1:18100";

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5176,
    proxy: {
      "/api": apiProxy,
      "/health": apiProxy,
    },
  },
});
