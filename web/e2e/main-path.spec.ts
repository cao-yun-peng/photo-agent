import path from 'node:path';
import { expect, test } from '@playwright/test';
import { login } from './helpers';

test('登录、上传、Agent 搜索和 Skill 生成主路径', async ({ page }) => {
  await login(page, `Phase 4 E2E ${Date.now()}`);

  await page.getByRole('link', { name: '上传', exact: true }).click();
  await expect(page.getByRole('heading', { name: '上传照片' })).toBeVisible();
  await page.locator('input[type="file"]').setInputFiles(
    path.resolve(process.cwd(), '../test_photos/p-102_coffee_morning.jpg'),
  );
  await page.getByRole('button', { name: /开始上传/ }).click();
  await expect(page.getByText(/上传完成|相册中已存在/).first()).toBeVisible({ timeout: 90_000 });

  await page.getByRole('link', { name: '时间线', exact: true }).click();
  await expect(page.getByText('智能搜索已就绪').first()).toBeVisible({ timeout: 90_000 });

  await page.getByRole('link', { name: '智能搜索', exact: true }).click();
  await expect(page.getByRole('heading', { name: '和 Photo Agent 一起找照片' })).toBeVisible();
  // Keep the CI path deterministic: this explicit search uses the Agent fast path
  // and does not require an external LLM to decide which tool to call.
  await page.getByLabel('给 Photo Agent 的消息').fill('我想要照片');
  await page.getByRole('button', { name: '发送' }).click();
  await expect(page.getByRole('button', { name: /查看第 1 张照片/ })).toBeVisible({ timeout: 90_000 });

  await page.getByRole('link', { name: 'Skill 广场', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Skill 广场' })).toBeVisible();
  await page.getByRole('link', { name: '使用这个 Skill' }).first().click();
  await expect(page.getByRole('heading', { name: '选择源图', exact: true })).toBeVisible();
  await page.locator('button[data-selected]').first().click();
  await page.getByRole('button', { name: '预览费用并生成' }).click();

  const confirmation = page.getByRole('dialog', { name: '确认生成费用' });
  if (await confirmation.isVisible({ timeout: 5_000 }).catch(() => false)) {
    await confirmation.getByRole('button', { name: /确认支付|重试入队/ }).click();
  }

  await expect(page.getByText('新作品已经完成')).toBeVisible({ timeout: 150_000 });
  await expect(page.getByAltText('AI 生成结果')).toBeVisible();
  await page.getByRole('link', { name: '查看全部历史' }).click();
  await expect(page.getByRole('heading', { name: '生成历史' })).toBeVisible();
  await expect(page.getByAltText('生成结果').first()).toBeVisible();
});
