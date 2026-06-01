import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./tests/setup.ts"],
    globals: true,
    // Playwright E2E specs use @playwright/test — exclude them from vitest.
    exclude: ["tests/e2e/**", "node_modules/**"],
  },
  resolve: {
    alias: {
      // Mirror the tsconfig paths alias: @/* → ./app/*
      "@": path.resolve(__dirname, "./app"),
    },
  },
});
