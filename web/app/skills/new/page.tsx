import type { Metadata } from 'next';
import { SkillEditorPage } from '@/features/skills/skill-editor-page';

export const metadata: Metadata = { title: '新建 Skill · Photo Agent' };

export default function NewSkillPage() {
  return <SkillEditorPage mode="create" />;
}
