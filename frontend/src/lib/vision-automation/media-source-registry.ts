export interface CameraDeviceDescriptor {
  deviceId: string;
  label: string;
}

export interface MediaStreamLease {
  ownerId: string;
  sourceKey: string;
  stream: MediaStream;
  release(): void;
}

export interface AcquireCameraOptions {
  ownerId: string;
  deviceId: string;
  width?: number;
  height?: number;
  signal?: AbortSignal;
}

export interface AcquireDisplayOptions {
  ownerId: string;
  requestDisplay: () => Promise<MediaStream>;
  signal?: AbortSignal;
}

export interface MediaSourceRegistryDependencies {
  getUserMedia?: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  enumerateDevices?: () => Promise<MediaDeviceInfo[]>;
}

export interface MediaSourceRegistryDiagnostics {
  streams: number;
  pendingStarts: number;
  pendingConsumers: number;
  pendingAttemptOwners: number;
  pendingAttempts: number;
  latestAttemptToken: number;
}

interface StreamEntry {
  stream: MediaStream;
  owners: Set<string>;
  stopped: boolean;
}

interface AcquisitionAttempt {
  token: number;
  ownerId: string;
  kind: "camera" | "display";
  valid: boolean;
}

function abortReason(signal: AbortSignal): Error {
  return signal.reason instanceof Error
    ? signal.reason
    : new DOMException("Media acquisition aborted", "AbortError");
}

export class MediaSourceRegistry {
  private readonly streams = new Map<string, StreamEntry>();
  private readonly pendingStarts = new Map<string, Promise<MediaStream>>();
  private readonly pendingConsumers = new Map<string, number>();
  private readonly pendingAttemptsByOwner = new Map<string, Set<AcquisitionAttempt>>();
  private readonly stoppedStreams = new WeakSet<MediaStream>();
  private cachedDevices: CameraDeviceDescriptor[] | null = null;
  private enumerationPromise: Promise<CameraDeviceDescriptor[]> | null = null;
  private permissionGranted = false;
  private lifecycleGeneration = 0;
  private deviceEnumerationGeneration = 0;
  private attemptSequence = 0;
  private readonly getUserMediaDependency?: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  private readonly enumerateDevicesDependency?: () => Promise<MediaDeviceInfo[]>;

  constructor(dependencies: MediaSourceRegistryDependencies = {}) {
    this.getUserMediaDependency = dependencies.getUserMedia;
    this.enumerateDevicesDependency = dependencies.enumerateDevices;
  }

  acquireCamera(options: AcquireCameraOptions): Promise<MediaStreamLease> {
    const sourceKey = `camera:${options.deviceId}`;
    const constraints: MediaStreamConstraints = {
      video: {
        deviceId: { exact: options.deviceId },
        ...(options.width === undefined ? {} : { width: { max: options.width } }),
        ...(options.height === undefined ? {} : { height: { max: options.height } }),
      },
      audio: false,
    };
    return this.acquire(
      sourceKey,
      options.ownerId,
      () => this.getUserMedia(constraints),
      options.signal,
    );
  }

  acquireDisplay(options: AcquireDisplayOptions): Promise<MediaStreamLease> {
    if (!options.ownerId.trim()) return Promise.reject(new Error("Media stream ownerId is required"));
    if (options.signal?.aborted) return Promise.reject(abortReason(options.signal));

    const sourceKey = `display:${options.ownerId}`;
    this.invalidateOwnerAttempts(options.ownerId, "display");
    const attempt = this.createAttempt(options.ownerId, "display");
    const lifecycleGeneration = this.lifecycleGeneration;
    this.releaseSource(sourceKey, options.ownerId);

    let request: Promise<MediaStream>;
    try {
      // Keep this invocation before any await so Retry retains its user gesture.
      request = Promise.resolve(options.requestDisplay());
    } catch (error) {
      request = Promise.reject(error);
    }

    const onAbort = () => {
      this.invalidateAttempt(attempt);
      this.releaseSource(sourceKey, options.ownerId);
    };
    options.signal?.addEventListener("abort", onAbort, { once: true });

    return request.then((stream) => {
      if (
        options.signal?.aborted
        || lifecycleGeneration !== this.lifecycleGeneration
        || !attempt.valid
      ) {
        this.stopStream(stream);
        if (options.signal?.aborted) throw abortReason(options.signal);
        throw new DOMException("Display acquisition generation changed", "AbortError");
      }

      const previous = this.streams.get(sourceKey);
      if (previous !== undefined) this.stopEntry(sourceKey, previous);
      const entry: StreamEntry = { stream, owners: new Set([options.ownerId]), stopped: false };
      this.streams.set(sourceKey, entry);
      this.permissionGranted = true;
      return this.createLease(sourceKey, options.ownerId, entry, "display");
    }).finally(() => {
      options.signal?.removeEventListener("abort", onAbort);
      this.invalidateAttempt(attempt);
    });
  }

  async enumerateCameraDevices(warmPermission = true): Promise<CameraDeviceDescriptor[]> {
    if (this.cachedDevices !== null) return [...this.cachedDevices];
    if (this.enumerationPromise !== null) return this.enumerationPromise;
    const lifecycleGeneration = this.lifecycleGeneration;
    const enumerationGeneration = this.deviceEnumerationGeneration;
    const request = (async () => {
      if (warmPermission && !this.permissionGranted) {
        const stream = await this.getUserMedia({
          video: { width: { ideal: 320 }, height: { ideal: 240 } },
          audio: false,
        });
        stream.getTracks().forEach((track) => track.stop());
        if (
          lifecycleGeneration !== this.lifecycleGeneration
          || enumerationGeneration !== this.deviceEnumerationGeneration
        ) return [];
        this.permissionGranted = true;
      }
      const devices = await this.enumerateDevices();
      if (
        lifecycleGeneration !== this.lifecycleGeneration
        || enumerationGeneration !== this.deviceEnumerationGeneration
      ) return [];
      const cameras = devices
        .filter((device) => device.kind === "videoinput")
        .map((device, index) => ({
          deviceId: device.deviceId,
          label: device.label || `摄像头 ${index + 1}`,
        }));
      this.cachedDevices = cameras;
      return [...cameras];
    })();
    this.enumerationPromise = request;
    const clearEnumeration = () => {
      if (this.enumerationPromise === request) this.enumerationPromise = null;
    };
    void request.then(clearEnumeration, clearEnumeration);
    return request;
  }

  clearDeviceCache(): void {
    this.deviceEnumerationGeneration += 1;
    this.cachedDevices = null;
    this.permissionGranted = false;
    this.enumerationPromise = null;
  }

  getLeasedStream(sourceKey: string, ownerId: string): MediaStream | null {
    const entry = this.streams.get(sourceKey);
    return entry?.owners.has(ownerId) && entry.stream.active ? entry.stream : null;
  }

  ownerLeaseCount(ownerId: string): number {
    let count = 0;
    this.streams.forEach((entry) => {
      if (entry.owners.has(ownerId)) count += 1;
    });
    return count;
  }

  diagnostics(): MediaSourceRegistryDiagnostics {
    let pendingAttempts = 0;
    this.pendingAttemptsByOwner.forEach((attempts) => {
      pendingAttempts += attempts.size;
    });
    return {
      streams: this.streams.size,
      pendingStarts: this.pendingStarts.size,
      pendingConsumers: this.pendingConsumers.size,
      pendingAttemptOwners: this.pendingAttemptsByOwner.size,
      pendingAttempts,
      latestAttemptToken: this.attemptSequence,
    };
  }

  releaseSource(sourceKey: string, ownerId: string): void {
    const entry = this.streams.get(sourceKey);
    if (entry === undefined || !entry.owners.delete(ownerId)) return;
    if (entry.owners.size === 0) this.stopEntry(sourceKey, entry);
  }

  releaseOwner(ownerId: string): void {
    this.invalidateOwnerAttempts(ownerId);
    for (const [sourceKey, entry] of Array.from(this.streams.entries())) {
      if (!entry.owners.delete(ownerId)) continue;
      if (entry.owners.size === 0) this.stopEntry(sourceKey, entry);
    }
  }

  destroy(): void {
    this.lifecycleGeneration += 1;
    this.streams.forEach((entry, sourceKey) => this.stopEntry(sourceKey, entry));
    this.streams.clear();
    this.pendingStarts.clear();
    this.pendingConsumers.clear();
    this.pendingAttemptsByOwner.forEach((attempts) => {
      attempts.forEach((attempt) => { attempt.valid = false; });
    });
    this.pendingAttemptsByOwner.clear();
    this.clearDeviceCache();
  }

  private async acquire(
    sourceKey: string,
    ownerId: string,
    start: () => Promise<MediaStream>,
    signal?: AbortSignal,
  ): Promise<MediaStreamLease> {
    if (!ownerId.trim()) throw new Error("Media stream ownerId is required");
    if (signal?.aborted) throw abortReason(signal);
    const attempt = this.createAttempt(ownerId, "camera");
    const lifecycleGeneration = this.lifecycleGeneration;
    const onAbort = () => this.invalidateAttempt(attempt);
    signal?.addEventListener("abort", onAbort, { once: true });
    try {
      let entry = this.streams.get(sourceKey);
      if (entry === undefined || !entry.stream.active) {
        if (entry !== undefined) this.stopEntry(sourceKey, entry);
        let pending = this.pendingStarts.get(sourceKey);
        if (pending === undefined) {
          // Invoke camera acquisition before the first await so concurrent owners share one start.
          pending = Promise.resolve(start());
          this.pendingStarts.set(sourceKey, pending);
          const clearPending = () => {
            if (this.pendingStarts.get(sourceKey) === pending) this.pendingStarts.delete(sourceKey);
          };
          void pending.then(clearPending, clearPending);
        }
        this.pendingConsumers.set(sourceKey, (this.pendingConsumers.get(sourceKey) ?? 0) + 1);
        try {
          const stream = await pending;
          if (lifecycleGeneration !== this.lifecycleGeneration) {
            this.stopStream(stream);
            throw new DOMException("Media registry generation changed", "AbortError");
          }
          entry = this.streams.get(sourceKey);
          if (entry === undefined) {
            entry = { stream, owners: new Set(), stopped: false };
            this.streams.set(sourceKey, entry);
          } else if (entry.stream !== stream) {
            this.stopStream(stream);
          }
          if (signal?.aborted || !attempt.valid) {
            if (entry.owners.size === 0 && this.pendingConsumers.get(sourceKey) === 1) {
              this.stopEntry(sourceKey, entry);
            }
            if (signal?.aborted) throw abortReason(signal);
            throw new DOMException("Media owner attempt invalidated", "AbortError");
          }
          entry.owners.add(ownerId);
        } finally {
          const remaining = (this.pendingConsumers.get(sourceKey) ?? 1) - 1;
          if (remaining === 0) this.pendingConsumers.delete(sourceKey);
          else this.pendingConsumers.set(sourceKey, remaining);
        }
      }
      if (signal?.aborted || !attempt.valid) {
        if (entry.owners.size === 0) this.stopEntry(sourceKey, entry);
        if (signal?.aborted) throw abortReason(signal);
        throw new DOMException("Media owner attempt invalidated", "AbortError");
      }
      entry.owners.add(ownerId);
      this.permissionGranted = true;
      return this.createLease(sourceKey, ownerId, entry);
    } finally {
      signal?.removeEventListener("abort", onAbort);
      this.unregisterAttempt(attempt);
    }
  }

  private createAttempt(ownerId: string, kind: AcquisitionAttempt["kind"]): AcquisitionAttempt {
    this.attemptSequence += 1;
    const attempt: AcquisitionAttempt = {
      token: this.attemptSequence,
      ownerId,
      kind,
      valid: true,
    };
    const ownerAttempts = this.pendingAttemptsByOwner.get(ownerId) ?? new Set<AcquisitionAttempt>();
    ownerAttempts.add(attempt);
    this.pendingAttemptsByOwner.set(ownerId, ownerAttempts);
    return attempt;
  }

  private invalidateAttempt(attempt: AcquisitionAttempt): void {
    attempt.valid = false;
    this.unregisterAttempt(attempt);
  }

  private unregisterAttempt(attempt: AcquisitionAttempt): void {
    const ownerAttempts = this.pendingAttemptsByOwner.get(attempt.ownerId);
    if (ownerAttempts === undefined) return;
    ownerAttempts.delete(attempt);
    if (ownerAttempts.size === 0) this.pendingAttemptsByOwner.delete(attempt.ownerId);
  }

  private invalidateOwnerAttempts(ownerId: string, kind?: AcquisitionAttempt["kind"]): void {
    const ownerAttempts = this.pendingAttemptsByOwner.get(ownerId);
    if (ownerAttempts === undefined) return;
    for (const attempt of Array.from(ownerAttempts)) {
      if (kind !== undefined && attempt.kind !== kind) continue;
      attempt.valid = false;
      ownerAttempts.delete(attempt);
    }
    if (ownerAttempts.size === 0) this.pendingAttemptsByOwner.delete(ownerId);
  }

  private createLease(
    sourceKey: string,
    ownerId: string,
    entry: StreamEntry,
    publicSourceKey = sourceKey,
  ): MediaStreamLease {
    let released = false;
    return {
      ownerId,
      sourceKey: publicSourceKey,
      stream: entry.stream,
      release: () => {
        if (released) return;
        released = true;
        this.releaseSource(sourceKey, ownerId);
      },
    };
  }

  private stopEntry(sourceKey: string, entry: StreamEntry): void {
    if (entry.stopped) return;
    entry.stopped = true;
    this.stopStream(entry.stream);
    if (this.streams.get(sourceKey) === entry) this.streams.delete(sourceKey);
  }

  private stopStream(stream: MediaStream): void {
    if (this.stoppedStreams.has(stream)) return;
    this.stoppedStreams.add(stream);
    stream.getTracks().forEach((track) => track.stop());
  }

  private getUserMedia(constraints: MediaStreamConstraints): Promise<MediaStream> {
    if (this.getUserMediaDependency !== undefined) return this.getUserMediaDependency(constraints);
    return navigator.mediaDevices.getUserMedia(constraints);
  }

  private enumerateDevices(): Promise<MediaDeviceInfo[]> {
    if (this.enumerateDevicesDependency !== undefined) return this.enumerateDevicesDependency();
    return navigator.mediaDevices.enumerateDevices();
  }
}

export const browserMediaSourceRegistry = new MediaSourceRegistry();

if (typeof window !== "undefined") {
  window.addEventListener("beforeunload", () => browserMediaSourceRegistry.destroy());
}
