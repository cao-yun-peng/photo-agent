import type { Metadata } from 'next';
import { PhotosPageView } from '@/features/photos/photos-page';

export const metadata: Metadata = { title: '时间线' };

export default function PhotosPage() {
  return <PhotosPageView />;
}
