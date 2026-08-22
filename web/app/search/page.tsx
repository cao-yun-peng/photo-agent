import type { Metadata } from 'next';
import { SearchPageView } from '@/features/search/search-page';

export const metadata: Metadata = { title: '智能搜索 · Photo Agent' };

export default function SearchPage() {
  return <SearchPageView />;
}
