import { expect, type Page } from '@playwright/test';

export async function login(page: Page, nickname = 'Phase 4 E2E') {
  await page.goto('/login');
  await page.waitForLoadState('networkidle');
  await page.getByLabel('开发用户昵称').fill(nickname);
  await page.getByRole('button', { name: '使用开发用户进入' }).click();
  await expect(page).toHaveURL(/\/photos$/);
  await expect(page.getByRole('heading', { name: '时间线' })).toBeVisible();
}

export async function expectNoHorizontalOverflow(page: Page) {
  const sizes = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }));
  expect(sizes.content, `页面横向溢出 ${sizes.content - sizes.viewport}px`)
    .toBeLessThanOrEqual(sizes.viewport + 1);
}
