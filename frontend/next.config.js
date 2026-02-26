/** @type {import('next').NextConfig} */
const nextConfig = {
  // 关闭 React Strict Mode（防止开发模式下重复渲染）
  reactStrictMode: false,

  // 输出为独立模式，便于部署到 Jetson Thor
  output: 'standalone',

  // 环境变量
  env: {
    // 后端 API 地址（可通过环境变量覆盖）
    NEXT_PUBLIC_API_BASE_URL: process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000',
  },

  // 图片优化配置
  images: {
    unoptimized: true, // 便于在嵌入式设备上部署
  },

  // 实验性功能
  experimental: {
    // 启用服务器操作
    serverActions: {
      bodySizeLimit: '2mb',
    },
  },
}

module.exports = nextConfig
