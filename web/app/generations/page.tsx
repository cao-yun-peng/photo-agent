import type { Metadata } from 'next';
import { GenerationsPageView } from '@/features/generations/generations-page';

export const metadata: Metadata = { title: '生成历史 · Photo Agent' };

export default function GenerationsPage() { return <GenerationsPageView />; }
