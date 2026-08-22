import type { Metadata } from 'next';
import { GeneratePageView } from '@/features/generations/generate-page';

export const metadata: Metadata = { title: '开始生成 · Photo Agent' };

export default function GeneratePage() {
  return <GeneratePageView />;
}
