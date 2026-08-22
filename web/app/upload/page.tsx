import type { Metadata } from 'next';
import { UploadPageView } from '@/features/upload/upload-page';

export const metadata: Metadata = { title: '上传照片 · Photo Agent' };

export default function UploadPage() {
  return <UploadPageView />;
}
