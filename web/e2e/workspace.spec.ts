import { expect, test } from '@playwright/test';
test('persists selection and explicit preferences, saves an album and undoes without losing photos', async ({ page }, testInfo) => {
  await page.addInitScript(() => sessionStorage.setItem('photo-agent:web-session', JSON.stringify({ accessToken: 'fixture-only', expiresAt: Date.now() + 3600000 })));
  const photos = Array.from({ length: 12 }, (_, i) => ({ id: `00000000-0000-4000-8000-${String(i+1).padStart(12,'0')}`, version: 'a'.repeat(64), description: `旅行照片 ${i+1}`, thumb_url: null, correction: null, correction_stale: false }));
  type State = { revision: number; selection: typeof photos; task: { goal: string; target_count: number; min_group_count: number; locked_ids: string[]; excluded_ids: string[]; prefer_landscape: boolean }; preferences: { preferred_subject: string; title_mode: string }; albums: { id: string; title: string; count: number }[]; facts: unknown[]; missing_selection_count: number; memory_source: string; undo: { operation_id: string; expires_at: string } | null };
  let state: State = { revision: 0, selection: photos, task: { goal: '旅行精选', target_count: 12, min_group_count: 3, locked_ids: [], excluded_ids: [], prefer_landscape: false }, preferences: { preferred_subject: 'none', title_mode: 'auto' }, albums: [], facts: [], missing_selection_count: 0, memory_source: 'explicit_user', undo: null };
  let before = structuredClone(state);
  const calls: Record<string, unknown>[] = [];
  await page.route('**/*', async route => {
    const req = route.request();
    if (!['fetch','xhr'].includes(req.resourceType())) return route.continue();
    const path = new URL(req.url()).pathname;
    if (path === '/auth/me') return route.fulfill({ json: { id: 'user-fixture', nickname: '测试用户' } });
    if (path === '/workspace') return route.fulfill({ json: state });
    if (path === '/workspace/actions') {
      const body = req.postDataJSON(); calls.push(body);
      expect(body.expected_revision).toBe(state.revision);
      before = structuredClone(state);
      if (body.kind === 'set_preferences') state.preferences = body.preferences;
      else if (body.kind === 'save_album') state.albums = [{ id: '10000000-0000-4000-8000-000000000001', title: body.title, count: 12 }];
      else throw new Error(`Unexpected operation ${body.kind}`);
      state.revision += 1; state.undo = { operation_id: '20000000-0000-4000-8000-000000000001', expires_at: '2030-01-01T00:00:00Z' };
      return route.fulfill({ json: { workspace: state, operation_id: state.undo.operation_id, can_undo: true } });
    }
    if (path.endsWith('/undo')) {
      expect(req.postDataJSON().expected_revision).toBe(state.revision);
      state = { ...before, revision: state.revision+1, undo: null };
      return route.fulfill({ json: { workspace: state, operation_id: '20000000-0000-4000-8000-000000000001', can_undo: false } });
    }
    return route.continue();
  });
  await page.goto('/workspace');
  await expect(page.getByText('已选 12 张', { exact: true })).toBeVisible();
  await page.getByLabel('常用选片偏好').selectOption('people');
  expect(calls).toHaveLength(0);
  await page.getByRole('button', { name: '保存明确偏好' }).click();
  await expect.poll(() => state.revision).toBe(1);
  await page.reload();
  await expect(page.getByLabel('常用选片偏好')).toHaveValue('people');
  await expect(page.getByText('已选 12 张', { exact: true })).toBeVisible();
  await page.getByLabel('相册名称').fill('旅行十二张');
  await page.getByRole('button', { name: '保存为相册' }).click();
  await expect(page.getByText('旅行十二张', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '撤销上一步' }).click();
  await expect(page.getByText('旅行十二张', { exact: true })).toHaveCount(0);
  await expect(page.getByText('已选 12 张', { exact: true })).toBeVisible();
  await expect(page.getByLabel('常用选片偏好')).toHaveValue('people');
  await page.screenshot({ path: testInfo.outputPath('workspace.png'), fullPage: true });
});
