/**
 * 多摄像头捕获组件
 *
 * 枚举所有 videoinput 设备，以响应式网格展示实时预览，
 * 每个摄像头拥有独立的开启 / 拍照 / 关闭控制。
 * 支持"开启所有"/"关闭所有"批量操作与"刷新设备列表"。
 *
 * 优化：
 * - 模块级缓存：设备列表、活跃流、权限状态在组件卸载后保留，
 *   避免每次挂载重新枚举和请求权限。
 * - 页面卸载时自动清理所有流。
 */

'use client';

import React, { useRef, useState, useCallback, useEffect } from 'react';
import { Camera, CameraOff, Circle, Loader2, RefreshCw, X } from 'lucide-react';
import { llmClient } from '@/lib/api';

// ============================================================
// Types
// ============================================================

interface CameraCaptureProps {
  onCapture: (imageData: string) => void;
  maxImages?: number;
  currentImageCount?: number;
  maxDimension?: number;
}

interface CameraDevice {
  deviceId: string;
  label: string;
}

interface CameraState {
  stream: MediaStream | null;
  isLoading: boolean;
  isCapturing: boolean;
  error: string | null;
}

// ============================================================
// Module-level cache (survives component unmount/remount)
// ============================================================

let _cachedDevices: CameraDevice[] | null = null;
let _cachedStreams: Map<string, MediaStream> = new Map();
let _permissionGranted = false;

/** Stop all cached streams and clear the cache entirely.
 *
 * Exported so that other components (e.g. cycle mode) can release the
 * browser's exclusive camera lock before the backend opens the same devices
 * via OpenCV.
 *
 * Also resets _cachedDevices and _permissionGranted so that the next time
 * CameraCapture mounts it starts from a clean state (re-enumerate devices,
 * re-request permission). This prevents stale device handles from lingering
 * after a full destroy.
 */
export function destroyAllCameraStreams() {
  _cachedStreams.forEach(stream => {
    stream.getTracks().forEach(t => t.stop());
  });
  _cachedStreams.clear();
  _cachedDevices = null;
  _permissionGranted = false;
}

// Clean up streams when the page unloads (navigation / tab close)
if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', () => {
    destroyAllCameraStreams();
  });
}

// ============================================================
// CameraCard — single camera preview + controls
// ============================================================

interface CameraCardProps {
  device: CameraDevice;
  state: CameraState;
  onStart: () => void;
  onStop: () => void;
  onCapture: () => void;
  canCapture: boolean;
  compact: boolean;
}

function CameraCard({
  device,
  state,
  onStart,
  onStop,
  onCapture,
  canCapture,
  compact,
}: CameraCardProps) {
  const videoRef = useRef<HTMLVideoElement>(null);

  // Attach / detach stream to <video> element
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    if (state.stream) {
      video.srcObject = state.stream;
      video.play().catch(() => {});
    } else {
      video.srcObject = null;
    }
  }, [state.stream]);

  const isStreaming = !!state.stream;

  return (
    <div className="flex flex-col gap-1.5">
      {/* Device label */}
      <p className={`text-gray-300 truncate ${compact ? 'text-xs' : 'text-sm'}`}>
        {device.label || `摄像头 ${device.deviceId.slice(0, 8)}`}
      </p>

      {/* Preview area */}
      <div
        className={`relative w-full aspect-video bg-gray-900 rounded-lg overflow-hidden ${
          compact ? 'max-w-full' : 'max-w-md'
        }`}
      >
        {/* video element is always mounted; hidden when not streaming */}
        <video
          ref={videoRef}
          data-device-id={device.deviceId}
          className={`w-full h-full object-cover ${isStreaming ? '' : 'hidden'}`}
          playsInline
          muted
        />

        {isStreaming ? (
          <>
            {/* Capture button */}
            <button
              onClick={onCapture}
              disabled={!canCapture || state.isCapturing}
              className={`absolute bottom-3 left-1/2 -translate-x-1/2
                flex items-center justify-center
                ${compact ? 'w-10 h-10 border-[3px]' : 'w-14 h-14 border-4'}
                rounded-full border-white transition-all duration-200
                ${
                  canCapture && !state.isCapturing
                    ? 'bg-red-500 hover:bg-red-600 cursor-pointer'
                    : 'bg-gray-500 cursor-not-allowed'
                }`}
              title={
                state.isCapturing
                  ? '处理中...'
                  : canCapture
                    ? '点击拍照'
                    : '已达到最大图片数量'
              }
            >
              {state.isCapturing ? (
                <Loader2 className={`${compact ? 'w-4 h-4' : 'w-5 h-5'} text-white animate-spin`} />
              ) : (
                <Circle className={`${compact ? 'w-4 h-4' : 'w-6 h-6'} text-white fill-white`} />
              )}
            </button>

            {/* Close button */}
            <button
              onClick={onStop}
              className="absolute top-2 right-2 p-1.5 bg-black/50 rounded-full
                hover:bg-black/70 transition-colors"
              title="关闭摄像头"
            >
              <CameraOff className={`${compact ? 'w-4 h-4' : 'w-5 h-5'} text-white`} />
            </button>
          </>
        ) : state.isLoading ? (
          <div className="w-full h-full flex items-center justify-center">
            <Loader2 className="w-8 h-8 text-white animate-spin" />
          </div>
        ) : state.error ? (
          <div className="w-full h-full flex flex-col items-center justify-center p-3">
            <CameraOff className={`${compact ? 'w-8 h-8' : 'w-10 h-10'} text-red-400 mb-2`} />
            <p className="text-red-400 text-xs text-center line-clamp-2">{state.error}</p>
            <button
              onClick={onStart}
              className="mt-2 px-3 py-1.5 bg-red-500/20 hover:bg-red-500/30
                text-red-400 rounded-lg text-xs transition-colors"
            >
              重试
            </button>
          </div>
        ) : (
          <div className="w-full h-full flex flex-col items-center justify-center">
            <Camera className={`${compact ? 'w-8 h-8' : 'w-10 h-10'} text-gray-500 mb-2`} />
            <button
              onClick={onStart}
              disabled={!canCapture}
              className={`px-3 py-1.5 rounded-lg text-xs transition-colors
                ${
                  canCapture
                    ? 'bg-nvidia-500 hover:bg-nvidia-600 text-white'
                    : 'bg-gray-600 text-gray-400 cursor-not-allowed'
                }`}
            >
              开启摄像头
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ============================================================
// CameraCapture — main component (default export)
// ============================================================

const DEFAULT_CAMERA_STATE: CameraState = {
  stream: null,
  isLoading: false,
  isCapturing: false,
  error: null,
};

export default function CameraCapture({
  onCapture,
  maxImages = 9,
  currentImageCount = 0,
  maxDimension = 768,
}: CameraCaptureProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [devices, setDevices] = useState<CameraDevice[]>(_cachedDevices ?? []);
  const [cameraStates, setCameraStates] = useState<Map<string, CameraState>>(new Map());
  const [isEnumerating, setIsEnumerating] = useState(false);

  const canCapture = currentImageCount < maxImages;

  // ---- helpers to update per-device state ----

  const updateCameraState = useCallback(
    (deviceId: string, patch: Partial<CameraState>) => {
      setCameraStates(prev => {
        const next = new Map(prev);
        const current = next.get(deviceId) ?? { ...DEFAULT_CAMERA_STATE };
        next.set(deviceId, { ...current, ...patch });
        return next;
      });
    },
    [],
  );

  const getCameraState = useCallback(
    (deviceId: string): CameraState => cameraStates.get(deviceId) ?? { ...DEFAULT_CAMERA_STATE },
    [cameraStates],
  );

  // ---- enumerate devices ----

  const enumerateDevices = useCallback(async () => {
    setIsEnumerating(true);
    try {
      // Always call getUserMedia at least once per page session before
      // enumerateDevices. On Linux/Chromium with V4L2 drivers,
      // enumerateDevices() without a prior getUserMedia() in the current
      // session may return an incomplete device list (e.g. only 1 camera
      // instead of 2). navigator.permissions.query({name:'camera'}) cannot
      // substitute for this — it only reflects the browser-level permission
      // state, not whether the media subsystem has been activated.
      if (!_permissionGranted) {
        const tempStream = await navigator.mediaDevices.getUserMedia({ video: true });
        tempStream.getTracks().forEach(t => t.stop());
        _permissionGranted = true;
      }

      const allDevices = await navigator.mediaDevices.enumerateDevices();
      const videoDevices: CameraDevice[] = allDevices
        .filter(d => d.kind === 'videoinput')
        .map((d, idx) => ({
          deviceId: d.deviceId,
          label: d.label || `摄像头 ${idx + 1}`,
        }));

      // Update module-level cache
      _cachedDevices = videoDevices;
      setDevices(videoDevices);

      // Initialise state entries, restoring cached streams where available
      setCameraStates(prev => {
        const next = new Map(prev);
        for (const d of videoDevices) {
          if (!next.has(d.deviceId)) {
            // Check if we have a cached stream that's still active
            const cachedStream = _cachedStreams.get(d.deviceId);
            if (cachedStream && cachedStream.active) {
              next.set(d.deviceId, {
                ...DEFAULT_CAMERA_STATE,
                stream: cachedStream,
              });
            } else {
              // Clean up stale cached stream
              if (cachedStream) {
                _cachedStreams.delete(d.deviceId);
              }
              next.set(d.deviceId, { ...DEFAULT_CAMERA_STATE });
            }
          } else {
            // Entry already exists — check if stream died
            const existing = next.get(d.deviceId)!;
            if (existing.stream && !existing.stream.active) {
              _cachedStreams.delete(d.deviceId);
              next.set(d.deviceId, { ...DEFAULT_CAMERA_STATE });
            }
          }
        }
        // Remove entries for devices no longer present
        for (const key of Array.from(next.keys())) {
          if (!videoDevices.some(d => d.deviceId === key)) {
            const old = next.get(key);
            if (old?.stream) {
              old.stream.getTracks().forEach(t => t.stop());
              _cachedStreams.delete(key);
            }
            next.delete(key);
          }
        }
        return next;
      });
    } catch (err) {
      console.error('设备枚举失败:', err);
      setDevices([]);
    } finally {
      setIsEnumerating(false);
    }
  }, []);

  // Initial enumeration on mount — skip if cache is valid
  useEffect(() => {
    if (_cachedDevices && _cachedDevices.length > 0) {
      // Restore from cache instantly (no enumeration delay)
      setDevices(_cachedDevices);
      setCameraStates(() => {
        const restored = new Map<string, CameraState>();
        for (const d of _cachedDevices!) {
          const cachedStream = _cachedStreams.get(d.deviceId);
          if (cachedStream && cachedStream.active) {
            restored.set(d.deviceId, {
              ...DEFAULT_CAMERA_STATE,
              stream: cachedStream,
            });
          } else {
            if (cachedStream) _cachedStreams.delete(d.deviceId);
            restored.set(d.deviceId, { ...DEFAULT_CAMERA_STATE });
          }
        }
        return restored;
      });
    } else {
      enumerateDevices();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- start / stop per device ----

  const startCamera = useCallback(
    async (deviceId: string) => {
      const current = getCameraState(deviceId);
      if (current.stream || current.isLoading) return;

      // Check if we have a cached stream that's still active
      const cachedStream = _cachedStreams.get(deviceId);
      if (cachedStream && cachedStream.active) {
        updateCameraState(deviceId, { stream: cachedStream, isLoading: false });
        return;
      }

      updateCameraState(deviceId, { isLoading: true, error: null });

      try {
        const mediaStream = await navigator.mediaDevices.getUserMedia({
          video: {
            deviceId: { exact: deviceId },
            width: { ideal: 1280 },
            height: { ideal: 720 },
          },
          audio: false,
        });
        // Store in module-level cache
        _cachedStreams.set(deviceId, mediaStream);
        _permissionGranted = true;
        updateCameraState(deviceId, { stream: mediaStream, isLoading: false });
      } catch (err) {
        let message = '摄像头访问失败';
        if (err instanceof DOMException) {
          if (err.name === 'NotAllowedError') {
            message = '摄像头访问被拒绝。请在浏览器设置中允许访问摄像头。';
          } else if (err.name === 'NotFoundError') {
            message = '未找到该摄像头设备。';
          } else {
            message = `摄像头访问失败: ${err.message}`;
          }
        }
        updateCameraState(deviceId, { isLoading: false, error: message });
      }
    },
    [getCameraState, updateCameraState],
  );

  const stopCamera = useCallback(
    (deviceId: string) => {
      const current = getCameraState(deviceId);
      if (current.stream) {
        current.stream.getTracks().forEach(t => t.stop());
        _cachedStreams.delete(deviceId);
      }
      updateCameraState(deviceId, { stream: null, isLoading: false, error: null });
    },
    [getCameraState, updateCameraState],
  );

  // ---- capture photo ----

  const capturePhoto = useCallback(
    async (deviceId: string) => {
      const canvas = canvasRef.current;
      if (!canvas || !canCapture) return;

      const state = getCameraState(deviceId);
      if (!state.stream || state.isCapturing) return;

      updateCameraState(deviceId, { isCapturing: true });

      const video = document.querySelector<HTMLVideoElement>(
        `video[data-device-id="${deviceId}"]`,
      );
      if (!video) {
        updateCameraState(deviceId, { isCapturing: false });
        return;
      }

      const ctx = canvas.getContext('2d');
      if (!ctx) {
        updateCameraState(deviceId, { isCapturing: false });
        return;
      }

      // Scale to maxDimension
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
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

      try {
        const blob = await new Promise<Blob>((resolve, reject) => {
          canvas.toBlob(
            b => (b ? resolve(b) : reject(new Error('canvas.toBlob 返回 null'))),
            'image/jpeg',
            0.9,
          );
        });
        const file = new File([blob], 'capture.jpg', { type: 'image/jpeg' });
        const processed = await llmClient.processImageBinary(file, 'auto');
        onCapture(processed.image);
      } catch (processErr) {
        console.warn('图片后端处理失败，使用原始图片:', processErr);
        onCapture(canvas.toDataURL('image/jpeg', 0.9));
      } finally {
        updateCameraState(deviceId, { isCapturing: false });
      }
      // NOTE: photo capture does NOT auto-stop the camera
    },
    [canCapture, getCameraState, updateCameraState, maxDimension, onCapture],
  );

  // ---- batch controls ----

  const startAll = useCallback(() => {
    for (const d of devices) {
      const s = getCameraState(d.deviceId);
      if (!s.stream && !s.isLoading) {
        startCamera(d.deviceId);
      }
    }
  }, [devices, getCameraState, startCamera]);

  const stopAll = useCallback(() => {
    for (const d of devices) {
      stopCamera(d.deviceId);
    }
  }, [devices, stopCamera]);

  /** Stop all streams, clear the module-level cache, and reset state. */
  const destroyAll = useCallback(() => {
    destroyAllCameraStreams();
    _cachedDevices = null;
    _permissionGranted = false;
    setCameraStates(prev => {
      const next = new Map<string, CameraState>();
      prev.forEach((_, key) => {
        next.set(key, { ...DEFAULT_CAMERA_STATE });
      });
      return next;
    });
  }, []);

  // ---- cleanup on unmount: detach only, keep streams alive ----

  useEffect(() => {
    return () => {
      // Do NOT stop streams on unmount — they live in _cachedStreams.
      // Just clear the React state so video elements detach naturally.
      // Streams will be reattached on next mount.
    };
  }, []);

  // ---- grid columns helper ----

  const gridCols =
    devices.length <= 1
      ? 'grid-cols-1'
      : devices.length === 2
        ? 'grid-cols-1 sm:grid-cols-2'
        : 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3';

  const compact = devices.length > 1;

  // Check if any stream is active (for showing destroy button)
  const hasActiveStreams = _cachedStreams.size > 0;

  // ============================================================
  // Render
  // ============================================================

  // Loading state
  if (isEnumerating) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-8">
        <Loader2 className="w-8 h-8 text-gray-400 animate-spin" />
        <p className="text-gray-400 text-sm">正在检测摄像头设备…</p>
      </div>
    );
  }

  // No devices found
  if (devices.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-8">
        <CameraOff className="w-12 h-12 text-gray-500" />
        <p className="text-gray-400 text-sm">未找到摄像头设备</p>
        <button
          onClick={enumerateDevices}
          className="flex items-center gap-1.5 px-4 py-2 bg-nvidia-500 hover:bg-nvidia-600
            text-white rounded-lg text-sm transition-colors"
        >
          <RefreshCw className="w-4 h-4" />
          重新检测
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {/* Hidden shared canvas for capture */}
      <canvas ref={canvasRef} className="hidden" />

      {/* Toolbar: master controls + refresh */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          {devices.length > 1 && (
            <>
              <button
                onClick={startAll}
                disabled={!canCapture}
                className={`flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs transition-colors
                  ${
                    canCapture
                      ? 'bg-nvidia-500 hover:bg-nvidia-600 text-white'
                      : 'bg-gray-600 text-gray-400 cursor-not-allowed'
                  }`}
              >
                <Camera className="w-3.5 h-3.5" />
                开启所有
              </button>
              <button
                onClick={stopAll}
                className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs
                  bg-gray-700 hover:bg-gray-600 text-gray-200 transition-colors"
              >
                <CameraOff className="w-3.5 h-3.5" />
                关闭所有
              </button>
            </>
          )}
          {hasActiveStreams && (
            <button
              onClick={destroyAll}
              className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs
                bg-red-700 hover:bg-red-600 text-white transition-colors"
              title="彻底关闭所有摄像头并释放资源"
            >
              <X className="w-3.5 h-3.5" />
              释放所有摄像头
            </button>
          )}
        </div>

        <button
          onClick={enumerateDevices}
          className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs
            bg-gray-700 hover:bg-gray-600 text-gray-200 transition-colors"
          title="刷新设备列表"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          刷新设备列表
        </button>
      </div>

      {/* Camera grid */}
      <div className={`grid ${gridCols} gap-3`}>
        {devices.map(device => (
          <CameraCard
            key={device.deviceId}
            device={device}
            state={getCameraState(device.deviceId)}
            onStart={() => startCamera(device.deviceId)}
            onStop={() => stopCamera(device.deviceId)}
            onCapture={() => capturePhoto(device.deviceId)}
            canCapture={canCapture}
            compact={compact}
          />
        ))}
      </div>

      {/* Remaining capacity hint */}
      <p className="text-xs text-gray-500 text-center">
        {canCapture
          ? `还可以上传 ${maxImages - currentImageCount} 张图片`
          : '已达到最大图片数量'}
      </p>
    </div>
  );
}
