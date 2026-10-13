import { defineConfig, devices } from "@playwright/test";

// Starts the real API (with the deterministic demo model and an in-process MCP server)
// and the production build of the web app, then drives a browser through the review flow.
const API_PORT = process.env.E2E_API_PORT ?? "8010";
const WEB_PORT = process.env.E2E_WEB_PORT ?? "3010";
const DATABASE_URL =
  process.env.DATABASE_URL ?? "postgresql+asyncpg://agent:agent@127.0.0.1:5432/agent";

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  retries: 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "uv run python -m ga_api.app",
      cwd: "..",
      url: `http://127.0.0.1:${API_PORT}/healthz`,
      timeout: 120_000,
      reuseExistingServer: false,
      env: {
        DATABASE_URL,
        GA_MODEL: "demo",
        GA_API_PORT: API_PORT,
        GA_OUTBOX_POLL_S: "0.2",
        GA_WAREHOUSE: "data/warehouse.duckdb",
        LOG_LEVEL: "WARNING",
      },
    },
    {
      command: `npm run start -- --port ${WEB_PORT}`,
      url: `http://127.0.0.1:${WEB_PORT}`,
      timeout: 120_000,
      reuseExistingServer: false,
      env: { API_URL: `http://127.0.0.1:${API_PORT}` },
    },
  ],
});
