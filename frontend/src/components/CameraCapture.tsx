/** Browser-camera manual photo capture backed by owner-leased media streams. */
'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Camera, CameraOff, Circle, Loader2, RefreshCw } from 'lucide-react';
import { llmClient } from '@/lib/api';
import {
  browserMediaSourceRegistry,
  type CameraDeviceDescriptor,
} from '@/lib/vision-automation/media-source-registry';

export const MANUAL_CAMERA_OWNER_ID = 'manual-photo';

interface CameraCaptureProps {
  onCapture: (imageData: string) => void;
  maxImages?: number;
  currentImageCount?: number;
  maxDimension?: number;
  captureDimension?: number;
}

interface CameraState {
  stream: MediaStream | null;
  isLoading: boolean;
  isCapturing: boolean;
  error: string | null;
}

const DEFAULT_CAMERA_STATE: CameraState = {
  stream: null,
  isLoading: false,
  isCapturing: false,
  error: null,
};

/** Release only manual-photo leases. Automation leases on the same streams survive. */
export function stopAllCameraStreams() {
  browserMediaSourceRegistry.releaseOwner(MANUAL_CAMERA_OWNER_ID);
}

/** Release manual-photo leases and force fresh device discovery next time. */
export function destroyAllCameraStreams() {
  stopAllCameraStreams();
  browserMediaSourceRegistry.clearDeviceCache();
}

function CameraCard({
  device,
  state,
  onStart,
  onStop,
  onCapture,
  canCapture,
  compact,
}: {
  device: CameraDeviceDescriptor;
  state: CameraState;
  onStart: () => void;
  onStop: () => void;
  onCapture: () => void;
  canCapture: boolean;
  compact: boolean;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.srcObject = state.stream;
    if (state.stream) void video.play().catch(() => undefined);
    return () => {
      video.srcObject = null;
    };
  }, [state.stream]);

  return (
    <div className="flex flex-col gap-1.5">
      <p className={`truncate text-gray-300 ${compact ? 'text-xs' : 'text-sm'}`}>{device.label}</p>
      <div className="relative aspect-video w-full overflow-hidden rounded-lg bg-gray-900">
        <video
          ref={videoRef}
          data-device-id={device.deviceId}
          className={`h-full w-full object-cover ${state.stream ? '' : 'hidden'}`}
          playsInline
          muted
        />
        {state.stream ? (
          <>
            <button
              type="button"
              onClick={onCapture}
              disabled={!canCapture || state.isCapturing}
              title={state.isCapturing ? '处理中...' : canCapture ? '点击拍照' : '已达到最大图片数量'}
              className={`absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center justify-center rounded-full border-white transition-colors ${compact ? 'h-10 w-10 border-[3px]' : 'h-14 w-14 border-4'} ${canCapture ? 'bg-red-500 hover:bg-red-600' : 'cursor-not-allowed bg-gray-500'}`}
            >
              {state.isCapturing
                ? <Loader2 className="h-5 w-5 animate-spin text-white" />
                : <Circle className="h-5 w-5 fill-white text-white" />}
            </button>
            <button
              type="button"
              onClick={onStop}
              title="关闭摄像头"
              className="absolute right-2 top-2 rounded-full bg-black/50 p-1.5 hover:bg-black/70"
            >
              <CameraOff className="h-4 w-4 text-white" />
            </button>
          </>
        ) : state.isLoading ? (
          <div className="flex h-full items-center justify-center"><Loader2 className="h-8 w-8 animate-spin text-white" /></div>
        ) : state.error ? (
          <div className="flex h-full flex-col items-center justify-center p-3 text-center">
            <CameraOff className="mb-2 h-8 w-8 text-red-400" />
            <p className="line-clamp-2 text-xs text-red-400">{state.error}</p>
            <button type="button" onClick={onStart} className="mt-2 rounded-lg bg-red-500/20 px-3 py-1.5 text-xs text-red-300">重试</button>
          </div>
        ) : (
          <div className="flex h-full flex-col items-center justify-center">
            <Camera className="mb-2 h-8 w-8 text-gray-500" />
            <button type="button" onClick={onStart} disabled={!canCapture} className="rounded-lg bg-nvidia-500 px-3 py-1.5 text-xs text-white disabled:cursor-not-allowed disabled:bg-gray-600 disabled:text-gray-400">开启摄像头</button>
          </div>
        )}
      </div>
    </div>
  );
}

function cameraError(error: unknown): string {
  if (!(error instanceof DOMException)) return '摄像头访问失败';
  if (error.name === 'NotAllowedError') return '摄像头访问被拒绝。请在浏览器设置中允许访问摄像头。';
  if (error.name === 'NotFoundError') return '未找到该摄像头设备。';
  return `摄像头访问失败: ${error.message}`;
}

export default function CameraCapture({
  onCapture,
  maxImages = 9,
  currentImageCount = 0,
  maxDimension = 1024,
  captureDimension = 1280,
}: CameraCaptureProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [devices, setDevices] = useState<CameraDeviceDescriptor[]>([]);
  const [cameraStates, setCameraStates] = useState<Map<string, CameraState>>(new Map());
  const [isEnumerating, setIsEnumerating] = useState(false);
  const canCapture = currentImageCount < maxImages;

  const updateCameraState = useCallback((deviceId: string, patch: Partial<CameraState>) => {
    setCameraStates((previous) => {
      const next = new Map(previous);
      next.set(deviceId, { ...(next.get(deviceId) ?? DEFAULT_CAMERA_STATE), ...patch });
      return next;
    });
  }, []);

  const getCameraState = useCallback(
    (deviceId: string) => cameraStates.get(deviceId) ?? DEFAULT_CAMERA_STATE,
    [cameraStates],
  );

  const enumerateDevices = useCallback(async (refresh = false) => {
    setIsEnumerating(true);
    try {
      if (refresh) browserMediaSourceRegistry.clearDeviceCache();
      const cameras = await browserMediaSourceRegistry.enumerateCameraDevices();
      setDevices(cameras);
      setCameraStates((previous) => {
        const next = new Map<string, CameraState>();
        cameras.forEach((device) => {
          const leased = browserMediaSourceRegistry.getLeasedStream(`camera:${device.deviceId}`, MANUAL_CAMERA_OWNER_ID);
          next.set(device.deviceId, {
            ...(previous.get(device.deviceId) ?? DEFAULT_CAMERA_STATE),
            stream: leased,
            isLoading: false,
          });
        });
        return next;
      });
    } catch {
      setDevices([]);
    } finally {
      setIsEnumerating(false);
    }
  }, []);

  useEffect(() => {
    void enumerateDevices();
  }, [enumerateDevices]);

  const startCamera = useCallback(async (deviceId: string) => {
    const current = getCameraState(deviceId);
    if (current.stream || current.isLoading) return;
    updateCameraState(deviceId, { isLoading: true, error: null });
    try {
      const lease = await browserMediaSourceRegistry.acquireCamera({
        ownerId: MANUAL_CAMERA_OWNER_ID,
        deviceId,
        width: captureDimension,
        height: captureDimension,
      });
      updateCameraState(deviceId, { stream: lease.stream, isLoading: false });
    } catch (error) {
      updateCameraState(deviceId, { isLoading: false, error: cameraError(error) });
    }
  }, [captureDimension, getCameraState, updateCameraState]);

  const stopCamera = useCallback((deviceId: string) => {
    browserMediaSourceRegistry.releaseSource(`camera:${deviceId}`, MANUAL_CAMERA_OWNER_ID);
    updateCameraState(deviceId, { stream: null, isLoading: false, error: null });
  }, [updateCameraState]);

  const capturePhoto = useCallback(async (deviceId: string) => {
    const canvas = canvasRef.current;
    const state = getCameraState(deviceId);
    if (!canvas || !canCapture || !state.stream || state.isCapturing) return;
    const video = document.querySelector<HTMLVideoElement>(`video[data-device-id="${deviceId}"]`);
    const context = canvas.getContext('2d');
    if (!video || !context) return;
    updateCameraState(deviceId, { isCapturing: true });
    let { videoWidth, videoHeight } = video;
    const scale = Math.min(1, maxDimension / Math.max(videoWidth, videoHeight));
    videoWidth = Math.max(1, Math.floor(videoWidth * scale));
    videoHeight = Math.max(1, Math.floor(videoHeight * scale));
    canvas.width = videoWidth;
    canvas.height = videoHeight;
    context.drawImage(video, 0, 0, videoWidth, videoHeight);
    try {
      const blob = await new Promise<Blob>((resolve, reject) => {
        canvas.toBlob((value) => value ? resolve(value) : reject(new Error('canvas.toBlob 返回 null')), 'image/jpeg', 0.9);
      });
      const file = new File([blob], 'capture.jpg', { type: 'image/jpeg' });
      const processed = await llmClient.processImageBinary(file, 'auto');
      onCapture(processed.image);
    } catch (error) {
      console.warn('图片后端处理失败，使用原始图片:', error);
      onCapture(canvas.toDataURL('image/jpeg', 0.9));
    } finally {
      updateCameraState(deviceId, { isCapturing: false });
    }
  }, [canCapture, getCameraState, maxDimension, onCapture, updateCameraState]);

  const closeAllCameras = useCallback(() => {
    stopAllCameraStreams();
    setCameraStates((previous) => new Map(Array.from(previous.keys()).map((key) => [key, { ...DEFAULT_CAMERA_STATE }])));
  }, []);

  if (isEnumerating) {
    return <div className="flex flex-col items-center gap-3 py-8"><Loader2 className="h-8 w-8 animate-spin text-gray-400" /><p className="text-sm text-gray-400">正在检测摄像头设备…</p></div>;
  }
  if (devices.length === 0) {
    return (
      <div className="flex flex-col items-center gap-3 py-8">
        <CameraOff className="h-12 w-12 text-gray-500" />
        <p className="text-sm text-gray-400">未找到摄像头设备</p>
        <button type="button" onClick={() => void enumerateDevices(true)} className="rounded-lg bg-nvidia-500 px-4 py-2 text-sm text-white">重新检测</button>
      </div>
    );
  }

  const hasActiveStreams = Array.from(cameraStates.values()).some((state) => state.stream !== null);
  const compact = devices.length > 1;
  return (
    <div className="flex flex-col gap-3">
      <canvas ref={canvasRef} className="hidden" />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex gap-2">
          {devices.length > 1 && <button type="button" disabled={!canCapture} onClick={() => devices.forEach((device) => void startCamera(device.deviceId))} className="rounded-lg bg-nvidia-500 px-3 py-1.5 text-xs text-white disabled:bg-gray-600">开启所有</button>}
          {hasActiveStreams && (
            <button type="button" onClick={closeAllCameras} title="停止全部视频流并释放浏览器摄像头资源" className="rounded-lg bg-red-700 px-3 py-1.5 text-xs text-white hover:bg-red-600">
              关闭所有摄像头
            </button>
          )}
        </div>
        <button type="button" onClick={() => void enumerateDevices(true)} className="flex items-center gap-1 rounded-lg bg-gray-700 px-3 py-1.5 text-xs text-gray-200"><RefreshCw className="h-3.5 w-3.5" />刷新设备列表</button>
      </div>
      <div className={`grid gap-3 ${devices.length === 1 ? 'grid-cols-1' : 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3'}`}>
        {devices.map((device) => (
          <CameraCard
            key={device.deviceId}
            device={device}
            state={getCameraState(device.deviceId)}
            onStart={() => void startCamera(device.deviceId)}
            onStop={() => stopCamera(device.deviceId)}
            onCapture={() => void capturePhoto(device.deviceId)}
            canCapture={canCapture}
            compact={compact}
          />
        ))}
      </div>
      <p className="text-center text-xs text-gray-500">{canCapture ? `还可以上传 ${maxImages - currentImageCount} 张图片` : '已达到最大图片数量'}</p>
    </div>
  );
}
