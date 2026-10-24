import type { ImageResource, TickBundle } from "./types.ts";

export interface ObjectUrlApi {
  createObjectURL(blob: Blob): string;
  revokeObjectURL(url: string): void;
}

function defaultObjectUrlApi(): ObjectUrlApi {
  return {
    createObjectURL: (blob) => URL.createObjectURL(blob),
    revokeObjectURL: (url) => URL.revokeObjectURL(url),
  };
}

export class OwnedImageResource implements ImageResource {
  uploadBlob: Blob | null;
  thumbnailBlob: Blob | null;
  disposed = false;

  private readonly objectUrls = new Set<string>();
  private readonly objectUrlApi: ObjectUrlApi;

  constructor(uploadBlob: Blob, thumbnailBlob: Blob | null = null, objectUrlApi?: ObjectUrlApi) {
    this.uploadBlob = uploadBlob;
    this.thumbnailBlob = thumbnailBlob;
    this.objectUrlApi = objectUrlApi ?? defaultObjectUrlApi();
  }

  acquireObjectURL(kind: "upload" | "thumbnail"): string {
    if (this.disposed) throw new Error("Image resource is disposed");
    const blob = kind === "upload" ? this.uploadBlob : this.thumbnailBlob;
    if (blob === null) throw new Error(`Image resource has no ${kind} Blob`);
    const url = this.objectUrlApi.createObjectURL(blob);
    this.objectUrls.add(url);
    return url;
  }

  releaseObjectURL(url: string): boolean {
    if (!this.objectUrls.delete(url)) return false;
    this.objectUrlApi.revokeObjectURL(url);
    return true;
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.objectUrls.forEach((url) => this.objectUrlApi.revokeObjectURL(url));
    this.objectUrls.clear();
    this.uploadBlob = null;
    this.thumbnailBlob = null;
  }
}

export function disposeBundle(bundle: TickBundle): void {
  const resources = new Set(bundle.frames.map((frame) => frame.resource));
  resources.forEach((resource) => {
    if (!resource.disposed) resource.dispose();
  });
}
