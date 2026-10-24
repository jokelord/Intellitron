import type { Metadata, Viewport } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: 'LLM-Test | 大语言模型体验程序',
  description: '基于 Qwen3-Next-80B-A3B-Instruct-NVFP4 的对话体验程序，支持 NVIDIA Jetson Thor 部署',
  keywords: ['LLM', 'Qwen3', 'NVIDIA', 'Jetson Thor', 'ChatBot'],
  authors: [{ name: 'LLM-Test Team' }],
  icons: {
    icon: '/favicon.svg',
  },
}

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  viewportFit: 'cover',
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="zh-CN" className="h-full overflow-hidden">
      <body className="h-full overflow-hidden bg-white dark:bg-gray-900">
        {children}
      </body>
    </html>
  )
}
