import type { Metadata } from 'next';
import { Providers } from './providers';
import './globals.css';

export const metadata: Metadata = {
  title: {
    default: 'Photo Agent',
    template: '%s · Photo Agent',
  },
  description: '中文语境下的 AI 照片管家 Web 开发入口',
  openGraph: {
    title: 'Photo Agent',
    description: '和 AI 一起找到那张照片',
    type: 'website',
    images: [
      {
        url: '/og.png',
        width: 1728,
        height: 909,
        alt: 'Photo Agent — 和 AI 一起找到那张照片',
      },
    ],
  },
  twitter: {
    card: 'summary_large_image',
    title: 'Photo Agent',
    description: '和 AI 一起找到那张照片',
    images: ['/og.png'],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
