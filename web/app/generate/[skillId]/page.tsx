import type { Metadata } from 'next';
import { GeneratePageView } from '@/features/generations/generate-page';

export const metadata: Metadata = { title: '使用 Skill · Photo Agent' };

export default function GenerateWithSkillPage() {
  return <GeneratePageView />;
}
