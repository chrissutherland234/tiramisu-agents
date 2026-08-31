import { fileURLToPath, URL } from "node:url";

import { defineConfig, devices } from "@playwright/test";

const frontendRoot = fileURLToPath(new URL(".", import.meta.url));
const repositoryRoot = fileURLToPath(new URL("..", import.meta.url));
const uv = process.env.TIRAMISU_UV_COMMAND || "uv";
const apiPort = Number(process.env.TIRAMISU_OPERATOR_SMOKE_API_PORT || "8010");
const frontendPort = Number(process.env.TIRAMISU_OPERATOR_SMOKE_FRONTEND_PORT || "5180");
const apiBaseUrl = `http://127.0.0.1:${apiPort}`;

const localDemoEnvironment = {
  ...process.env,
  TIRAMISU_ENVIRONMENT: "development",
  TIRAMISU_API_PORT: String(apiPort),
  TIRAMISU_ALLOW_UNSAFE_DEVELOPMENT_TENANT_HEADER: "true",
  TIRAMISU_LOAD_FICTIONAL_EXAMPLE_PROCESSES: "true",
  TIRAMISU_DEPLOYMENT_ID: "operator-smoke",
  TIRAMISU_DEPLOYMENT_BUILD_ID: "playwright",
  TIRAMISU_DEPLOYMENT_TENANT_IDS: '["00000000-0000-0000-0000-000000000001"]',
  TIRAMISU_OPENAI_MODEL: "smoke-test-model",
  VITE_API_PROXY_TARGET: apiBaseUrl,
};

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  use: {
    ...devices["Desktop Chrome"],
    baseURL: `http://127.0.0.1:${frontendPort}`,
  },
  webServer: [
    {
      command: `${uv} run tiramisu-admin bootstrap-local && ${uv} run tiramisu-api`,
      cwd: repositoryRoot,
      env: localDemoEnvironment,
      url: `${apiBaseUrl}/health`,
      timeout: 60_000,
      reuseExistingServer: !process.env.CI,
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${frontendPort}`,
      cwd: frontendRoot,
      env: localDemoEnvironment,
      url: `http://127.0.0.1:${frontendPort}`,
      timeout: 60_000,
      reuseExistingServer: !process.env.CI,
    },
  ],
});
