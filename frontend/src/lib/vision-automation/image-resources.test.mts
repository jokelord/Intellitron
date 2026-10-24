import assert from "node:assert/strict";
import { test } from "node:test";

import { OwnedImageResource, disposeBundle } from "./image-resources.ts";
import type { ImageResource, TickBundle } from "./types.ts";

test("owned resources track acquired URLs and revoke each exactly once", () => {
  const created: Blob[] = [];
  const revoked: string[] = [];
  const resource = new OwnedImageResource(
    new Blob(["upload"], { type: "image/jpeg" }),
    new Blob(["thumb"], { type: "image/jpeg" }),
    {
      createObjectURL(blob) {
        created.push(blob);
        return `blob:${created.length}`;
      },
      revokeObjectURL(url) {
        revoked.push(url);
      },
    },
  );

  assert.equal(resource.acquireObjectURL("upload"), "blob:1");
  assert.equal(resource.acquireObjectURL("thumbnail"), "blob:2");
  resource.dispose();
  resource.dispose();

  assert.deepEqual(revoked, ["blob:1", "blob:2"]);
  assert.equal(resource.uploadBlob, null);
  assert.equal(resource.thumbnailBlob, null);
  assert.equal(resource.disposed, true);
  assert.throws(() => resource.acquireObjectURL("upload"), /disposed/);
});

test("disposeBundle is idempotent even when a resource appears twice", () => {
  let disposeCount = 0;
  const resource = {
    uploadBlob: new Blob(["x"]),
    thumbnailBlob: null,
    disposed: false,
    acquireObjectURL() {
      throw new Error("unused");
    },
    releaseObjectURL() {
      return false;
    },
    dispose() {
      disposeCount += 1;
      this.disposed = true;
      this.uploadBlob = null as never;
    },
  };
  const bundle: TickBundle = {
    tickIndex: 1,
    timestampMs: 100,
    relativeTimeSeconds: 0,
    frames: [
      { source: { sourceId: "a", kind: "camera", stableAlias: "camera_1", label: "A" }, resource },
      { source: { sourceId: "b", kind: "camera", stableAlias: "camera_2", label: "B" }, resource },
    ],
  };

  disposeBundle(bundle);
  disposeBundle(bundle);
  assert.equal(disposeCount, 1);
});

test("the public resource contract acquires and individually releases owned URLs safely", () => {
  const revoked: string[] = [];
  const resource: ImageResource = new OwnedImageResource(
    new Blob(["upload"], { type: "image/jpeg" }),
    new Blob(["thumbnail"], { type: "image/jpeg" }),
    {
      createObjectURL(blob) {
        return blob.size === 6 ? "blob:upload" : "blob:thumbnail";
      },
      revokeObjectURL(url) {
        revoked.push(url);
      },
    },
  );

  const uploadUrl = resource.acquireObjectURL("upload");
  const thumbnailUrl = resource.acquireObjectURL("thumbnail");
  assert.equal(resource.releaseObjectURL(uploadUrl), true);
  assert.equal(resource.releaseObjectURL(uploadUrl), false);
  assert.equal(resource.releaseObjectURL("blob:not-owned"), false);
  resource.dispose();

  assert.deepEqual(revoked, ["blob:upload", "blob:thumbnail"]);
  assert.throws(() => resource.acquireObjectURL("upload"), /disposed/);
});
