import { defineConfig } from '@playwright/test';
import path from 'node:path';
const root = path.resolve(import.meta.dirname, '..');
export default defineConfig({
  testDir: './e2e', testMatch: ['room-search.spec.ts'], workers: 1, timeout: 60000,
  expect: { timeout: 12000 }, reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:8774', channel: 'chrome', headless: true, viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: {
    command: `"${path.join(root, '.venv', 'Scripts', 'python.exe')}" -X utf8 -m uvicorn idi.api:create_app --factory --host 127.0.0.1 --port 8774`,
    cwd: root, env: { IDI_DATA_DIR: path.join(root, 'tmp', 'room-search-browser-' + Date.now()), IDI_PORT: '8774', IDI_WORKER: '0', IDI_PLATFORM_WRITE: '0', IDI_FRONTEND_DIR: path.join(root, 'frontend', 'dist-room-search-20261031') },
    url: 'http://127.0.0.1:8774/api/health', reuseExistingServer: false, timeout: 30000,
  },
});
