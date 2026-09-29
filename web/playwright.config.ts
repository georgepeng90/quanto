import { defineConfig } from '@playwright/test'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve, join } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const workspace = process.env.QUANTO_E2E_WORKSPACE || mkdtempSync(join(tmpdir(), 'quanto-e2e-'))
if (!process.env.QUANTO_E2E_WORKSPACE) console.log(`E2E workspace: ${workspace}`)
process.env.QUANTO_E2E_WORKSPACE = workspace

export default defineConfig({
  testDir: './e2e', timeout: 120_000, workers: 1,
  reporter: [['list'], ['json', { outputFile: 'test-results/acceptance.json' }]],
  expect: { timeout: 15_000 },
  use: { actionTimeout: 15_000, channel: process.env.PLAYWRIGHT_CHANNEL, baseURL: 'http://127.0.0.1:8766', viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: {
    command: `"${resolve(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python')}" -m quanto.cli serve --host 127.0.0.1 --port 8766 --workspace "${workspace}" --frontend-dir web/dist`,
    cwd: root, url: 'http://127.0.0.1:8766/api/session', reuseExistingServer: false, timeout: 30_000,
  },
})
