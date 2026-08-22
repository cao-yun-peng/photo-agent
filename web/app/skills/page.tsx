import type { Metadata } from 'next';
import { SkillsPageView } from '@/features/skills/skills-page';

export const metadata: Metadata = { title: 'Skill 广场 · Photo Agent' };

export default function SkillsPage() {
  return <SkillsPageView />;
}
