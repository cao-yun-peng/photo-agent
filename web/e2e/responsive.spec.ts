import { expect, test } from '@playwright/test';
import { expectNoHorizontalOverflow, login } from './helpers';

test('移动端登录、时间线和 Skill 广场无横向溢出', async ({ page }) => {
  await page.goto('/login');
  await expect(page.getByRole('heading', { name: '让相册真正听懂你。' })).toBeVisible();
  await expectNoHorizontalOverflow(page);

  await login(page, `Phase 4 Mobile ${Date.now()}`);
  await expectNoHorizontalOverflow(page);
  await expect(page.getByRole('navigation', { name: '主要导航' })).toBeVisible();

  await page.getByRole('link', { name: 'Skill 广场', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Skill 广场' })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});
