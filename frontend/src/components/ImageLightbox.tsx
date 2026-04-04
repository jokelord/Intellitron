/**
 * 图片全屏预览组件（Lightbox）
 *
 * 点击缩略图后弹出全屏预览，点击背景或关闭按钮关闭。
 * 支持 Escape 键关闭。
 */

'use client';

import React, { useEffect, useCallback } from 'react';
import { X } from 'lucide-react';

interface ImageLightboxProps {
  /** 要预览的图片 src（base64 或 URL），为 null 时不渲染 */
  src: string | null;
  /** 关闭回调 */
  onClose: () => void;
  /** 可选的 alt 文本 */
  alt?: string;
}

export default function ImageLightbox({ src, onClose, alt = '图片预览' }: ImageLightboxProps) {
  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    },
    [onClose],
  );

  useEffect(() => {
    if (!src) return;
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [src, handleKeyDown]);

  if (!src) return null;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/90 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <button
        onClick={onClose}
        className="absolute top-4 right-4 p-2 bg-white/20 hover:bg-white/30 rounded-full transition-colors"
      >
        <X className="w-6 h-6 text-white" />
      </button>
      <img
        src={src}
        alt={alt}
        className="max-w-full max-h-full object-contain"
        onClick={(e) => e.stopPropagation()}
      />
    </div>
  );
}
