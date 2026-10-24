import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import test, { type TestContext } from 'node:test';
const require = createRequire(import.meta.url);
const { createRenderer, installTask6TypeScriptHook } = require('../vision-automation/react-component-renderer.cjs');
installTask6TypeScriptHook(fileURLToPath(new URL('../../../', import.meta.url)));
const React = require('react');
const {useConversationState} = require('./use-conversation-state.ts');
const tick = () => new Promise<void>((resolve) => setImmediate(resolve));

// Only browser-native audio, sockets and storage are replaced; both shared hooks,
// the existing useVoiceInput controller and the actual React renderer stay real.
function harness(t: TestContext, init: Record<string, unknown> = {}) {
  const {useConversationVoice} = require('./use-conversation-voice.ts');
  const {useConversationCommands} = require('./use-conversation-commands.ts');
  const tracks: any[] = [], contexts: any[] = [], worklets: any[] = [], sockets: any[] = [];
  const sends: unknown[][] = [], compactRequests: any[] = [];
  const revoked: string[] = [];
  let permissionRequests = 0, failPermission = false;
  class AudioNode { connect(node: unknown) { return node; } disconnect() {} }
  class AudioContext extends AudioNode {
    sampleRate: number; destination = new AudioNode(); closed = false;
    get audioWorklet() { return {addModule: async () => {}}; }
    constructor(options: {sampleRate: number}) { super(); this.sampleRate = options.sampleRate; contexts.push(this); }
    createMediaStreamSource() { return new AudioNode(); }
    createGain() { return Object.assign(new AudioNode(), {gain: {value: 1}}); }
    async close() {this.closed = true;} async resume() {}
  }
  class Worklet extends AudioNode {
    epoch = 0;
    messages: any[] = [];
    port = {onmessage: null as any, close() {}, postMessage: (message: any) => {this.messages.push(message); if (message.epoch !== undefined) this.epoch = message.epoch;}};
    constructor() {super(); worklets.push(this);}
    frame() {this.port.onmessage?.({data: {type: 'pcm_frame', epoch: this.epoch, samples: new Float32Array(320)}});}
  }
  class Socket extends EventTarget {
    static OPEN = 1;
    readyState = 0; bufferedAmount = 0; binaryType = ''; sent: unknown[] = [];
    onopen: any; onmessage: any; onclose: any; onerror: any;
    constructor() {super(); sockets.push(this);}
    open() {this.readyState = 1; this.onopen?.();}
    event(value: unknown) {const data = value instanceof ArrayBuffer ? value : JSON.stringify(value); this.onmessage?.({data}); this.dispatchEvent(new MessageEvent('message', {data}));}
    send(value: unknown) {this.sent.push(value); if(typeof value === 'string' && JSON.parse(value).type === 'abort') queueMicrotask(() => this.event({type: 'aborted'}));}
    close() {this.readyState = 3; this.onclose?.(); this.dispatchEvent(new Event('close'));}
  }
  const storage = new Map<string, string>();
  const intervals = new Map<number, () => void>();
  let timerId = 0;
  const pageDocument = Object.assign(new EventTarget(), {visibilityState: 'visible', activeElement: null, hasFocus: () => pageDocument.visibilityState === 'visible'});
  const originalGlobals = new Map<string, PropertyDescriptor | undefined>();
  function setGlobal(name: string, value: unknown) {originalGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name)); Object.defineProperty(globalThis, name, {configurable: true, writable: true, value});}
  setGlobal('window', {AudioContext, AudioWorkletNode: Worklet, isSecureContext: true, location: {protocol: 'https:', hostname: 'localhost', port: '8090'}, localStorage: {getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key,value), removeItem: (key: string) => storage.delete(key)}, setInterval: (fn: () => void) => {intervals.set(++timerId, fn); return timerId;}, clearInterval: (id: number) => intervals.delete(id)});
  setGlobal('document', pageDocument);
  setGlobal('navigator', {mediaDevices: {getUserMedia: async () => {permissionRequests++; if(failPermission) throw new Error('permission request failed'); const track = {stopped: false, onended: null, stop() {this.stopped = true;}}; tracks.push(track); return {getTracks: () => [track]};}}});
  setGlobal('AudioWorkletNode', Worklet); setGlobal('WebSocket', Socket);
  setGlobal('requestAnimationFrame', (callback: () => void) => {callback(); return 1;});
  const originalRevoke = URL.revokeObjectURL;
  URL.revokeObjectURL = (url: string) => {revoked.push(url); originalRevoke(url);};
  t.mock.method(console, 'warn', () => {});
  const config = {enabled: true, sampleRateHz: 16000, captureFrameMs: 20, inputGainDb: 0, inputGainMinDb: -12, inputGainMaxDb: 30, inputGainStepDb: 1, clipLimit: .999, autoSend: true, streaming: false, websocketPath: '/v1/asr/stream', requireSecureContext: true, abortTimeoutSeconds: .05, maxBufferedFrames: 250, dictationMagicSend: {commands: [], pollIntervalMs: 100}, browserConstraints: {echoCancellation: false, noiseSuppression: false, autoGainControl: false}, backendPort: 8090};
  const options: any = {busy: false, pendingAcceptance: false, cycleRunning: false, cycleStatus: 'idle', pageMode: 'text', ...init};
  let current: any;
  let compactResponder = async (_body: any, _event: any) => {};
  const send = async (...args: unknown[]) => {sends.push(args);};
  function Harness() {
    const state = useConversationState();
    // The ordinary state owner is real. Route transport refs remain the route's
    // job; here we mirror messages/settings exactly as the page composition does.
    state.messagesRef.current = state.messages;
    state.settingsRef.current = state.settings;
    const voice = useConversationVoice({state, send, busy: options.busy, pendingAcceptance: options.pendingAcceptance, cycleRunning: options.cycleRunning});
    const commands = useConversationCommands({state, pageMode: options.pageMode, busy: options.busy, cycleStatus: options.cycleStatus, send, voiceController: voice.voiceController, buildRuntimeTimeContext: () => 'Runtime pinned', client: {runCompact: async (body: any, event: any) => {compactRequests.push(body); await compactResponder(body, event);}}});
    current = {state, ...voice, ...commands};
    return null;
  }
  const renderer = createRenderer(React.createElement(Harness));
  t.after(async () => {renderer.unmount(); await tick(); URL.revokeObjectURL = originalRevoke; for(const [key, descriptor] of originalGlobals) {if(descriptor) Object.defineProperty(globalThis,key,descriptor); else Reflect.deleteProperty(globalThis,key);}});
  const act = (fn: () => unknown) => renderer.act(fn);
  const refresh = () => renderer.update(React.createElement(Harness));
  const start = async (mode = 'voice', autoSend = true) => {
    act(() => current.state.setVoiceConfig({...config, autoSend}));
    await current.handleVoiceModeChange(mode); renderer.flush();
    const socket = sockets.at(-1); assert.ok(socket);
    act(() => {socket.open(); socket.event({type: 'ready', protocol_version: 1, session_id: `asr-${sockets.length}`, state: 'buffering'});});
    return socket;
  };
  return {get current() {return current;}, renderer, act, refresh, start, options, config, sends, compactRequests, storage, intervals, tracks, contexts, worklets, sockets, revoked, permissionRequests: () => permissionRequests, failPermission(value: boolean) {failPermission = value;}, visibility(value: string) {act(() => {pageDocument.visibilityState = value; pageDocument.dispatchEvent(new Event('visibilitychange'));});}, compactResponder(fn: typeof compactResponder) {compactResponder = fn;}, pageDocument};
}

// Visibility is a reactive retry signal, while terminal/admission own resume.
// Hiding a page must never suspend its existing user-activated capture graph.
test('background terminal resumes retained voice while admission still gates capture', async (t) => {
  const h = harness(t);
  const socket = await h.start();
  h.options.busy = true; h.refresh();
  h.act(() => {socket.event({type: 'transcribing'}); socket.event({type: 'transcript', text: 'Question'});});
  assert.deepEqual(h.sends, [['Question', undefined]]);
  h.visibility('hidden'); h.options.busy = false; h.refresh();
  h.act(() => socket.event({type: 'state_changed', state: 'buffering'}));
  h.worklets[0].frame();
  assert.equal(socket.sent.filter((item: unknown) => item instanceof ArrayBuffer).length, 1);
  assert.equal(h.tracks[0].stopped, false);
  h.options.pendingAcceptance = true; h.refresh(); h.act(() => h.current.voiceController.pauseForResponse());
  h.visibility('visible'); h.worklets[0].frame();
  assert.equal(socket.sent.filter((item: unknown) => item instanceof ArrayBuffer).length, 1);
  h.options.pendingAcceptance = false; h.refresh(); await tick();
  const resumed = h.sockets.at(-1);
  if (resumed !== socket) h.act(() => {resumed.open(); resumed.event({type: 'ready', protocol_version: 1, session_id: 'resumed', state: 'buffering'});});
  h.worklets[0].frame();
  assert.equal(h.current.voiceController.state.phase, 'listening');
  assert.equal(h.permissionRequests(), 1); assert.equal(h.contexts.length, 1);
});

test('dictation inserts at selection, releases replay and never sends its ordinary transcript', async (t) => {
  const h = harness(t);
  const socket = await h.start('dictation');
  let caret: unknown;
  const input = {value: 'abXYcd', selectionStart: 2, selectionEnd: 4, setSelectionRange: (...args: unknown[]) => {caret = args;}};
  h.current.state.inputRef.current = input; h.pageDocument.activeElement = input as any;
  h.act(() => {socket.event({type: 'transcribing'}); socket.event({type: 'replay_audio', utterance_id: 'r1', mime_type: 'audio/wav', byte_length: 8}); socket.event(new Uint8Array(8).buffer); socket.event({type: 'transcript', text: 'word', replay_utterance_id: 'r1'});});
  assert.equal(h.current.state.input, 'abwordcd'); assert.deepEqual(caret, [6,6]);
  assert.deepEqual(h.sends, []); assert.equal(h.revoked.length, 1);
});

test('null voice config remains inactive and malformed enabled config keeps controller diagnostics visible', async (t) => {
  const h = harness(t);
  await h.current.handleVoiceModeChange('voice');
  assert.equal(h.current.state.inputMode, 'keyboard'); assert.equal(h.permissionRequests(), 0);
  h.act(() => h.current.state.setVoiceConfig({...h.config, captureFrameMs: NaN}));
  await h.current.handleVoiceModeChange('voice'); h.renderer.flush();
  assert.equal(h.current.state.inputMode, 'voice');
  assert.equal(h.current.voiceController.state.error?.code, 'protocol_error');
});

test('failed voice startup remains diagnosable and explicit retry uses the actual controller', async (t) => {
  const h = harness(t); h.failPermission(true);
  h.act(() => h.current.state.setVoiceConfig(h.config));
  await h.current.handleVoiceModeChange('voice'); h.renderer.flush();
  assert.equal(h.current.state.inputMode, 'voice');
  assert.ok(h.current.voiceController.state.error);
  h.failPermission(false); await h.current.voiceController.activateVoice(); h.renderer.flush();
  const socket = h.sockets.at(-1); h.act(() => {socket.open(); socket.event({type: 'ready', protocol_version: 1, session_id: 'retry', state: 'buffering'});});
  assert.equal(h.current.voiceController.state.phase, 'listening'); assert.equal(h.permissionRequests(), 2);
});

test('keyboard transition preserves manual transcript replay and retires old audio callbacks', async (t) => {
  const h = harness(t); const socket = await h.start('voice', false);
  h.act(() => {socket.event({type: 'transcribing'}); socket.event({type: 'replay_audio', utterance_id: 'r1', mime_type: 'audio/wav', byte_length: 8}); socket.event(new Uint8Array(8).buffer); socket.event({type: 'transcript', text: 'Manual phrase', replay_utterance_id: 'r1'});});
  const replay = h.current.voiceController.state.pendingReplay;
  await h.current.handleVoiceModeChange('keyboard'); h.renderer.flush();
  assert.equal(h.current.state.input, 'Manual phrase');
  assert.deepEqual(h.current.state.pendingKeyboardVoiceReplayRef.current, {transcript: 'Manual phrase', replay});
  assert.equal(h.tracks[0].stopped, true); assert.equal(h.contexts[0].closed, true);
  h.act(() => socket.event({type: 'transcript', text: 'Late old transcript'}));
  assert.equal(h.current.state.input, 'Manual phrase'); assert.deepEqual(h.sends, []);
});

test('dictation magic send waits for admission and retains the exact source draft', async (t) => {
  const h = harness(t); await h.start('dictation');
  h.act(() => {h.current.state.setDictationMagicSendSettings({rules: [{text: 'SEND', enabled: true}], pollIntervalMs: 100}); h.current.state.setInput('Question SEND');});
  assert.equal(h.current.dictationMagicSendPending, true);
  h.options.pendingAcceptance = true; h.refresh();
  for(const poll of h.intervals.values()) poll(); await tick(); assert.deepEqual(h.sends, []);
  h.options.pendingAcceptance = false; h.refresh();
  for(const poll of h.intervals.values()) poll(); await tick();
  assert.deepEqual(h.sends, [['Question', undefined, 'Question SEND']]);
});

const history = () => [{id: 'u', role: 'user', content: 'User request', timestamp: new Date(0)}, {id: 'a', role: 'assistant', content: 'Assistant answer', timestamp: new Date(0)}];
test('templates and keyboard commands reuse send and controlled slash state', async (t) => {
  const h = harness(t); let focused = false;
  h.current.state.inputRef.current = {focus: () => {focused = true;}};
  h.act(() => h.current.handleInsertMessageTemplate('Template draft'));
  assert.equal(h.current.state.input, 'Template draft'); assert.equal(focused, true);
  h.current.handleSendMessageTemplate('Immediate template'); await tick(); assert.deepEqual(h.sends, [['Immediate template']]);
  h.act(() => h.current.onInputChange('/co', 3)); assert.equal(h.current.state.slashMenu.visible, true);
  const event = (key: string, extra = {}) => ({key, shiftKey: false, ctrlKey: false, metaKey: false, nativeEvent: {isComposing: false}, preventDefault() {}, ...extra});
  h.act(() => h.current.handleKeyDown(event('Tab'))); assert.equal(h.current.state.input, '/compact '); assert.equal(h.current.state.slashMenu.visible, false);
  h.act(() => h.current.state.setInput('Typed draft'));
  h.act(() => h.current.handleKeyDown(event('Enter'))); assert.equal(h.sends.length, 1);
  h.act(() => h.current.handleKeyDown(event('Enter', {ctrlKey: true}))); await tick(); assert.deepEqual(h.sends.at(-1), []);
});

test('manual compact uses paired Agent identity and exact Chat history body with pinned runtime', async (t) => {
  const h = harness(t);
  h.act(() => {h.current.state.setMessages(history()); h.current.state.setSettings((s: any) => ({...s, systemPrompt: 'System'})); h.current.state.setRuntimeContextConfig({enable_runtime_time_context: true}); h.current.state.setRuntimeTimeContext('Pinned runtime'); h.current.state.setChatSessionId('chat-context');});
  const command = h.current.filteredSlashCommands[0];
  await h.current.handleSelectSlashCommand(command, '<Keep active task>'); h.renderer.flush();
  assert.equal(h.compactRequests[0].mode, 'chat'); assert.equal(h.compactRequests[0].session_id, 'chat-context');
  assert.deepEqual(h.compactRequests[0].messages.map((m: any) => [m.role,m.content]), [['system','System\n\nPinned runtime'],['user','User request'],['assistant','Assistant answer']]);
  assert.equal(h.compactRequests[0].custom_instructions, 'Keep active task');
  h.act(() => {h.current.state.setSettings((s: any) => ({...s, agentMode: true})); h.current.state.setAgentSessionId('unpaired-identity');});
  await h.current.handleSelectSlashCommand(command); assert.equal(h.compactRequests.length, 1);
  assert.deepEqual(h.current.state.messages, history());
  const candidate = {schema_version: 1, catalog_revision: 'r1', manager_agent_tools: ['read_file'], basic_agent_tools: ['read_file']};
  h.act(() => h.current.state.dispatchAgentToolState({type: 'begin', sessionId: 'paired-agent', requestId: 'request', candidate}));
  await h.current.handleSelectSlashCommand(command);
  assert.equal(h.compactRequests[1].mode, 'agent'); assert.equal(h.compactRequests[1].session_id, 'paired-agent');
});

test('compact stream failure rolls back only its newly marked history and busy commands do not submit', async (t) => {
  const h = harness(t); h.act(() => h.current.state.setMessages(history()));
  h.compactResponder(async (_body, emit) => {emit({kind: 'compact_started', summary_message_id: 'summary-1', mode: 'chat'}); emit({kind: 'compact_failed', summary_message_id: 'summary-1', metadata: {reason: 'validation_failed'}});});
  await h.current.handleSelectSlashCommand(h.current.filteredSlashCommands[0]); h.renderer.flush();
  assert.equal(h.current.state.messages[0].isPreCompact, false);
  assert.equal(h.current.state.messages.length, 2);
  assert.deepEqual(h.current.state.messages.map((m: any) => m.content), ['User request','Assistant answer']);
  h.options.busy = true; h.refresh();
  await h.current.handleSelectSlashCommand(h.current.filteredSlashCommands[0]); assert.equal(h.compactRequests.length, 1);
});

test('manual dictation transcript is inserted once while awaiting readiness and resumes after admission', async (t) => {
  const h = harness(t); const socket = await h.start('dictation', false);
  h.options.pendingAcceptance = true; h.refresh();
  h.act(() => {socket.event({type: 'transcribing'}); socket.event({type: 'transcript', text: 'First phrase'});});
  assert.equal(h.current.state.input, 'First phrase');
  assert.equal(h.current.voiceController.state.phase, 'transcript_ready');
  h.act(() => h.current.state.setInput('Edited phrase'));
  assert.equal(h.current.state.input, 'Edited phrase', 'rerenders must not insert the retained transcript twice');
  h.act(() => socket.event({type: 'state_changed', state: 'buffering'}));
  h.worklets[0].frame(); assert.equal(socket.sent.filter((value: unknown) => value instanceof ArrayBuffer).length, 0);
  h.options.pendingAcceptance = false; h.refresh(); h.worklets[0].frame();
  assert.equal(socket.sent.filter((value: unknown) => value instanceof ArrayBuffer).length, 1);
  assert.equal(h.current.voiceController.state.phase, 'listening');
});

test('manual voice submission sends trimmed transcript and its original replay through ordinary send', async (t) => {
  const h = harness(t); const socket = await h.start('voice', false);
  h.act(() => {socket.event({type: 'transcribing'}); socket.event({type: 'replay_audio', utterance_id: 'r2', mime_type: 'audio/wav', byte_length: 8}); socket.event(new Uint8Array(8).buffer); socket.event({type: 'transcript', text: '  Spoken request  ', replay_utterance_id: 'r2'});});
  const replay = h.current.voiceController.state.pendingReplay;
  assert.deepEqual(h.sends, []);
  h.options.busy = true; h.refresh();
  h.act(() => h.current.handleSubmit()); await tick();
  assert.deepEqual(h.sends, [['Spoken request', replay]]);
  assert.equal(h.current.voiceController.state.phase, 'paused_for_response');
  assert.deepEqual(h.revoked, [], 'the ordinary send path owns the submitted replay');
});

test('template edits persist, restore YAML defaults and expose browser storage rejection', async (t) => {
  const h = harness(t);
  h.act(() => {h.current.state.setYamlMessageTemplates(['Default']); h.current.handleMessageTemplatesChange(['Saved']); h.current.handleMessageTemplateImmediateSendChange(false);});
  assert.equal(h.storage.get('llm-message-templates'), '["Saved"]');
  assert.equal(h.storage.get('llm-message-template-immediate-send'), 'false');
  h.act(() => h.current.handleRestoreMessageTemplates());
  assert.deepEqual(h.current.state.messageTemplates, ['Default']); assert.equal(h.storage.has('llm-message-templates'), false);
  const storage = (globalThis as any).window.localStorage;
  t.mock.method(storage, 'setItem', () => {throw new Error('storage disabled');});
  h.act(() => h.current.handleMessageTemplatesChange(['Unsaved live value']));
  assert.deepEqual(h.current.state.messageTemplates, ['Unsaved live value']);
  assert.equal(h.current.state.messageTemplateStorageError, '消息模板无法保存到浏览器缓存');
});
