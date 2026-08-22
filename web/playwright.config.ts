import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.WEB_BASE_URL || 'http://127.0.0.1:3001';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  timeout: 180_000,
  expect: { timeout: 20_000 },
  reporter: process.env.CI
    ? [['line'], ['html', { open: 'never' }]]
    : [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', testIgnore: /responsive\.spec\.ts/, use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile-chromium', testMatch: /responsive\.spec\.ts/, use: { ...devices['Pixel 7'] } },
  ],
  webServer: process.env.WEB_BASE_URL
    ? undefined
    : {
        command: 'npm run dev',
        url: `${baseURL}/login`,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
      },
});
