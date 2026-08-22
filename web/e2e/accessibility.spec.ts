import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';
import { login } from './helpers';

async function expectAccessible(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();
  const summary = results.violations.map((item) => `${item.id}: ${item.help}`).join('\n');
  expect(results.violations, summary).toEqual([]);
}

test('登录页通过 WCAG A/AA 自动检查', async ({ page }) => {
  await page.goto('/login');
  await expect(page.getByRole('heading', { name: '让相册真正听懂你。' })).toBeVisible();
  await expectAccessible(page);
});

test('工作台与 Skill 广场通过 WCAG A/AA 自动检查', async ({ page }) => {
  await login(page, `Phase 4 A11y ${Date.now()}`);
  await expectAccessible(page);
  await page.getByRole('link', { name: 'Skill 广场', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Skill 广场' })).toBeVisible();
  await expectAccessible(page);
});
