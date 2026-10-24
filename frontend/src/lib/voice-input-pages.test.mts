import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const root = new URL("../../", import.meta.url);

async function page(relative: string): Promise<string> {
  return readFile(new URL(relative, root), "utf8");
}

test("Text and Vision pages use the same voice controller and Layout A components", async () => {
  for (const relative of ["src/app/page.tsx", "src/app/vision/page.tsx"]) {
    const source = await page(relative);
    assert.match(source, /useVoiceInput/);
    assert.match(source, /VoiceModeToggle/);
    assert.match(source, /VoiceInputPanel/);
    assert.match(source, /explicitText\?: string/);
    assert.match(source, /voiceController\.isVoiceMode/);
    assert.match(source, /voiceController\.state\.pendingTranscript/);
  }
});

test("both pages await voice cleanup before mode transition or reactivation", async () => {
  const shared = await page("src/lib/conversation/use-conversation-voice.ts");
  assert.match(shared, /await voiceController\.activateVoice\(\)/);
  assert.match(shared, /await voiceController\.deactivateVoice\(\)/);
  assert.match(shared, /voiceModeTransitionRef\.current/);
  for (const relative of ["src/app/page.tsx", "src/app/vision/page.tsx"]) assert.match(await page(relative), /useConversationVoice\(\{\s*state:\s*conversationState/);
  const controller = await page("src/lib/voice-input.ts");
  assert.match(controller, /activationSetupBarrierRef/);
  assert.match(controller, /waitForVoiceReleaseBarrier/);
  assert.match(controller, /previousActivationSetup\?\.abortTimeoutSeconds/);
  const deactivate = controller.match(/const deactivateVoice = useCallback\(async \(\) => \{([\s\S]*?)\n\s*\}, \[releaseCurrent\]\);/)?.[1] ?? '';
  assert.ok(deactivate.indexOf('await cleanup') < deactivate.indexOf('dispatch({'));
});

test("both pages update the final-transcript send ref in a layout effect", async () => {
  for (const relative of ["src/app/page.tsx", "src/app/vision/page.tsx"]) {
    const source = await page(relative);
    assert.match(
      source,
      /useLayoutEffect\(\(\) => \{\s*handleSendRef\.current = handleSend;[\s\S]*?\}, \[handleSend\]\);/,
    );
    assert.doesNotMatch(
      source,
      /useEffect\(\(\) => \{\s*handleSendRef\.current = handleSend;\s*\}, \[handleSend\]\)/,
    );
    assert.doesNotMatch(
      source,
      /\n\s*handleSendRef\.current = handleSend;\s*\n\s*const voiceController = useVoiceInput/,
    );
  }
});

test("both voice send buttons require a final pending transcript", async () => {
  const source = await page("src/lib/conversation/use-conversation-commands.ts");
  const branch = source.match(/if \(voiceController\.isVoiceMode && inputMode === 'voice'\) \{([\s\S]*?)\n\s*\}/)?.[1] ?? '';
  assert.match(branch, /pendingTranscript\.trim\(\)/); assert.match(branch, /if \(!transcript\) return/);
  assert.doesNotMatch(branch, /partialTranscript/);
  for (const relative of ["src/app/page.tsx", "src/app/vision/page.tsx"]) {
    const route = await page(relative);
    assert.match(route, /sendDisabled=\{\(voiceController\.isVoiceMode && inputMode === 'voice'\s*\? !voiceController\.state\.pendingTranscript\.trim\(\)/);
  }
});

test("Voice mode replaces the textarea rather than rendering both", async () => {
  const source = await page("src/components/conversation/ConversationComposer.tsx");
  assert.match(source, /inputMode === 'voice' \?/);
  assert.match(source, /<VoiceInputPanel/);
  assert.match(source, /:\s*\(\s*<div className="min-w-0 flex-1 space-y-2">[\s\S]*?<textarea/);
  for (const relative of ["src/app/page.tsx", "src/app/vision/page.tsx"]) assert.match(await page(relative), /<ConversationComposer/);
});

test("dictation renders the listening indicator above the editable textarea", async () => {
  for (const relative of ["src/components/conversation/ConversationComposer.tsx"]) {
    const source = await page(relative);
    assert.match(source, /<VoiceInputPanel/);
    assert.match(
      source,
      /inputMode === 'voice' \?\s*\(\s*<div className="min-w-0 flex-1">\s*<VoiceInputPanel/,
    );
    assert.match(
      source,
      /inputMode === 'dictation' && voiceController\.isVoiceMode \?\s*\(\s*<VoiceListeningIndicator/,
    );
  }
  const panel = await page("src/components/VoiceInputPanel.tsx");
  assert.doesNotMatch(panel, /Clipping/);
  assert.doesNotMatch(panel, /h-16 w-full/);
  assert.doesNotMatch(panel, /viewBox/);
  assert.match(panel, /<VoiceListeningIndicator state=\{state\} inputGain=\{inputGain\} compact \/>/);
  assert.match(panel, /px-3 py-\[4px\] shadow-sm/);

  const indicator = await page("src/components/VoiceListeningIndicator.tsx");
  assert.match(indicator, /export function VoiceListeningIndicator\(/);
  assert.match(indicator, /56 - Math\.max\(0, Math\.min\(1, sample\)\) \* 50/);
  assert.doesNotMatch(indicator, /32 - Math\.max\(0/);
  assert.match(indicator, /y1="56" x2="256" y2="56"/);
  assert.match(indicator, /viewBox="0 0 256 64"/);
  assert.match(indicator, /compact = false/);
  assert.match(
    indicator,
    /compact\s*\?\s*"min-w-\[130px\] flex-1 overflow-hidden rounded-xl border border-gray-200 bg-gray-950 px-1\.5 py-0\.5 dark:border-gray-700"\s*:\s*"min-w-\[130px\] flex-1 overflow-hidden rounded-xl border border-gray-200 bg-gray-950 px-2 py-1 dark:border-gray-700"/,
  );
  assert.match(indicator, /compact \? "h-8 w-full" : "h-10 w-full"/);
  assert.match(indicator, /type="range"/);
  assert.match(indicator, /min=\{inputGain\.minDb\}/);
  assert.match(indicator, /max=\{inputGain\.maxDb\}/);
  assert.match(indicator, /step=\{inputGain\.stepDb\}/);
  assert.match(indicator, /value=\{inputGain\.valueDb\}/);
  assert.match(indicator, /onChange=\{\(event\) => inputGain\.setValueDb\(event\.currentTarget\.valueAsNumber\)\}/);
  assert.match(indicator, /aria-label="Input gain"/);
  assert.match(indicator, /aria-valuemin/);
  assert.match(indicator, /aria-valuemax/);
  assert.match(indicator, /aria-valuenow/);
  assert.doesNotMatch(indicator, /disabled=.*recording|recording.*disabled/s);
});

test("Text and Vision pass controller-owned input gain to voice and dictation paths", async () => {
  const shared = await page("src/components/conversation/ConversationComposer.tsx");
  assert.match(shared, /<VoiceInputPanel\s+state=\{voiceController\.state\}\s+inputGain=\{voiceController\.inputGain\}/);
  assert.match(shared, /<VoiceListeningIndicator\s+state=\{voiceController\.state\}\s+inputGain=\{voiceController\.inputGain\}/);
  assert.doesNotMatch(shared, /voiceConfig\?\.inputGainDb/);
  for (const relative of ["src/app/page.tsx", "src/app/vision/page.tsx"]) assert.match(await page(relative), /voiceController=\{voiceController\}/);
});

test("Vision explicit transcript send retains the current image list", async () => {
  const source = await page("src/app/vision/page.tsx");
  const voice = await page("src/lib/conversation/use-conversation-voice.ts");
  assert.match(voice, /sendRef\.current\(transcript, replay\)/);
  assert.match(source, /useConversationVoice\(\{\s*state:\s*conversationState,\s*send:\s*sendConversationInput/);
  assert.match(source, /const currentImages = automationInput \? \[\.\.\.automationInput\.payload\.images\] : \[\.\.\.turnImages\]/);
  assert.match(source, /const \{ bubbleImages, consumedManualImages \} = projectVisionTurnImages/);
  assert.match(source, /createConversationTurnMessages\(\{[^\n]*images:\s*bubbleImages/);
  assert.match(source, /manualImages: consumedManualImages/);
});

test("API config response exposes the browser-safe ASR contract", async () => {
  const source = await page("src/lib/api.ts");
  assert.match(source, /asr\?:\s*\{/);
  assert.match(source, /input_gain_db:\s*number/);
  assert.match(source, /input_gain_min_db:\s*number/);
  assert.match(source, /input_gain_max_db:\s*number/);
  assert.match(source, /input_gain_step_db:\s*number/);
  assert.match(source, /auto_send:\s*boolean/);
  assert.match(source, /websocket_path:\s*string/);
  assert.match(source, /abort_timeout_seconds:\s*number/);
  assert.match(source, /max_buffered_frames:\s*number/);
  assert.match(source, /dictation_magic_send:\s*\{/);
  assert.match(source, /commands:\s*string\[\]/);
  assert.match(source, /poll_interval_ms:\s*number/);
  assert.doesNotMatch(source, /asr\?:\s*\{[^}]*model_path/s);
});

test("both pages share dictation magic-send polling and settings wiring", async () => {
  for (const relative of ["src/app/page.tsx", "src/app/vision/page.tsx"]) {
    const source = await page(relative);
    assert.match(source, /useConversationVoice\(\{/);

    assert.match(source, /onOpenDictationSettings=/);
    assert.match(source, /<DictationMagicSendModal/);
    assert.match(source, /writeDictationMagicSendSettings\(window\.localStorage, next\)/);


  }
  const shared = await page("src/lib/conversation/use-conversation-voice.ts");
  assert.match(shared, /useDictationMagicSend\(\{/);
  assert.match(shared, /mode: inputMode/);
  assert.match(shared, /send: async \(text, sourceInput\)/);
  assert.match(shared, /busy: [^\n]*showDictationMagicSendSettings/);
  const modal = await page("src/components/DictationMagicSendModal.tsx");
  assert.match(modal, /role="dialog"/);
  assert.match(modal, /aria-modal="true"/);
  assert.match(modal, /max-h-64 overflow-y-auto/);
  assert.match(modal, /disabled=\{!rule\.text\.trim\(\)\}/);
  assert.match(modal, /filter\(\(rule\) => rule\.text\.trim\(\)\)/);
  const indicator = await page("src/components/VoiceListeningIndicator.tsx");
  assert.match(indicator, /Dictation magic send settings/);
  const panel = await page("src/components/VoiceInputPanel.tsx");
  assert.match(panel, /<VoiceListeningIndicator state=\{state\} inputGain=\{inputGain\} compact \/>/);
  const vision = await page("src/app/vision/page.tsx");
  assert.match(vision, /magicSendSourceInput/);
  assert.match(vision, /clearDictationMagicSendSourceIfUnchanged\(current, pageMutation\.magicSendSourceInput\)/);
  assert.match(
    vision,
    /clearAcceptedDictationMagicSendSource\(\s*accepted,\s*current,\s*magicSendSourceInput,?\s*\)/,
  );
});

test("both pages attach page-local voice replay only to user bubble rendering", async () => {
  const messageTypes = await page("src/lib/message-types.ts");
  assert.match(messageTypes, /voiceReplay\?:\s*VoiceReplayAudio/);
  assert.doesNotMatch(messageTypes.slice(messageTypes.indexOf("buildChatWireMessages")), /voiceReplay/);

  const player = await page("src/components/VoiceReplayPlayer.tsx");
  assert.match(player, /<audio/);
  assert.match(player, /controls/);
  assert.match(player, /preload="metadata"/);

  for (const relative of ["src/app/page.tsx", "src/app/vision/page.tsx"]) {
    const source = await page(relative);
    assert.match(source, /useConversationVoice\(/);
    assert.match(source, /(?:replay:\s*effectiveReplay|voiceReplay:\s*effectiveReplay)/);
    const list = await page("src/components/conversation/ConversationMessageList.tsx");
    assert.match(list, /message\.role === ['"]user['"][\s\S]*?<VoiceReplayPlayer/);
  }
});

test("both pages preserve replay when a final transcript switches to keyboard", async () => {
  const shared = await page("src/lib/conversation/use-conversation-voice.ts");
  assert.match(shared, /const replay = voiceController\.state\.pendingReplay/);
  assert.match(shared, /await voiceController\.deactivateVoice\(\)/);
  assert.match(shared, /pendingKeyboardVoiceReplayRef\.current = replay\s*\? \{transcript: pendingTranscript\.trim\(\), replay\}/);
  for (const relative of ["src/app/page.tsx", "src/app/vision/page.tsx"]) {
    const route = await page(relative); assert.match(route, /pending\.transcript === trimmedInput/);
    assert.match(route, /(?:replay:\s*effectiveReplay|voiceReplay:\s*effectiveReplay)/);
  }
});

test("both pages release replay URLs discarded before or after bubble attachment", async () => {
  const actions = await page("src/lib/conversation/conversation-actions.ts");
  assert.match(actions, /message\.voiceReplay[\s\S]*?releaseVoiceReplayRef\.current/);
  for (const relative of ["src/app/page.tsx", "src/app/vision/page.tsx"]) {
    const source = await page(relative);
    assert.match(source, /releaseVoiceReplayRef/);
    assert.match(source, /(?:effectiveReplay|pageMutation\.replay) !== pending\.replay/);
    assert.match(source, /clearConversation\(/);
  }
});

test("both pages let an active microphone mode be re-selected to restart capture", async () => {
  const shared = await page("src/lib/conversation/use-conversation-voice.ts");
  assert.match(shared, /mode === inputMode && mode === 'keyboard'/);
  assert.doesNotMatch(shared, /mode === inputMode && !\(mode !== ['"]keyboard['"] && !voiceController\.isVoiceMode\)\)/);
  assert.match(shared, /if \(mode === 'voice'\)[\s\S]*await voiceController\.activateVoice\(\)/);
});

test("both pages pause active dictation before manual and magic-send model requests", async () => {
  const commands = await page("src/lib/conversation/use-conversation-commands.ts");
  const dictation = commands.indexOf("if (voiceController.isVoiceMode && inputMode === 'dictation')");
  assert.ok(dictation >= 0);
  assert.ok(commands.indexOf('voiceController.pauseForResponse();', dictation) < commands.indexOf('send();', dictation));
  const voice = await page("src/lib/conversation/use-conversation-voice.ts");
  const magic = voice.slice(voice.indexOf('send: async (text, sourceInput)'));
  assert.ok(magic.indexOf('voiceController.pauseForResponse()') < magic.indexOf('sendRef.current(text, undefined, sourceInput)'));
});

test("both pages defer voice resume while a dictation magic-send command is pending", async () => {
  const shared = await page("src/lib/conversation/use-conversation-voice.ts");
  assert.match(shared, /findDictationMagicSendMatch\(input, dictationMagicSendSettings\?\.rules \?\? \[\]\)/);
  assert.match(shared, /resumeBlocked: dictationMagicSendPending \|\| pendingAcceptance \|\| cycleRunning/);
  assert.match(shared, /!busy && !pendingAcceptance && !cycleRunning && !dictationMagicSendPending/);
});

test("VoiceModeToggle exposes keyboard/dictation/voice segments", async () => {
  const source = await page("src/components/VoiceModeToggle.tsx");
  assert.match(source, /export type VoiceInputMode = "keyboard" \| "dictation" \| "voice";/);
  assert.equal((source.match(/aria-pressed=/g) ?? []).length, 3);
  assert.match(source, /aria-label="听写输入"/);
  assert.match(source, /onModeChange\("dictation"\)/);
});

test("both pages pass the hook config through literally with no derived override", async () => {
  const shared = await page("src/lib/conversation/use-conversation-voice.ts");
  const state = await page("src/lib/conversation/use-conversation-state.ts");
  assert.match(state, /useState<VoiceInputMode>\('keyboard'\)/);
  assert.match(shared, /config:\s*voiceConfig,/);
  assert.doesNotMatch(shared, /effectiveVoiceConfig|validVoiceConfig|autoSend:\s*inputMode/);
});

test("visibility subscription reports changes once without owning voice teardown", async () => {
  const visibilityModule = await import("./page-visibility.ts").catch(() => ({}));
  const subscribe = (visibilityModule as {
    subscribePageVisibility?: (
      pageDocument: {
        visibilityState: "visible" | "hidden";
        addEventListener(type: "visibilitychange", listener: () => void): void;
        removeEventListener(type: "visibilitychange", listener: () => void): void;
      },
      listener: (visible: boolean) => void,
    ) => () => void;
  }).subscribePageVisibility;
  assert.equal(typeof subscribe, "function");
  const listeners = new Set<() => void>();
  const pageDocument = {
    visibilityState: "hidden" as "visible" | "hidden",
    addEventListener: (_type: "visibilitychange", listener: () => void) => { listeners.add(listener); },
    removeEventListener: (_type: "visibilitychange", listener: () => void) => { listeners.delete(listener); },
  };
  const observed: boolean[] = [];
  const unsubscribe = subscribe!(pageDocument, (visible) => { observed.push(visible); });

  listeners.forEach((listener) => listener());
  pageDocument.visibilityState = "visible";
  listeners.forEach((listener) => listener());
  listeners.forEach((listener) => listener());
  pageDocument.visibilityState = "hidden";
  listeners.forEach((listener) => listener());
  unsubscribe();
  pageDocument.visibilityState = "visible";
  listeners.forEach((listener) => listener());

  assert.deepEqual(observed, [false, true, false]);
  assert.equal(listeners.size, 0);
});

test("both pages resume full-auto voice in the background unless requests or magic-send are pending", async () => {
  const shared = await page("src/lib/conversation/use-conversation-voice.ts");
  assert.match(shared, /!busy && !pendingAcceptance && !cycleRunning && !dictationMagicSendPending/);
  assert.match(shared, /voiceController\.state\.phase === 'paused_for_response'/);
  assert.doesNotMatch(shared, /if \(pageVisible &&|resumeBlocked:[^\n]*!pageVisible/);
  assert.match(shared, /releaseVoiceReplayRef, pageVisible, busy, pendingAcceptance/);
  assert.doesNotMatch(shared, /inputMode !== 'voice'[\s\S]*?paused_for_response/);
});

test("both pages route dictation transcripts to cursor insertion instead of send", async () => {
  const shared = await page("src/lib/conversation/use-conversation-voice.ts");
  const branch = shared.match(/if \(inputModeRef\.current === 'dictation'\) \{([\s\S]*?)return;/)?.[1] ?? '';
  assert.match(branch, /insertDictationTextAtCursor\(transcript\)/);
  assert.match(branch, /releaseVoiceReplayRef\.current\(replay\)/);
  assert.doesNotMatch(branch, /sendRef\.current/);
  assert.match(shared, /sendRef\.current\(transcript, replay\)/);
  assert.match(shared, /base\.slice\(0, start\) \+ textToInsert \+ base\.slice\(end\)/);
  assert.match(shared, /element\.selectionStart/); assert.match(shared, /element\.selectionEnd/);
  assert.match(shared, /setSelectionRange\(caret, caret\)/);
  assert.match(shared, /if \(pendingTranscript\.trim\(\)\) insertDictationTextAtCursor\(pendingTranscript\)/);
  assert.match(shared, /transcriptHandledRef\.current/);
});

test("voice capture persists across tab visibility changes", async () => {
  const controller = await page("src/lib/voice-input.ts");
  assert.doesNotMatch(
    controller,
    /visibilitychange/,
    "controller must not tear down voice when the tab is hidden",
  );
  const visibility = await page("src/lib/page-visibility.ts");
  assert.doesNotMatch(visibility, /abortVoice|deactivateVoice|MediaStream|\.stop\(/);
});

test("both pages keep the Voice panel visible after controller startup failure", async () => {
  const shared = await page("src/components/conversation/ConversationComposer.tsx");
  assert.match(shared, /inputMode === 'voice' \?\s*\(\s*<div className="min-w-0 flex-1">\s*<VoiceInputPanel/);
  assert.doesNotMatch(shared, /voiceController\.isVoiceMode && inputMode === 'voice' \?/);
  assert.match(shared, /onRetry=\{\(\) => void voiceController\.activateVoice\(\)\}/);
});

test("both pages use explicit Session Hub recovery candidates without retained-session probes", async () => {
  const { selectSessionHubRecoveryCandidate, writeSessionHubBookmark } = await import('./session-hub-recovery.ts');
  const values = new Map<string, string>();
  const storage = {getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => {values.set(key, value);}, removeItem: (key: string) => {values.delete(key);}};
  for (const pageMode of ['text', 'vision'] as const) {
    assert.equal(selectSessionHubRecoveryCandidate(pageMode, storage), null);
    writeSessionHubBookmark({pageMode, interactionMode: 'chat', sessionId: `${pageMode}-chat`, createdAt: 1}, storage);
    assert.equal(selectSessionHubRecoveryCandidate(pageMode, storage)?.bookmark.sessionId, `${pageMode}-chat`);
  }
  const recovery = await page('src/lib/conversation/use-conversation-recovery.ts');
  assert.match(recovery, /selectSessionHubRecoveryCandidate\(current\.pageMode\)/);
  assert.doesNotMatch(recovery, /listRetainedAgentSessions|readChatTranscript/);
});

test("legacy transcripts migrate only recoverable identity after explicit canonical Hub recovery", async () => {
  const { selectSessionHubRecoveryCandidate } = await import('./session-hub-recovery.ts');
  for (const pageMode of ['text', 'vision'] as const) {
    const key = `llm-chat-transcript:${pageMode}`;
    const raw = JSON.stringify({schemaVersion: 1, savedAt: 1, messages: [{role: 'assistant', content: 'display only', chatResume: {sessionId: `${pageMode}-chat`}}]});
    const values = new Map<string, string>([[key, raw]]);
    const storage = {getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => {values.set(key, value);}, removeItem: (key: string) => {values.delete(key);}};
    const candidate = selectSessionHubRecoveryCandidate(pageMode, storage);
    assert.equal(candidate?.bookmark.sessionId, `${pageMode}-chat`);
    assert.equal(values.get(key), raw, 'selection cannot consume or restore local bubbles');
    candidate!.commitMigration(storage);
    assert.equal(values.has(key), false);
    assert.ok(values.has(`llm-session-hub-bookmark:${pageMode}`));
  }
  const recovery = await page('src/lib/conversation/use-conversation-recovery.ts');
  assert.match(recovery, /s\.setMessages\(\[\]\)/);
  assert.match(recovery, /llmClient\.sessionHubEvents/);
  assert.match(recovery, /onSettled: result => \{\s*if \(!ownedController \|\| s\.abortControllerRef\.current !== ownedController\) return;/);
  assert.match(recovery, /settledTerminal = result\.kind === 'terminal';\s*if \(settledTerminal\) candidate\.commitMigration\(\)/);
  assert.match(recovery, /else \{\s*s\.setAttachedRecoverySessionId\(null\)/);
});

test("both pages submit and render as keyboard outside full-auto voice mode", async () => {
  const commands = await page("src/lib/conversation/use-conversation-commands.ts");
  const composer = await page("src/components/conversation/ConversationComposer.tsx");
  assert.match(commands, /if \(voiceController\.isVoiceMode && inputMode === 'voice'\)/);
  assert.match(commands, /if \(voiceController\.isVoiceMode && inputMode === 'dictation'\)/);
  assert.match(composer, /inputMode === 'voice' \?/);
  assert.match(composer, /<textarea/);
  assert.doesNotMatch(commands, /dictationResumeArmedRef|voiceTranscriptLandingRef|landPendingVoiceTranscriptIntoInput/);
});

test("both pages serialize asynchronous input-mode transitions", async () => {
  const shared = await page("src/lib/conversation/use-conversation-voice.ts");
  assert.match(shared, /voiceModeTransitionRef\.current \|\|/);
  assert.match(shared, /voiceModeTransitionRef\.current = true/);
  assert.match(shared, /finally[\s\S]*?voiceModeTransitionRef\.current = false/);
  assert.match(shared, /if \(mountedRef\.current\) setInputMode/);
});

test("configured transport bound produces a recoverable classified overflow", async () => {
  const source = await page("src/lib/voice-input.ts");
  assert.match(source, /config\.maxBufferedFrames/);
  assert.match(source, /socket\.bufferedAmount/);
  assert.match(source, /code:\s*["']transport_overflow["']/);
  assert.match(source, /recoverable:\s*true/);
});

test("both pages show startup errors without disabling recoverable retry", async () => {
  for (const relative of ["src/app/page.tsx", "src/app/vision/page.tsx"]) {
    const source = await page(relative);
    assert.match(source, /voiceAvailabilityDisabledReason/);
    assert.match(
      source,
      /voiceAvailabilityDisabledReason \?\? voiceController\.state\.error\?\.message/,
    );
    assert.match(
      source,
      /voiceController\.state\.error\?\.recoverable === false/,
    );
    assert.match(source, /voiceDisabled=\{voiceDisabled\}/);
    assert.match(source, /voiceDisabledReason=\{voiceDisabledReason\}/);
  }
});

test("clearing conversation resets voice mode so the toggle can restart ASR", async () => {
  const actions = await page("src/lib/conversation/conversation-actions.ts");
  assert.match(actions, /abortVoice\(\)/);
  assert.match(actions, /setInputMode\('keyboard'\)/);
  const shared = await page("src/lib/conversation/use-conversation-voice.ts");
  assert.match(shared, /mode === inputMode && mode === 'keyboard'/);
  for (const relative of ["src/app/page.tsx", "src/app/vision/page.tsx"]) assert.match(await page(relative), /clearConversation\(/);
});
