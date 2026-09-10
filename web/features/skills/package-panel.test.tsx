import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { PackageUpload } from './package-panel';

const mocks = vi.hoisted(() => ({ preview: vi.fn(), save: vi.fn() }));
vi.mock('@/lib/api/skills', () => ({ previewSkillPackage: mocks.preview, importSkillPackage: mocks.save }));
afterEach(cleanup);
beforeEach(() => { mocks.preview.mockReset(); mocks.save.mockReset(); });
const report = { name: '针织', description: '照片转针织', content_sha256: 'a'.repeat(64), assets: [], supported: ['Markdown'], warnings: ['流程包暂不可生成'], errors: [], can_import: true };

it('previews before saving the exact file and hash privately', async () => {
  mocks.preview.mockResolvedValue(report);
  mocks.save.mockResolvedValue({ deduplicated: false });
  const saved = vi.fn();
  render(<PackageUpload onSaved={saved} />);
  const file = new File(['zip'], 'knit.zip');
  fireEvent.change(screen.getByLabelText('选择Skill ZIP'), { target: { files: [file] } });
  expect(await screen.findByText('流程包暂不可生成')).toBeInTheDocument();
  expect(mocks.save).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '保存私有流程包' }));
  await waitFor(() => expect(mocks.save).toHaveBeenCalledWith(file, report.content_sha256, undefined));
  expect(await screen.findByText('已保存为私有流程包，可查看资源和版本。')).toBeInTheDocument();
  expect(saved).toHaveBeenCalledOnce();
});

it('blocks unsupported packages and clears previous preview on a new file', async () => {
  mocks.preview.mockResolvedValueOnce({ ...report, can_import: false, errors: ['引用缺失'] }).mockRejectedValueOnce(new Error('ZIP损坏'));
  render(<PackageUpload onSaved={vi.fn()} />);
  const input = screen.getByLabelText('选择Skill ZIP');
  fireEvent.change(input, { target: { files: [new File(['bad'], 'bad.zip')] } });
  expect(await screen.findByText('引用缺失')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '保存私有流程包' })).toBeDisabled();
  await waitFor(() => expect(input).not.toBeDisabled());
  fireEvent.change(input, { target: { files: [new File(['bad2'], 'bad2.zip')] } });
  expect(await screen.findByText('ZIP损坏')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '保存私有流程包' })).not.toBeInTheDocument();
});

it('sends the chosen skill for a version upload and reports duplicate content', async () => {
  mocks.preview.mockResolvedValue(report);
  mocks.save.mockResolvedValue({ deduplicated: true });
  render(<PackageUpload skillId="skill-1" onSaved={vi.fn()} />);
  const file = new File(['zip'], 'new.zip');
  fireEvent.change(screen.getByLabelText('选择Skill ZIP'), { target: { files: [file] } });
  fireEvent.click(await screen.findByRole('button', { name: '保存私有新版本' }));
  expect(await screen.findByText('相同内容已保存，未新增或切换版本。')).toBeInTheDocument();
  expect(mocks.save).toHaveBeenCalledWith(file, report.content_sha256, 'skill-1');
});
