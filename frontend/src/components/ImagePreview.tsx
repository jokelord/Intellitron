/**
 * 图片预览组件
 * 
 * 显示图片缩略图，支持点击查看大图和删除。
 */

'use client';

import React, { useState } from 'react';
import { X, ZoomIn } from 'lucide-react';

interface ImagePreviewProps {
  src: string;
  onRemove?: () => void;
  alt?: string;
}

/**
 * 图片预览组件
 * 
 * @param src - 图片源 (base64 或 URL)
 * @param onRemove - 删除图片的回调
 * @param alt - 图片描述文字
 */
export default function ImagePreview({ src, onRemove, alt = '预览图片' }: ImagePreviewProps) {
  const [isFullscreen, setIsFullscreen] = useState(false);

  return (
    <>
      <div className="relative group aspect-square rounded-lg overflow-hidden bg-gray-100 dark:bg-gray-800">
        {/* 图片 */}
        <img
          src={src}
          alt={alt}
          className="w-full h-full object-cover"
        />

        {/* 悬停时的操作按钮 */}
        <div className="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center gap-2">
          {/* 查看大图按钮 */}
          <button
            onClick={() => setIsFullscreen(true)}
            className="p-2 bg-white/20 hover:bg-white/30 rounded-full transition-colors"
            title="查看大图"
          >
            <ZoomIn className="w-5 h-5 text-white" />
          </button>

          {/* 删除按钮 */}
          {onRemove && (
            <button
              onClick={onRemove}
              className="p-2 bg-red-500/80 hover:bg-red-500 rounded-full transition-colors"
              title="删除图片"
            >
              <X className="w-5 h-5 text-white" />
            </button>
          )}
        </div>
      </div>

      {/* 全屏查看模态框 */}
      {isFullscreen && (
        <div
          className="fixed inset-0 z-50 bg-black/90 flex items-center justify-center p-4"
          onClick={() => setIsFullscreen(false)}
        >
          {/* 关闭按钮 */}
          <button
            onClick={() => setIsFullscreen(false)}
            className="absolute top-4 right-4 p-2 bg-white/20 hover:bg-white/30 rounded-full transition-colors"
          >
            <X className="w-6 h-6 text-white" />
          </button>

          {/* 大图 */}
          <img
            src={src}
            alt={alt}
            className="max-w-full max-h-full object-contain"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </>
  );
}
