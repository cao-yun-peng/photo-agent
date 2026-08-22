import type { Metadata } from 'next';
import { SkillEditorPage } from '@/features/skills/skill-editor-page';

export const metadata: Metadata = { title: '编辑 Skill · Photo Agent' };

export default function EditSkillPage() {
  return <SkillEditorPage mode="edit" />;
}
