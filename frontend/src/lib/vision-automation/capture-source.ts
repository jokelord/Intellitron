import { OwnedImageResource } from "./image-resources.ts";
import {
  browserMediaSourceRegistry,
  type MediaSourceRegistry,
  type MediaStreamLease,
} from "./media-source-registry.ts";
import type {
  AutomationBinding,
  AutomationFrame,
  ImageResource,
  SourceDescriptor,
} from "./types.ts";

export interface CaptureCallbackToken {
  pageInstanceId?: string;
  pageGeneration: number;
  mode?: AutomationBinding["mode"];
  contextId?: string;
  automationTaskId: string;
  captureCycleId: string;
  cycleGeneration: number;
}

export interface FullCaptureCallbackToken extends AutomationBinding {
  captureCycleId: string;
  cycleGeneration: number;
}

export interface CaptureSource {
  readonly descriptor: SourceDescriptor;
  capture(signal: AbortSignal): Promise<AutomationFrame>;
  onEnded(listener: () => void): () => void;
  dispose(): void;
}

export interface CaptureFrameContext {
  stream: MediaStream;
  descriptor: SourceDescriptor;
  signal: AbortSignal;
}

export interface CreateLeasedCaptureSourceOptions {
  descriptor: SourceDescriptor;
  lease: MediaStreamLease;
  captureFrame: (context: CaptureFrameContext) => Promise<ImageResource>;
  now?: () => number;
}

export interface BrowserFrameCaptureOptions {
  maxDimensionPx: number;
  thumbnailSizePx: number;
  jpegQuality: number;
}

export function raceWithAbort<T>(work: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => {
      cleanup();
      reject(signal.reason);
    };
    const cleanup = () => signal.removeEventListener("abort", onAbort);
    signal.addEventListener("abort", onAbort, { once: true });
    void work.then(
      (value) => {
        cleanup();
        resolve(value);
      },
      (error) => {
        cleanup();
        reject(error);
      },
    );
  });
}

export function createLeasedCaptureSource(
  options: CreateLeasedCaptureSourceOptions,
): CaptureSource {
  let disposed = false;
  let released = false;
  const endedListeners = new Set<() => void>();
  const onTrackEnded = () => endedListeners.forEach((listener) => listener());
  options.lease.stream.getTracks().forEach((track) => track.addEventListener("ended", onTrackEnded));
  return {
    descriptor: { ...options.descriptor },
    async capture(signal) {
      if (disposed) throw new Error("Capture source is disposed");
      if (signal.aborted) throw signal.reason;
      const pendingResource = Promise.resolve(options.captureFrame({
        stream: options.lease.stream,
        descriptor: options.descriptor,
        signal,
      }));
      void pendingResource.then((resource) => {
        if (signal.aborted || disposed) resource.dispose();
      }, () => undefined);
      const resource = await raceWithAbort(pendingResource, signal);
      if (signal.aborted || disposed) {
        resource.dispose();
        if (disposed && !signal.aborted) throw new Error("Capture source is disposed");
        throw signal.reason;
      }
      return {
        source: options.descriptor,
        resource,
        capturedAtMs: (options.now ?? Date.now)(),
      };
    },
    onEnded(listener) {
      endedListeners.add(listener);
      return () => endedListeners.delete(listener);
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      options.lease.stream.getTracks().forEach((track) => track.removeEventListener("ended", onTrackEnded));
      endedListeners.clear();
      if (!released) {
        released = true;
        options.lease.release();
      }
    },
  };
}

export async function acquireCameraCaptureSource(options: {
  ownerId: string;
  descriptor: SourceDescriptor;
  captureDimensionPx: number;
  frame: BrowserFrameCaptureOptions;
  signal?: AbortSignal;
  registry?: MediaSourceRegistry;
}): Promise<CaptureSource> {
  if (!options.descriptor.deviceId) throw new Error("Camera source requires deviceId");
  const lease = await (options.registry ?? browserMediaSourceRegistry).acquireCamera({
    ownerId: options.ownerId,
    deviceId: options.descriptor.deviceId,
    width: options.captureDimensionPx,
    height: options.captureDimensionPx,
    signal: options.signal,
  });
  return createLeasedCaptureSource({
    descriptor: options.descriptor,
    lease,
    captureFrame: (context) => captureBrowserFrame(context, options.frame),
  });
}

export async function acquireDisplayCaptureSource(options: {
  ownerId: string;
  descriptor: SourceDescriptor;
  requestDisplay: () => Promise<MediaStream>;
  frame: BrowserFrameCaptureOptions;
  signal?: AbortSignal;
  registry?: MediaSourceRegistry;
}): Promise<CaptureSource> {
  const lease = await (options.registry ?? browserMediaSourceRegistry).acquireDisplay({
    ownerId: options.ownerId,
    requestDisplay: options.requestDisplay,
    signal: options.signal,
  });
  return createLeasedCaptureSource({
    descriptor: options.descriptor,
    lease,
    captureFrame: (context) => captureBrowserFrame(context, options.frame),
  });
}

export function requestBrowserDisplayMedia(): Promise<MediaStream> {
  return navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
}

export async function captureBrowserFrame(
  context: CaptureFrameContext,
  options: BrowserFrameCaptureOptions,
): Promise<ImageResource> {
  if (context.signal.aborted) throw context.signal.reason;
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.srcObject = context.stream;
  try {
    await raceWithAbort(Promise.resolve().then(() => video.play()), context.signal);
    if (video.videoWidth === 0 || video.videoHeight === 0) {
      await waitForVideoFrame(video, context.signal);
    }
    const scale = Math.min(1, options.maxDimensionPx / Math.max(video.videoWidth, video.videoHeight));
    const width = Math.max(1, Math.floor(video.videoWidth * scale));
    const height = Math.max(1, Math.floor(video.videoHeight * scale));
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const draw = canvas.getContext("2d");
    if (draw === null) throw new Error("Canvas 2D context is unavailable");
    draw.drawImage(video, 0, 0, width, height);
    const uploadBlob = await canvasToBlob(canvas, options.jpegQuality / 100, context.signal);

    const thumbnail = document.createElement("canvas");
    thumbnail.width = options.thumbnailSizePx;
    thumbnail.height = options.thumbnailSizePx;
    const thumbnailDraw = thumbnail.getContext("2d");
    if (thumbnailDraw === null) throw new Error("Thumbnail canvas context is unavailable");
    thumbnailDraw.drawImage(canvas, 0, 0, options.thumbnailSizePx, options.thumbnailSizePx);
    const thumbnailBlob = await canvasToBlob(thumbnail, 0.72, context.signal);
    return new OwnedImageResource(uploadBlob, thumbnailBlob);
  } finally {
    video.pause();
    video.srcObject = null;
  }
}

function waitForVideoFrame(video: HTMLVideoElement, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason);
      return;
    }
    const cleanup = () => {
      video.removeEventListener("loadeddata", onLoaded);
      signal.removeEventListener("abort", onAbort);
    };
    const onLoaded = () => {
      cleanup();
      resolve();
    };
    const onAbort = () => {
      cleanup();
      reject(signal.reason);
    };
    video.addEventListener("loadeddata", onLoaded, { once: true });
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function canvasToBlob(canvas: HTMLCanvasElement, quality: number, signal: AbortSignal): Promise<Blob> {
  if (signal.aborted) return Promise.reject(signal.reason);
  const encoding = new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob === null) reject(new Error("Canvas JPEG encoding returned null"));
      else resolve(blob);
    }, "image/jpeg", quality);
  });
  return raceWithAbort(encoding, signal);
}
