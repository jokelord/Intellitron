import assert from "node:assert/strict";
import test from "node:test";

import type { Message } from "../message-types.ts";
import {
  formatHistoricalImagePlaceholder,
  stageChatHistoryImageOmission,
} from "./history-omission.ts";

function message(
  id: string,
  role: Message["role"],
  content: Message["content"],
  images?: string[],
): Message {
  return { id, role, content, images, timestamp: new Date(0) };
}

test("formatHistoricalImagePlaceholder emits the exact stable grammar", () => {
  assert.equal(
    formatHistoricalImagePlaceholder({
      count: 48,
      sources: ["camera_2", "manual", "desktop", "camera_1", "manual"],
      relativeRange: { startSeconds: 0, endSeconds: 15.214 },
    }),
    "[historical images omitted: count=48; sources=manual,desktop,camera_1,camera_2; relative_range=0.00..15.21s]",
  );
  assert.equal(
    formatHistoricalImagePlaceholder({ count: 1, sources: [] }),
    "[historical images omitted: count=1; sources=none; relative_range=unavailable]",
  );
});

test("staged Chat omission replaces each continuous image run at its original position", () => {
  const textPart = { type: "text", text: "exact  text\nbytes" };
  const opaquePart = { type: "input_audio", text: "leave-me-byte-for-byte" };
  const history = [
    message("system", "system", "stable system"),
    message("user", "user", [
      { type: "image_url", image_url: { url: "data:image/jpeg;base64,one" } },
      { type: "image_url", image_url: { url: "data:image/jpeg;base64,two" } },
      textPart,
      opaquePart,
      { type: "image_url", image_url: { url: "data:image/jpeg;base64,three" } },
    ], ["one", "two", "three"]),
    message("assistant", "assistant", "unchanged assistant"),
  ];
  const before = JSON.stringify(history);

  const staged = stageChatHistoryImageOmission(history, ({ blockIndex }) => (
    blockIndex === 0
      ? {
          sources: ["camera_2", "manual", "desktop"],
          relativeRange: { startSeconds: 1.005, endSeconds: 4.126 },
        }
      : { sources: [] }
  ));

  assert.equal(JSON.stringify(history), before, "staging must not mutate canonical UI history");
  assert.equal(staged.omittedBlockCount, 2);
  assert.equal(staged.stagedMessages[0], history[0]);
  assert.equal(staged.stagedMessages[2], history[2]);
  const stagedParts = staged.stagedMessages[1].content as Array<Record<string, unknown>>;
  assert.deepEqual(stagedParts, [
    {
      type: "text",
      text: "[historical images omitted: count=2; sources=manual,desktop,camera_2; relative_range=1.00..4.13s]",
    },
    textPart,
    opaquePart,
    {
      type: "text",
      text: "[historical images omitted: count=1; sources=none; relative_range=unavailable]",
    },
  ]);
  assert.equal(stagedParts[1], textPart, "non-image content parts must retain identity and bytes");
  assert.equal(stagedParts[2], opaquePart, "unknown non-image content must be untouched");
  assert.deepEqual(history[1].images, ["one", "two", "three"]);
  assert.equal(history[1].historicalImageSummaries, undefined);
  const committed = staged.commit();
  assert.deepEqual(
    committed[1].historicalImageSummaries?.map((summary) => summary.contentPartIndex),
    [0, 3],
  );
  assert.equal(
    committed[1].historicalImageSummaries?.every((summary) => Object.isFrozen(summary)),
    true,
  );
});

test("commit projects UI summaries and clears images only after explicit acceptance boundary", () => {
  const history = [message("user", "user", [
    { type: "image_url", image_url: { url: "data:image/jpeg;base64,one" } },
    { type: "text", text: "question" },
  ], ["data:image/jpeg;base64,one"]), message("assistant", "assistant", "answer")];
  const staged = stageChatHistoryImageOmission(history, () => ({ sources: ["manual"] }));

  assert.equal(history[0].historicalImageSummaries, undefined);
  assert.equal(history[0].images?.length, 1);

  const committed = staged.commit();
  assert.equal(committed[0].images, undefined);
  assert.deepEqual(committed[0].historicalImageSummaries, [{
    count: 1,
    sources: ["manual"],
    relativeRange: undefined,
    placeholder: "[historical images omitted: count=1; sources=manual; relative_range=unavailable]",
    contentPartIndex: 0,
  }]);
  assert.deepEqual(committed[0].content, staged.stagedMessages[0].content);
  assert.equal(history[0].images?.length, 1, "commit returns a projection without mutating input");
});

test("discarding a pre-accept stage leaves canonical history and image references unchanged", () => {
  const history = [message("user", "user", [
    { type: "image_url", image_url: { url: "data:image/jpeg;base64,one" } },
  ], ["data:image/jpeg;base64,one"])];
  const staged = stageChatHistoryImageOmission(history);

  staged.discard();

  assert.deepEqual(history[0].images, ["data:image/jpeg;base64,one"]);
  assert.equal(history[0].historicalImageSummaries, undefined);
  assert.throws(() => staged.commit(), /discarded/);
});

test("commit is exactly once and rejects a stale target message version", () => {
  const history = [message("user", "user", [
    { type: "image_url", image_url: { url: "data:image/jpeg;base64,one" } },
    { type: "text", text: "question" },
  ], ["data:image/jpeg;base64,one"])];
  const staged = stageChatHistoryImageOmission(history);

  const changed = [{
    ...history[0],
    content: [
      { type: "image_url", image_url: { url: "data:image/jpeg;base64,two" } },
      { type: "text", text: "new question" },
    ],
  }] as Message[];
  assert.throws(() => staged.commit(changed), /stale/i);

  const committed = staged.commit(history);
  assert.match(JSON.stringify(committed[0].content), /historical images omitted/);
  assert.throws(() => staged.commit(history), /already committed/);
});

test("commit rejects duplicate current message IDs instead of applying one plan twice", () => {
  const source = message("same", "user", [
    { type: "image_url", image_url: { url: "data:image/jpeg;base64,one" } },
  ]);
  const staged = stageChatHistoryImageOmission([source]);

  assert.throws(() => staged.commit([source, { ...source }]), /duplicate/i);
});

test("staging versions image history without serializing or copying Base64 strings", () => {
  const hugeImage = `data:image/jpeg;base64,${"x".repeat(2_000_000)}`;
  const history = [message("history", "user", [
    { type: "image_url", image_url: { url: hugeImage } },
    { type: "text", text: "question" },
  ], [hugeImage])];
  const originalStringify = JSON.stringify;
  JSON.stringify = () => { throw new Error("history serialization is forbidden"); };
  try {
    const staged = stageChatHistoryImageOmission(history);
    assert.equal(staged.omittedBlockCount, 1);
    staged.discard();
  } finally {
    JSON.stringify = originalStringify;
  }
});

test("reference versions reject in-place image content and image-list mutation", () => {
  const content = [
    { type: "image_url", image_url: { url: "data:image/jpeg;base64,one" } },
    { type: "text", text: "question" },
  ];
  const images = ["data:image/jpeg;base64,one"];
  const history = [message("history", "user", content, images)];
  const staged = stageChatHistoryImageOmission(history);

  content[0].image_url!.url = "data:image/jpeg;base64,two";
  images[0] = "data:image/jpeg;base64,two";

  assert.throws(() => staged.commit(history), /stale/i);
});

test("resolver relativeRange is cloned and frozen for matching wire and UI summaries", () => {
  const relativeRange = { startSeconds: 1.25, endSeconds: 4.5 };
  const history = [message("history", "user", [
    { type: "image_url", image_url: { url: "data:image/jpeg;base64,one" } },
  ])];
  const staged = stageChatHistoryImageOmission(history, () => ({
    sources: ["camera_1"],
    relativeRange,
  }));

  relativeRange.startSeconds = 99;
  relativeRange.endSeconds = 100;
  const committed = staged.commit(history);

  assert.match(
    JSON.stringify(staged.stagedMessages[0].content),
    /relative_range=1\.25\.\.4\.50s/,
  );
  assert.deepEqual(committed[0].historicalImageSummaries?.[0].relativeRange, {
    startSeconds: 1.25,
    endSeconds: 4.5,
  });
  assert.equal(Object.isFrozen(committed[0].historicalImageSummaries?.[0].relativeRange), true);
});

test("Abort cleanup preserves text-only and ASR strings while omitting only image containers", () => {
  const textOne = "first user text  byte-for-byte\n";
  const asrText = "ASR transcript  with spacing\n第二行";
  const imageTextPart = { type: "text", text: "image question exact text" };
  const history: Message[] = [
    message("text-1", "user", textOne),
    message("assistant-1", "assistant", "answer one"),
    {
      ...message("asr-1", "user", asrText, ["data:image/jpeg;base64,duplicate-ui-reference"]),
      voiceReplay: { utteranceId: "utterance-1", mimeType: "audio/wav", url: "blob:asr-1" },
    },
    message("assistant-2", "assistant", "answer two"),
    message("image-1", "user", [
      { type: "image_url", image_url: { url: "data:image/jpeg;base64,image-one" } },
      imageTextPart,
    ], ["data:image/jpeg;base64,image-one"]),
  ];

  const cleanup = stageChatHistoryImageOmission(history, ({ message: source }) => ({
    sources: source.id === "asr-1" ? ["manual"] : ["camera_1"],
  }));
  const committed = cleanup.commit(history);

  assert.equal(committed[0].content, textOne);
  assert.equal(committed[2].content, asrText);
  assert.equal(committed[2].images, undefined);
  assert.equal(committed[2].voiceReplay, history[2].voiceReplay);
  assert.equal(committed[2].historicalImageSummaries?.[0].count, 1);
  const imageParts = committed[4].content as Array<{ type: string; text?: string }>;
  assert.equal(imageParts[1], imageTextPart);
  assert.equal(imageParts[1].text, "image question exact text");

  const afterNormalSend = [
    ...committed,
    message("normal-after-cleanup", "user", "normal text after Abort cleanup"),
  ];
  const next = stageChatHistoryImageOmission(afterNormalSend);
  assert.equal(next.omittedBlockCount, 0);
  assert.equal(next.stagedMessages[0].content, textOne);
  assert.equal(next.stagedMessages[2].content, asrText);
  assert.equal(next.stagedMessages[5].content, "normal text after Abort cleanup");
});
