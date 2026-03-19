/**
 * 摄像头捕获组件
 * 
 * 提供实时摄像头预览和拍照功能。
 * 用户点击拍照按钮时，截取当前视频帧作为图片。
 */

'use client';

import React, { useRef, useState, useCallback, useEffect } from 'react';
import { Camera, CameraOff, Circle, Loader2 } from 'lucide-react';
import { llmClient } from '@/lib/api';

interface CameraCaptureProps {
  onCapture: (imageData: string) => void;
  maxImages?: number;
  currentImageCount?: number;
  maxDimension?: number; // 前端 canvas 预缩最大边长(px)，来自 config.ini camera_max_dimension
}

interface CaptureError {
  message: string;
  type: 'permission' | 'not-found' | 'unknown';
}

/**
 * 摄像头捕获组件
 * 
 * @param onCapture - 拍照完成后的回调函数，返回 base64 编码的图片数据
 * @param maxImages - 最大允许的图片数量
 * @param currentImageCount - 当前已上传的图片数量
 */
export default function CameraCapture({
  onCapture,
  maxImages = 9,
  currentImageCount = 0,
  maxDimension = 768,
}: CameraCaptureProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isCapturing, setIsCapturing] = useState(false); // 正在处理拍照（processImage in-flight）
  const [error, setError] = useState<CaptureError | null>(null);
  const [stream, setStream] = useState<MediaStream | null>(null);

  const canCapture = currentImageCount < maxImages;

  /**
   * 启动摄像头
   */
  const startCamera = useCallback(async () => {
    if (isStreaming) return;

    setIsLoading(true);
    setError(null);

    try {
      const mediaStream = await navigator.mediaDevices.getUserMedia({
        video: {
          width: { ideal: 1280 },
          height: { ideal: 720 },
          facingMode: { ideal: 'environment' }, // 优先后置/外部摄像头，不强制要求（USB 摄像头兼容）
        },
        audio: false,
      });

      // videoRef 始终在 DOM 中（靠 className hidden 控制显隐），故此处 ref 一定有值
      if (videoRef.current) {
        videoRef.current.srcObject = mediaStream;
        await videoRef.current.play();
      }
      setStream(mediaStream);
      setIsStreaming(true);
    } catch (err) {
      console.error('摄像头访问失败:', err);

      if (err instanceof DOMException) {
        if (err.name === 'NotAllowedError') {
          setError({
            message: '摄像头访问被拒绝。请在浏览器设置中允许访问摄像头。',
            type: 'permission',
          });
        } else if (err.name === 'NotFoundError') {
          setError({
            message: '未找到摄像头设备。请确保已连接摄像头。',
            type: 'not-found',
          });
        } else {
          setError({
            message: `摄像头访问失败: ${err.message}`,
            type: 'unknown',
          });
        }
      } else {
        setError({
          message: '未知错误，请重试。',
          type: 'unknown',
        });
      }
    } finally {
      setIsLoading(false);
    }
  }, [isStreaming]);

  /**
   * 停止摄像头
   */
  const stopCamera = useCallback(() => {
    if (stream) {
      stream.getTracks().forEach(track => track.stop());
      setStream(null);
    }
    setIsStreaming(false);
  }, [stream]);

  /**
   * 拍照 - 使用后端 Sharp 处理
   */
  const capturePhoto = useCallback(async () => {
    if (!videoRef.current || !canvasRef.current || !canCapture || isCapturing) return;

    setIsCapturing(true);

    const video = videoRef.current;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');

    if (!ctx) return;

    // 设置画布尺寸（由 config.ini camera_max_dimension 控制，默认 768px）
    let { videoWidth, videoHeight } = video;

    if (videoWidth > videoHeight && videoWidth > maxDimension) {
      videoHeight = (videoHeight * maxDimension) / videoWidth;
      videoWidth = maxDimension;
    } else if (videoHeight > maxDimension) {
      videoWidth = (videoWidth * maxDimension) / videoHeight;
      videoHeight = maxDimension;
    }

    canvas.width = videoWidth;
    canvas.height = videoHeight;

    // 绘制当前视频帧
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

    // 获取原始字节（避免 base64 中间格式，走二进制上传链路）
    try {
      const blob = await new Promise<Blob>((resolve, reject) => {
        canvas.toBlob(
          b => b ? resolve(b) : reject(new Error('canvas.toBlob 返回 null')),
          'image/jpeg',
          0.9
        );
      });
      const file = new File([blob], 'capture.jpg', { type: 'image/jpeg' });
      const processed = await llmClient.processImageBinary(file, 'auto');
      onCapture(processed.image);
    } catch (processErr) {
      // 如果后端处理失败，直接使用 canvas 原始图片（降级处理，不再走后端旧端点）
      console.warn('图片后端处理失败，使用原始图片:', processErr);
      onCapture(canvas.toDataURL('image/jpeg', 0.9));
    } finally {
      setIsCapturing(false);
    }

    // 拍照后自动停止摄像头
    stopCamera();
  }, [onCapture, canCapture, isCapturing, stopCamera, maxDimension]);

  /**
   * 组件卸载时停止摄像头
   */
  useEffect(() => {
    return () => {
      if (stream) {
        stream.getTracks().forEach(track => track.stop());
      }
    };
  }, [stream]);

  return (
    <div className="flex flex-col items-center gap-3">
      {/* 隐藏的 canvas，用于截图 */}
      <canvas ref={canvasRef} className="hidden" />

      {/* 摄像头预览区域 */}
      <div className="relative w-full max-w-md aspect-video bg-gray-900 rounded-lg overflow-hidden">
        {/* video 元素始终挂载，避免 startCamera 时 ref 为 null */}
        <video
          ref={videoRef}
          className={`w-full h-full object-cover ${isStreaming ? '' : 'hidden'}`}
          playsInline
          muted
        />

        {isStreaming ? (
          <>
            {/* 拍照按钮 */}
            <button
              onClick={capturePhoto}
              disabled={!canCapture || isCapturing}
              className={`absolute bottom-4 left-1/2 -translate-x-1/2 
                flex items-center justify-center w-14 h-14 
                rounded-full border-4 border-white 
                transition-all duration-200
                ${canCapture && !isCapturing
                  ? 'bg-red-500 hover:bg-red-600 cursor-pointer'
                  : 'bg-gray-500 cursor-not-allowed'
                }`}
              title={canCapture ? (isCapturing ? '处理中...' : '点击拍照') : '已达到最大图片数量'}
            >
              <Circle className="w-6 h-6 text-white fill-white" />
            </button>
            {/* 关闭按钮 */}
            <button
              onClick={stopCamera}
              className="absolute top-2 right-2 p-2 bg-black/50 rounded-full 
                hover:bg-black/70 transition-colors"
              title="关闭摄像头"
            >
              <CameraOff className="w-5 h-5 text-white" />
            </button>
          </>
        ) : isLoading ? (
          <div className="w-full h-full flex items-center justify-center">
            <Loader2 className="w-8 h-8 text-white animate-spin" />
          </div>
        ) : error ? (
          <div className="w-full h-full flex flex-col items-center justify-center p-4">
            <CameraOff className="w-12 h-12 text-red-400 mb-3" />
            <p className="text-red-400 text-sm text-center">{error.message}</p>
            <button
              onClick={startCamera}
              className="mt-3 px-4 py-2 bg-red-500/20 hover:bg-red-500/30 
                text-red-400 rounded-lg text-sm transition-colors"
            >
              重试
            </button>
          </div>
        ) : (
          <div className="w-full h-full flex flex-col items-center justify-center">
            <Camera className="w-12 h-12 text-gray-500 mb-3" />
            <p className="text-gray-400 text-sm mb-3">点击开启摄像头</p>
            <button
              onClick={startCamera}
              disabled={!canCapture}
              className={`px-4 py-2 rounded-lg text-sm transition-colors
                ${canCapture
                  ? 'bg-nvidia-500 hover:bg-nvidia-600 text-white'
                  : 'bg-gray-600 text-gray-400 cursor-not-allowed'
                }`}
            >
              开启摄像头
            </button>
          </div>
        )}
      </div>

      {/* 提示信息 */}
      {!isStreaming && !isLoading && !error && (
        <p className="text-xs text-gray-500">
          {canCapture
            ? `还可以上传 ${maxImages - currentImageCount} 张图片`
            : '已达到最大图片数量'
          }
        </p>
      )}
    </div>
  );
}
