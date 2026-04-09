/**
 * 图片上传组件
 * 
 * 支持文件选择上传和摄像头拍照两种方式。
 * 提供图片预览和删除功能。
 */

'use client';

import React, { useRef, useState, useCallback } from 'react';
import { Upload, Image as ImageIcon, Camera, X, AlertCircle, RefreshCw } from 'lucide-react';
import ImagePreview from './ImagePreview';
import CameraCapture, { destroyAllCameraStreams } from './CameraCapture';
import CycleEditor from './CycleEditor';
import CycleProgress from './CycleProgress';
import { llmClient } from '@/lib/api';
import {
  CycleStep,
  CycleSSEEvent,
  CycleExecutionStatus,
} from '@/lib/cycle-types';

interface ImageUploaderProps {
  onImagesChange: (images: string[] | ((prev: string[]) => string[])) => void;
  images?: string[];
  maxImages?: number;
  maxFileSize?: number;
  acceptedFormats?: string[];
  cameraDimension?: number;
  captureDimension?: number;
  // Cycle props
  cycleEnabled?: boolean;
  cycleMaxDepth?: number;
  cycleDefaultSteps?: CycleStep[];
  cycleExecutionStatus?: CycleExecutionStatus;
  cycleEvents?: CycleSSEEvent[];
  onCycleStart?: (steps: CycleStep[], shareContext: boolean) => void;
  onCycleStop?: () => void;
}

interface UploadError {
  message: string;
  type: 'size' | 'format' | 'count' | 'unknown';
}

/**
 * 图片上传组件
 * 
 * @param onImagesChange - 图片列表变化时的回调
 * @param maxImages - 最大允许的图片数量，默认 9
 * @param maxFileSize - 单个文件最大大小（字节），默认 10MB
 * @param acceptedFormats - 接受的文件格式，默认 ['png', 'jpg', 'jpeg', 'gif', 'webp']
 */
export default function ImageUploader({
  onImagesChange,
  images: externalImages,
  maxImages = 9,
  maxFileSize = 10 * 1024 * 1024,
  acceptedFormats = ['png', 'jpg', 'jpeg', 'gif', 'webp'],
  cameraDimension = 1024,
  captureDimension = 1280,
  cycleEnabled = false,
  cycleMaxDepth = 5,
  cycleDefaultSteps = [],
  cycleExecutionStatus = 'idle',
  cycleEvents = [],
  onCycleStart,
  onCycleStop,
}: ImageUploaderProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  // 使用外部传入的图片列表，如果没有则使用内部状态
  const [internalImages, setInternalImages] = useState<string[]>([]);
  const images = externalImages !== undefined ? externalImages : internalImages;
  const setImages = externalImages !== undefined
    ? (value: string[] | ((prev: string[]) => string[])) => {
      // 直接透传（包括函数式更新），让父组件的 React setState 正确使用最新 state
      onImagesChange(value);
    }
    : setInternalImages;
  const [error, setError] = useState<UploadError | null>(null);
  const [showCamera, setShowCamera] = useState(false);
  const [showCycle, setShowCycle] = useState(false);
  const [showUploadHint, setShowUploadHint] = useState(false);

  const remainingSlots = maxImages - images.length;
  const canAddMore = images.length < maxImages;

  /**
   * 验证文件
   */
  const validateFile = useCallback((file: File): UploadError | null => {
    // 检查文件大小
    if (file.size > maxFileSize) {
      return {
        message: `文件 "${file.name}" 超过最大大小限制 (${formatFileSize(maxFileSize)})`,
        type: 'size',
      };
    }

    // 检查文件格式
    const ext = file.name.split('.').pop()?.toLowerCase();
    if (!ext || !acceptedFormats.includes(ext)) {
      return {
        message: `不支持的文件格式: .${ext}，仅支持 ${acceptedFormats.join(', ')}`,
        type: 'format',
      };
    }

    return null;
  }, [maxFileSize, acceptedFormats]);

  /**
   * 处理文件选择
   */
  const handleFileSelect = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;

    setError(null);
    const newImages: string[] = [];
    const errors: UploadError[] = [];
    // MAX_DIMENSION 已不再用于缩放（保持全分辨率），仅保留注释供参考
    // const MAX_DIMENSION = 768;

    for (const file of Array.from(files)) {
      // 检查数量限制
      if (images.length + newImages.length >= maxImages) {
        errors.push({
          message: `最多只能上传 ${maxImages} 张图片`,
          type: 'count',
        });
        break;
      }

      // 验证文件
      const validationError = validateFile(file);
      if (validationError) {
        errors.push(validationError);
        continue;
      }

      // 优化 v2：直接发送原始二进制字节至后端，后端智能缩放 + 智能格式选择
      // 旧流程：fileToBase64(canvas resize) → Base64 HTTP → 后端 decode → WebP 编码(1.4s!) → encode → 返回
      // 新流程：FormData 原始字节 → HTTP → 后端智能缩放(1280px) + JPEG透传/快速编码 → 唯一一次 b64 → 返回
      // 效果：JPEG 输入无需重编码, 图片缩放减少 visual token 数 → TTFT 从 100s 降到 ~25s
      try {
        const processed = await llmClient.processImageBinary(file, 'auto');
        newImages.push(processed.image);
      } catch (processErr) {
        // 降级处理：后端二进制接口失败时，直接用 FileReader 读取 data URL（无 canvas 缩放）
        console.warn('二进制上传失败，降级到直接读取:', processErr);
        try {
          const dataUrl = await readFileAsDataUrl(file);
          newImages.push(dataUrl);
        } catch {
          errors.push({
            message: `文件 "${file.name}" 读取失败`,
            type: 'unknown',
          });
        }
      }
    }

    // 更新状态
    if (errors.length > 0) {
      setError(errors[0]);
    }

    if (newImages.length > 0) {
      const updatedImages = [...images, ...newImages];
      setImages(updatedImages);
      // 注意: externalImages 模式下 setImages 内部已触发 onImagesChange，无需重复调用
      if (externalImages === undefined) onImagesChange(updatedImages);
    }

    // 清空 input 以便再次选择相同文件
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  }, [images, maxImages, validateFile, onImagesChange]);

  /**
   * 删除图片
   */
  const handleRemoveImage = useCallback((index: number) => {
    const updatedImages = images.filter((_, i) => i !== index);
    setImages(updatedImages);
    if (externalImages === undefined) onImagesChange(updatedImages);
  }, [images, externalImages, onImagesChange]);

  /**
   * 处理摄像头拍照
   * 使用函数式更新，避免并发拍照（快速双击快门）时 stale closure 导致后一张覆盖前一张
   */
  const handleCameraCapture = useCallback((imageData: string) => {
    if (images.length >= maxImages) {
      setError({
        message: `最多只能上传 ${maxImages} 张图片`,
        type: 'count',
      });
      return;
    }

    // 函数式更新：React（及透传链）保证拿到真正最新的 prev，彻底消除 stale closure
    if (externalImages !== undefined) {
      onImagesChange((prev: string[]) => [...prev, imageData]);
    } else {
      setInternalImages((prev: string[]) => [...prev, imageData]);
    }
    setShowCamera(false);
  }, [images.length, maxImages, externalImages, onImagesChange]);

  return (
    <div className="flex flex-col gap-3">
      {/* 错误提示 */}
      {error && (
        <div className="flex items-center gap-2 p-3 bg-red-50 dark:bg-red-900/20 rounded-lg">
          <AlertCircle className="w-5 h-5 text-red-500 flex-shrink-0" />
          <p className="text-sm text-red-600 dark:text-red-400">{error.message}</p>
          <button
            onClick={() => setError(null)}
            className="ml-auto p-1 hover:bg-red-100 dark:hover:bg-red-900/40 rounded"
          >
            <X className="w-4 h-4 text-red-500" />
          </button>
        </div>
      )}

      {/* 工具按钮栏 */}
      <div className="flex gap-2">
        {/* 文件选择按钮 */}
        {canAddMore && (
          <button
            onClick={() => {
              setShowCamera(false);
              setShowCycle(false);
              setShowUploadHint(true);
              fileInputRef.current?.click();
            }}
            className="flex-1 flex items-center justify-center gap-2 
              px-4 py-2 border-2 border-dashed border-gray-300 dark:border-gray-600 
              rounded-lg hover:border-nvidia-500 hover:bg-gray-50 dark:hover:bg-gray-800 
              transition-colors"
          >
            <ImageIcon className="w-5 h-5 text-gray-500" />
            <span className="text-sm text-gray-600 dark:text-gray-400">
              上传图片
            </span>
            <Upload className="w-4 h-4 text-gray-400" />
          </button>
        )}

        {/* 摄像头按钮 */}
        {canAddMore && (
          <button
            onClick={() => {
              setShowCycle(false);
              setShowUploadHint(false);
              setShowCamera(!showCamera);
            }}
            className={`flex-1 flex items-center justify-center gap-2 
              px-4 py-2 border-2 border-dashed rounded-lg transition-colors
              ${showCamera
                ? 'border-nvidia-500 bg-nvidia-500/10'
                : 'border-gray-300 dark:border-gray-600 hover:border-nvidia-500 hover:bg-gray-50 dark:hover:bg-gray-800'
              }`}
          >
            <Camera className={`w-5 h-5 ${showCamera ? 'text-nvidia-500' : 'text-gray-500'}`} />
            <span className={`text-sm ${showCamera ? 'text-nvidia-500' : 'text-gray-600 dark:text-gray-400'}`}>
              {showCamera ? '关闭摄像头' : '摄像头拍照'}
            </span>
          </button>
        )}

        {/* 编程循环按钮 */}
        {cycleEnabled && (
          <button
            onClick={() => {
              // Release browser camera streams before entering cycle mode,
              // so the backend can open the same V4L2 devices via OpenCV.
              if (showCamera) {
                destroyAllCameraStreams();
              }
              setShowCamera(false);
              setShowUploadHint(false);
              setShowCycle(!showCycle);
            }}
            className={`flex-1 flex items-center justify-center gap-2 
              px-4 py-2 border-2 border-dashed rounded-lg transition-colors
              ${showCycle
                ? 'border-nvidia-500 bg-nvidia-500/10'
                : cycleExecutionStatus === 'running'
                  ? 'border-blue-500 bg-blue-500/10'
                  : 'border-gray-300 dark:border-gray-600 hover:border-nvidia-500 hover:bg-gray-50 dark:hover:bg-gray-800'
              }`}
          >
            <RefreshCw className={`w-5 h-5 ${showCycle ? 'text-nvidia-500'
              : cycleExecutionStatus === 'running' ? 'text-blue-400 animate-spin'
                : 'text-gray-500'
              }`} />
            <span className={`text-sm ${showCycle ? 'text-nvidia-500'
              : cycleExecutionStatus === 'running' ? 'text-blue-400'
                : 'text-gray-600 dark:text-gray-400'
              }`}>
              {cycleExecutionStatus === 'running' ? '循环运行中' : '编程循环'}
            </span>
          </button>
        )}
      </div>

      {/* 上传图片提示区域 */}
      {showUploadHint && canAddMore && !showCamera && !showCycle && (
        <div className="text-xs text-gray-500 space-y-0.5">
          <p>最多 {maxImages} 张图片，单个文件最大 {formatFileSize(maxFileSize)}，支持格式: {acceptedFormats.join(', ')}</p>
          <p>还可以上传 {remainingSlots} 张图片</p>
        </div>
      )}

      {/* 隐藏的文件输入 */}
      <input
        ref={fileInputRef}
        type="file"
        accept={acceptedFormats.map(f => `.${f}`).join(',')}
        multiple
        onChange={handleFileSelect}
        className="hidden"
      />

      {/* 摄像头捕获区域 */}
      {showCamera && canAddMore && (
        <div className="mt-2">
          <div className="mb-2 text-xs text-gray-500 space-y-0.5">
            <p>最多 {maxImages} 张图片，单个文件最大 {formatFileSize(maxFileSize)}，支持格式: {acceptedFormats.join(', ')}</p>
            <p>还可以上传 {remainingSlots} 张图片</p>
          </div>
          <CameraCapture
            onCapture={handleCameraCapture}
            maxImages={maxImages}
            currentImageCount={images.length}
            maxDimension={cameraDimension}
            captureDimension={captureDimension}
          />
        </div>
      )}

      {/* 编程循环编辑区域 */}
      {showCycle && cycleEnabled && (
        <div className="mt-2 bg-gray-800/30 border border-gray-700/50 rounded-lg p-3 space-y-3">
          <CycleEditor
            isVisionMode={true}
            maxDepth={cycleMaxDepth}
            defaultSteps={cycleDefaultSteps}
            executionStatus={cycleExecutionStatus}
            onStart={onCycleStart || (() => { })}
            onStop={onCycleStop || (() => { })}
          />
          {cycleExecutionStatus !== 'idle' && (
            <CycleProgress status={cycleExecutionStatus} events={cycleEvents} />
          )}
        </div>
      )}
    </div>
  );
}

/**
 * 格式化文件大小
 */
function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * 直接将文件读取为 data URL（无 canvas、无缩放、无质量压缩）
 * 仅用于 processImageBinary 失败时的降级处理
 */
function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}
