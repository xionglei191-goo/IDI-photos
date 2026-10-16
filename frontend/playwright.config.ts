import { defineConfig } from '@playwright/test';
import path from 'node:path';

const root = path.resolve(import.meta.dirname, '..');
const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const dataDir = path.join(root, 'tmp', 'browser-test-' + Date.now());

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 60000,
  expect: { timeout: 12000 },
  reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:8770', channel: 'chrome', headless: true, viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: {
    command: `"${python}" -X utf8 -m uvicorn idi.api:create_app --factory --host 127.0.0.1 --port 8770`,
    cwd: root,
    env: { IDI_DATA_DIR: dataDir, IDI_PORT: '8770', IDI_WORKER: '1', IDI_FRONTEND_DIR: path.join(root, 'frontend', 'dist-test') },
    url: 'http://127.0.0.1:8770/api/health',
    reuseExistingServer: false,
    timeout: 30000,
  },
});
