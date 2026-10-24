import assert from 'node:assert/strict';
import test from 'node:test';
import * as agentQueue from './agent-queue.ts';

test('Text and Vision imperative sends queue when busy changes before React rerenders', () => {
  let busy = false;
  const textQueue: string[] = [];
  const visionQueue: agentQueue.QueuedVisionMessage[] = [];
  let textPosts = 0;
  let visionPosts = 0;
  const textSend = (text: string) => {
    if (agentQueue.deferImperativeAgentSend(() => busy, () => textQueue.push(text))) return;
    textPosts += 1;
  };
  const visionSend = (text: string, images: string[]) => {
    if (agentQueue.deferImperativeAgentSend(
      () => busy,
      () => visionQueue.splice(0, visionQueue.length, ...agentQueue.enqueueVisionMessage(visionQueue, text, images)),
    )) return;
    visionPosts += 1;
  };

  busy = true;
  textSend('text queued before rerender');
  visionSend('vision queued before rerender', ['data:image/png;base64,one']);

  assert.deepEqual(textQueue, ['text queued before rerender']);
  assert.deepEqual(visionQueue, [{ text: 'vision queued before rerender', images: ['data:image/png;base64,one'] }]);
  assert.equal(textPosts, 0);
  assert.equal(visionPosts, 0);
});
