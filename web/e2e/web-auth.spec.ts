import { expect, test } from '@playwright/test';

test('正式账号注册、退出、错误密码、重新登录与会话过期', async ({ page }) => {
  const username = `web_e2e_${Date.now()}`;
  const password = 'OnlyForIsolatedE2E_123!';
  await page.goto('/login');
  await page.getByRole('button', { name: '还没有账号？立即注册' }).click();
  await page.getByLabel('账号', { exact: true }).fill(username);
  await page.getByLabel('密码', { exact: true }).fill(password);
  await page.getByLabel('确认密码', { exact: true }).fill(password);
  await page.getByRole('button', { name: '注册并进入' }).click();
  await expect(page).toHaveURL(/\/photos$/);
  await expect(page.getByRole('heading', { name: '时间线', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '退出登录', exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('账号', { exact: true }).fill(username);
  await page.getByLabel('密码', { exact: true }).fill('WrongPassword_123!');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('账号或密码不正确');
  await page.getByLabel('密码', { exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page).toHaveURL(/\/photos$/);
  await page.evaluate(() => {
    const key = 'photo-agent:web-session';
    const session = JSON.parse(sessionStorage.getItem(key)!);
    sessionStorage.setItem(key, JSON.stringify({ ...session, expiresAt: 1 }));
  });
  await page.reload();
  await expect(page).toHaveURL(/\/login$/);
});
