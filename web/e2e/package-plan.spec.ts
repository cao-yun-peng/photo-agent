import { expect, test } from '@playwright/test';

test('reviews a package plan, revises it and confirms the new frozen snapshot', async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem('photo-agent:web-session', JSON.stringify({ accessToken: 'fixture-only', expiresAt: Date.now() + 3600000 })));
  const skillId = '00000000-0000-4000-8000-000000000001';
  const photoId = '00000000-0000-4000-8000-000000000002';
  let generation: Record<string, unknown> | null = null;
  let preparations = 0;
  const confirmations: Record<string, unknown>[] = [];
  const bodies: Record<string, unknown>[] = [];
  await page.route('**/*', async (route) => {
    const request = route.request();
    if (!['fetch', 'xhr'].includes(request.resourceType())) return route.continue();
    const path = new URL(request.url()).pathname;
    let data: unknown;
    if (path === '/auth/me') data = { id: 'user-fixture', nickname: '测试用户' };
    else if (path === `/skills/${skillId}`) data = { id: skillId, name: '针织海报', kind: 'package', current_version_id: 'version-1', description: '保留主体，重新组织构图', model: 'gpt-image-2', reference_keys: [] };
    else if (path === '/skills/_/quota') data = { quota: 5, remaining: 5, used: 0 };
    else if (path === '/photos') data = [{ id: photoId, taken_at: '2026-09-06T00:00:00Z', thumb_url: null }];
    else if (path === `/photos/${photoId}/generate`) {
      bodies.push(request.postDataJSON());
      preparations += 1;
      generation = { id: `generation-${preparations}`, source_photo_id: photoId, skill_id: skillId, extra_prompt: bodies.at(-1)?.extra_prompt, status: 'awaiting_confirmation', model: 'gpt-image-2', estimated_cost_yuan: '0.30', confirmation_token: `token-${preparations}`, confirmation_expires_at: '2030-01-01T00:00:00Z', execution_digest: String(preparations).repeat(64), execution_snapshot: { contract: 'package-execution-v1', skill_name: '针织海报', skill_version_id: 'version-1', model: 'gpt-image-2', size: '1536x1024', planner_mode: 'mock', title_options: bodies.at(-1)?.package_options, plan: { observation: '演示方案：主体位于画面中央', concept: '保留山谷轮廓，转化为针织岛屿', retain: ['山谷轮廓'], transform: ['针织材质与留白'], discard: ['干扰背景'], title: '', production_prompt: '以原图为主体，参考图仅提供材质，禁止文字。' }, inputs: [{ position: 0, role: 'subject', path: 'source', sha256: 'a' }, { position: 1, role: 'style', path: 'assets/style.png', sha256: 'b' }] } };
      data = generation;
    } else if (path.endsWith('/iterations')) {
      expect(request.postDataJSON().feedback).toBe('主体不变，减少背景纹理');
      generation = { ...generation, id: 'generation-3', status: 'awaiting_confirmation', progress_stage: 'awaiting_confirmation', iteration_index: 1, parent_generation_id: 'generation-2', confirmation_token: 'token-3', execution_digest: '3'.repeat(64), verification: null };
      data = generation;
    } else if (path.endsWith('/cancel')) {
      generation = { ...generation, status: 'cancelled', progress_stage: 'cancelled', confirmation_token: null };
      data = generation;
    } else if (path.endsWith('/confirm')) {
      confirmations.push(request.postDataJSON());
      generation = { ...generation, status: 'done', verification: { simulated: true, status: 'needs_review' } };
      data = generation;
    } else if (path.endsWith('/cost')) data = { currency: 'CNY', scope: 'generation_workflow_recorded_calls', call_count: 3, estimated_yuan_known: 0.15, unknown_estimate_count: 1, actual_yuan: null, complete: false, billing_status: 'unreconciled', calls: [] };
    else if (path.includes('/inputs/')) return route.fulfill({ contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==', 'base64') });
    else if (path.startsWith('/generations/')) data = generation;
    else return route.continue();
    await route.fulfill({ json: data });
  });
  await page.goto(`/generate/${skillId}`);
  await page.locator('button[data-selected]').first().click();
  await page.getByLabel('标题策略').selectOption('none');
  await page.getByRole('button', { name: '生成创作方案' }).click();
  const dialog = page.getByRole('dialog', { name: '确认生成费用' });
  await expect(dialog.getByText('保留山谷轮廓，转化为针织岛屿')).toBeVisible();
  await expect(dialog.getByText('风格参考：assets/style.png')).toBeVisible();
  expect(confirmations).toHaveLength(0);
  // Restore via the same URL used by the chat handoff, without a route Skill ID.
  await page.goto('/generate?generationId=generation-1');
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: '调整方案' }).click();
  await expect(page.getByLabel('标题策略')).toHaveValue('none');
  await page.getByRole('textbox').first().fill('不要文字，增加留白');
  await page.getByRole('button', { name: '生成创作方案' }).click();
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: '确认生成 · 预计 ¥ 0.30' }).click();
  await expect(page.getByText('这是演示结果，未调用真实生图模型。')).toBeVisible();
  await expect(page.getByText(/另有 1 次费用未知/)).toBeVisible();
  expect(confirmations).toEqual([{ confirmation_token: 'token-2', execution_digest: '2'.repeat(64) }]);
  expect(bodies[0].package_options).toEqual({ title_mode: 'none', title: '' });
  expect(bodies[1].idempotency_key).not.toBe(bodies[0].idempotency_key);
  await page.getByLabel('结果调整要求').fill('主体不变，减少背景纹理');
  await page.getByRole('button', { name: '预览调整方案' }).click();
  await expect(dialog).toBeVisible();
  expect(confirmations).toHaveLength(1);
  await dialog.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByRole('button', { name: '取消本次任务' }).click();
  await expect(page.locator('[data-status="cancelled"]')).toBeVisible();
  expect(confirmations).toHaveLength(1);
});
