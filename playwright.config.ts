import { defineConfig } from '@playwright/test'

/**
 * The live gate: studio.hanzo.ai driven in Chromium as a signed-in person.
 * BASE names the host (default https://studio.hanzo.ai); IAM_ORG/IAM_USER/IAM_PASS
 * name the account, a throwaway one made for the run.
 */
export default defineConfig({
  testDir: 'e2e',
  timeout: 5 * 60_000,
  expect: { timeout: 30_000 },
  workers: 1,
  reporter: [['list']],
  use: { baseURL: process.env.BASE ?? 'https://studio.hanzo.ai', viewport: { width: 1440, height: 900 }, trace: 'retain-on-failure' },
})
