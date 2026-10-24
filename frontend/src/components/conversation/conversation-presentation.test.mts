import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
const require = createRequire(import.meta.url);
const { createRenderer, installTask6TypeScriptHook } = require('../../lib/vision-automation/react-component-renderer.cjs');
installTask6TypeScriptHook(fileURLToPath(new URL('../../../', import.meta.url)));
const React = require('react');
const h = React.createElement;
const component = (name: string) => require(`./${name}.tsx`)[name];
const noop = () => {};
const message = (id: string, content: string, extra = {}) => ({ id, content, role: 'assistant', timestamp: new Date(0), ...extra });
const listProps = (messages: unknown[]) => ({ messages, settings: {agentMode: true, showReasoning: true}, bubbleCharacterLimit: 20000, foldThinkingAfterFinalAnswer: false, stoppedMessageIds: new Set(), collapsedCompact: new Set(), onToggleCompact: noop, onToggleReasoning: noop, onOpenImage: noop, getUserText: (m: {content: string}) => m.content, emptyContent: 'Welcome' });
const button = (r: ReturnType<typeof createRenderer>, text: string) => r.findOne((n: any) => n.type === 'button' && r.text(n) === text);
const text = (r: ReturnType<typeof createRenderer>) => r.container.children.map(r.text).join('');

test('message list keeps before/body/after dispatch order and orchestration child cards', () => {
  const Component = component('ConversationMessageList');
  const steps = [
    {step_type: 'dispatch', display_slot: 'before_body', dispatch_kind: 'secondary_handoff', step_id: 'before', dispatch_status: 'completed', dispatch_target_agent: 'Leading handoff'},
    {step_type: 'orchestration', orchestration_type: 'plan', step_id: 'plan', stages: [{stage: 1, tools: [{name: 'dispatching_parallel_agent', call_id: 'call-1', status: 'completed'}]}]},
    {step_type: 'dispatch', display_slot: 'after_body', dispatch_kind: 'secondary_handoff', step_id: 'after', dispatch_status: 'completed', dispatch_target_agent: 'Trailing handoff'},
  ];
  const r = createRenderer(h(Component, listProps([message('a', 'Visible body', {agentSteps: steps, managerDispatches: {d1: [{dispatchId: 'd1', callId: 'call-1', childRunId: 'child-1', childName: 'Research child', status: 'completed', finalPreview: 'Child result'}]}})])));
  try {
    const body = text(r);
    assert.ok(body.indexOf('Leading handoff') >= 0 && body.indexOf('Leading handoff') < body.indexOf('Visible body'), body);
    assert.ok(body.indexOf('Research child') >= 0 && body.indexOf('Research child') < body.indexOf('Visible body'), body);
    assert.ok(body.indexOf('Trailing handoff') > body.indexOf('Visible body'), body);
    assert.equal(r.findOne((n: any) => n.type === 'a' && r.text(n) === 'Open child').props.href, '/agent/child/child-1');
  } finally { r.unmount(); }
});

test('message list delegates controlled reasoning and compact toggles, and retains stopped content without dimming', () => {
  const Component = component('ConversationMessageList');
  const toggles: string[] = [];
  const props = {...listProps([message('a', '', {reasoning_content: 'Retained reasoning', reasoningCollapsed: false, isPreCompact: true}), message('c', 'Compact body', {bubbleKind: 'compact_summary', compactStatus: 'completed', compactMetadata: {originalTokens: 1000, summaryTokens: 200}})]), stoppedMessageIds: new Set(['a']), onToggleReasoning: (id: string) => toggles.push(id), onToggleCompact: (id: string) => toggles.push(id)};
  const r = createRenderer(h(Component, props));
  try {
    assert.match(text(r), /已停止/);
    assert.match(text(r), /Retained reasoning/);
    assert.match(text(r), /1\.0k.*200/s);
    r.click(button(r, '收起推理过程已完成'));
    r.click(button(r, 'Collapse'));
    assert.deepEqual(toggles, ['a', 'c']);
    assert.equal(r.findAll((n: any) => String(n.props.className).split(/\s+/).some((name: string) => /^opacity-/.test(name))).length, 0);
    r.update(h(Component, {...props, collapsedCompact: new Set(['c'])}));
    assert.equal(r.findAll((n: any) => n.kind === 'text' && n.text === 'Compact body').length, 0);
  } finally { r.unmount(); }
});

test('user display images, historical omission summaries, replay and retrieved memory stay displayable', () => {
  const Component = component('ConversationMessageList');
  const opened: string[] = [];
  const r = createRenderer(h(Component, {...listProps([message('u', 'User text', {role: 'user', images: ['data:image/png;base64,AA'], historicalImageSummaries: [{count: 2, sources: ['manual'], relativeRange: {startSeconds: 1, endSeconds: 2}}], voiceReplay: {url: 'blob:replay', mimeType: 'audio/wav', utteranceId: 'v1'}, localMemories: [{text: 'Retrieved excerpt'}]})]), onOpenImage: (src: string) => opened.push(src)}));
  try {
    r.click(r.findOne((n: any) => n.type === 'img' && n.props.src === 'data:image/png;base64,AA'));
    assert.deepEqual(opened, ['data:image/png;base64,AA']);
    assert.match(text(r), /1\.00\.\.2\.00s/);
    assert.equal(r.findOne((n: any) => n.type === 'audio').props.src, 'blob:replay');
    r.click(button(r, '查看本地记忆（1）'));
    assert.match(text(r), /Retrieved excerpt/);
  } finally { r.unmount(); }
});

const settings = {temperature: .7, topP: .9, topK: 40, repetitionPenalty: 1, maxTokens: 2048, systemPrompt: '', showReasoning: true, enterToSend: true, enableThinking: true, enableLocalMemory: false, agentMode: false, managerAgentTools: [], basicAgentTools: []};
test('settings edits remain pending and confirmation/reset/cancel belong to the parent', () => {
  const Component = component('ConversationSettingsDialog');
  let pending = {...settings};
  const events: string[] = [];
  const props = {open: true, pendingSettings: pending, setPendingSettings: (update: any) => {pending = typeof update === 'function' ? update(pending) : update;}, settingsTab: 'general', setSettingsTab: (v: string) => events.push(v), pendingSkinId: 'default', setPendingSkinId: noop, maxContextLength: 8192, agentToolCatalog: {schema_version: 1, catalog_revision: 'r1', tools: [], default_profile_tools: {manager_agent: [], basic_agent: []}}, sessionRecord: null, toolNotice: null, resetDisabled: false, onReset: () => events.push('reset'), onCancel: () => events.push('cancel'), onDismiss: () => events.push('dismiss'), onConfirm: () => events.push('confirm')};
  const r = createRenderer(h(Component, props));
  try {
    r.act(() => r.findOne((n: any) => n.type === 'input' && n.props.value === .7).props.onChange({target: {value: '0'}}));
    assert.equal(pending.temperature, 0);
    assert.equal(settings.temperature, .7);
    r.click(button(r, '重置为服务器默认值'));
    r.click(button(r, '确认'));
    r.click(button(r, '取消'));
    r.click(button(r, '高级设置'));
    assert.deepEqual(events, ['reset', 'confirm', 'cancel', 'advanced']);
    r.update(h(Component, {...props, open: false}));
    assert.equal(text(r), '');
  } finally { r.unmount(); }
});

const voiceState = {phase: 'keyboard', generation: 0, sessionId: null, levelDbfs: null, clippingRatio: 0, waveform: [], partialTranscript: '', pendingTranscript: '', pendingReplay: null, error: null};
const composerProps = () => ({busy: false, cycleRunning: false, connectionError: false, sendDisabled: false, input: 'Draft', inputMode: 'keyboard', voiceController: {state: voiceState, inputGain: null, isVoiceMode: false, activateVoice: async () => {}}, showInputActions: true, onToggleInputActions: noop, onInputChange: noop, onKeyDown: noop, onSubmit: noop, onStop: noop, onSwitchToKeyboard: noop, slashMenu: {visible: false, query: '', selectedIndex: 0}, filteredSlashCommands: [], disabledCommandMap: new Map(), onSelectSlashCommand: noop, onCloseSlashMenu: noop, actions: h('span', null, 'Capability actions'), attachments: h('span', null, 'Attachments'), notices: h('span', null, 'Notice')});
test('composer preserves controlled keyboard actions, capability slots and Stop while busy', () => {
  const Component = component('ConversationComposer');
  const events: unknown[] = [];
  const props = {...composerProps(), onInputChange: (value: string, start: number) => events.push([value, start]), onSubmit: () => events.push('submit'), onStop: () => events.push('stop'), onToggleInputActions: () => events.push('actions')};
  const r = createRenderer(h(Component, props));
  try {
    assert.match(text(r), /Capability actions.*Attachments.*Notice/s);
    r.act(() => r.findOne((n: any) => n.type === 'textarea').props.onChange({target: {value: 'new', selectionStart: 1}}));
    r.click(r.findOne((n: any) => n.props['aria-label'] === '发送'));
    r.click(r.findOne((n: any) => n.props['aria-label'] === '展开输入操作'));
    r.update(h(Component, {...props, busy: true}));
    r.click(r.findOne((n: any) => n.props.title === '停止'));
    assert.deepEqual(events, [['new', 1], 'submit', 'actions', 'stop']);
    assert.equal(r.findAll((n: any) => n.props['aria-label'] === '发送').length, 0);
    assert.doesNotMatch(text(r), /Answer now|立即回答/i);
  } finally { r.unmount(); }
});

test('voice panel remains visible after failed startup and exposes retry/keyboard actions', () => {
  const Component = component('ConversationComposer');
  const events: string[] = [];
  const props = {...composerProps(), inputMode: 'voice', voiceController: {state: {...voiceState, error: {code: 'backend_error', message: 'Startup failed', recoverable: true}}, inputGain: null, isVoiceMode: false, activateVoice: async () => { events.push('retry'); }}, onSwitchToKeyboard: () => events.push('keyboard')};
  const r = createRenderer(h(Component, props));
  try {
    assert.match(text(r), /Startup failed/);
    assert.equal(r.findAll((n: any) => n.type === 'textarea').length, 0);
    r.click(button(r, 'Try again'));
    r.click(button(r, 'Use keyboard'));
    assert.deepEqual(events, ['retry', 'keyboard']);
  } finally { r.unmount(); }
});

test('header uses route title and delegates recovery, thinking, memory, settings and clear', () => {
  const Component = component('ConversationHeader');
  const events: unknown[] = [];
  const r = createRenderer(h(Component, {pageMode: 'text', webUIConfig: {components: {page_header: {display: true}}}, modelName: 'Qwen', agentMode: false, connectionStatus: 'connected', inputMode: 'keyboard', onVoiceModeChange: (mode: string) => events.push(mode), voiceDisabled: false, canRecover: true, onRecover: () => events.push('recover'), enableThinking: true, onToggleThinking: () => events.push('thinking'), enableLocalMemory: false, onToggleLocalMemory: (enabled: boolean) => events.push(enabled), onOpenSettings: () => events.push('settings'), onClear: () => events.push('clear')}));
  try {
    r.click(button(r, '恢复运行中的任务'));
    for (const label of ['思考模式已开启', '设置', '清空对话', '听写输入']) r.click(r.findOne((n: any) => n.type === 'button' && n.props['aria-label'] === label));
    assert.deepEqual(events, ['recover', 'thinking', 'settings', 'clear', 'dictation']);
    assert.match(text(r), /Qwen/);
  } finally { r.unmount(); }
});

test('execution bubble suppresses prior before-body handoff and retains following handoff before its body', () => {
  const Component = component('ConversationMessageList');
  const r = createRenderer(h(Component, listProps([message('execution', 'Execution result', {bubbleKind: 'execution', agentSteps: [
    {step_type: 'dispatch', display_slot: 'before_body', dispatch_kind: 'secondary_handoff', dispatch_status: 'completed', dispatch_target_agent: 'Previous handoff'},
    {step_type: 'dispatch', display_slot: 'after_body', dispatch_kind: 'secondary_handoff', dispatch_status: 'completed', dispatch_target_agent: 'Next handoff'},
  ]})])));
  try {
    const body = text(r);
    assert.doesNotMatch(body, /Previous handoff/);
    assert.ok(body.indexOf('Next handoff') >= 0 && body.indexOf('Next handoff') < body.indexOf('Execution result'), body);
  } finally { r.unmount(); }
});

test('empty content is route-owned and stopped partial assistant output is retained', () => {
  const Component = component('ConversationMessageList');
  const props = listProps([]);
  const r = createRenderer(h(Component, props));
  try {
    assert.equal(text(r), 'Welcome');
    r.update(h(Component, {...props, messages: [message('partial', 'Already produced')], stoppedMessageIds: new Set(['partial'])}));
    assert.match(text(r), /Already produced/);
    assert.doesNotMatch(text(r), /思考中/);
  } finally { r.unmount(); }
});

test('settings retain session tool locks and Vision automation mode lock without confirming locally', () => {
  const Component = component('ConversationSettingsDialog');
  const events: string[] = [];
  const candidate = {schema_version: 1, catalog_revision: 'r1', manager_agent_tools: ['read_file'], basic_agent_tools: ['read_file']};
  const props = {open: true, pendingSettings: {...settings, agentMode: true}, setPendingSettings: noop, settingsTab: 'advanced', setSettingsTab: noop, pendingSkinId: 'default', setPendingSkinId: noop, maxContextLength: 8192, agentToolCatalog: {...candidate, tools: [{name: 'read_file', order: 0}], default_profile_tools: {manager_agent: ['read_file'], basic_agent: ['read_file']}}, sessionRecord: {kind: 'pending', sessionId: 'session', requestId: 'request', candidate}, toolNotice: 'Admission pending', resetDisabled: true, visionAutomationArmed: true, onReset: () => events.push('reset'), onCancel: noop, onDismiss: () => events.push('dismiss'), onConfirm: () => events.push('confirm')};
  const r = createRenderer(h(Component, props));
  try {
    assert.match(text(r), /Admission pending/);
    assert.match(text(r), /Vision automation/);
    assert.equal(button(r, '重置为服务器默认值').props.disabled, true);
    assert.ok(r.findAll((n: any) => n.type === 'input' && n.props.type === 'checkbox').every((n: any) => n.props.disabled));
    r.click(r.findOne((n: any) => n.type === 'div' && n.props.className === 'absolute inset-0 bg-black/50'));
    assert.deepEqual(events, ['dismiss']);
    assert.match(text(r), /Admission pending/);
  } finally { r.unmount(); }
});

test('composer forwards keyboard and slash actions, disables Cycle/connection input, and keeps placeholder setting', () => {
  const Component = component('ConversationComposer');
  const events: unknown[] = [];
  const allowed = {name: '/clear', description: 'Clear', run: async () => ({})};
  const disabled = {name: '/compact', description: 'Compact', run: async () => ({})};
  const props = {...composerProps(), enterToSend: true, onKeyDown: (event: unknown) => events.push(event), slashMenu: {visible: true, query: '', selectedIndex: 0}, filteredSlashCommands: [allowed, disabled], disabledCommandMap: new Map([['/compact', 'Busy']]), onSelectSlashCommand: (command: unknown) => events.push(command), onCloseSlashMenu: () => events.push('close')};
  const r = createRenderer(h(Component, props));
  try {
    const input = r.findOne((n: any) => n.type === 'textarea');
    assert.match(input.props.placeholder, /Enter/);
    r.act(() => input.props.onKeyDown('keyboard event'));
    r.click(r.findOne((n: any) => n.props.role === 'option' && r.text(n).includes('/clear')));
    r.click(r.findOne((n: any) => n.props.role === 'option' && r.text(n).includes('/compact')));
    r.click(r.findOne((n: any) => n.props['aria-label'] === 'Close slash menu'));
    assert.deepEqual(events, ['keyboard event', allowed, 'close']);
    for (const extra of [{cycleRunning: true}, {connectionError: true}]) {
      r.update(h(Component, {...props, ...extra}));
      assert.equal(r.findOne((n: any) => n.type === 'textarea').props.disabled, true);
      assert.equal(r.findOne((n: any) => n.props['aria-label'] === '发送').props.disabled, true);
    }
  } finally { r.unmount(); }
});

test('dictation shows editable draft and settings while voice mode keeps the dedicated panel', () => {
  const Component = component('ConversationComposer');
  let opened = 0;
  const props = {...composerProps(), inputMode: 'dictation', voiceController: {state: {...voiceState, phase: 'listening'}, inputGain: null, isVoiceMode: true, activateVoice: async () => {}}, onOpenDictationSettings: () => { opened += 1; }};
  const r = createRenderer(h(Component, props));
  try {
    assert.equal(r.findOne((n: any) => n.type === 'textarea').props.value, 'Draft');
    r.click(r.findOne((n: any) => n.props['aria-label'] === 'Dictation magic send settings'));
    assert.equal(opened, 1);
    r.update(h(Component, {...props, inputMode: 'voice'}));
    assert.equal(r.findAll((n: any) => n.type === 'textarea').length, 0);
    assert.equal(r.findAll((n: any) => n.props['aria-label'] === 'Voice input').length, 1);
  } finally { r.unmount(); }
});

test('header reads route-specific titles, voice capability and controlled memory preference', () => {
  const Component = component('ConversationHeader');
  const enabled: boolean[] = [];
  const props = {pageMode: 'text', webUIConfig: {components: {page_header: {display: true, text_title: 'Text title', vision_title: 'Vision title'}}}, modelName: 'Qwen', agentMode: false, connectionStatus: 'error', inputMode: 'keyboard', onVoiceModeChange: noop, voiceDisabled: true, voiceDisabledReason: 'Backend unavailable', canRecover: false, onRecover: noop, enableThinking: false, onToggleThinking: noop, enableLocalMemory: false, onToggleLocalMemory: (value: boolean) => enabled.push(value), onOpenSettings: noop, onClear: noop};
  const r = createRenderer(h(Component, props));
  try {
    assert.equal(r.text(r.findOne((n: any) => n.type === 'h1')), 'Text title');
    assert.equal(r.findOne((n: any) => n.type === 'button' && n.props['aria-label'] === '语音输入').props.disabled, true);
    assert.equal(r.findOne((n: any) => n.type === 'button' && n.props['aria-label'] === '键盘输入').props.disabled, false);
    assert.equal(r.findAll((n: any) => n.type === 'button' && r.text(n) === '恢复运行中的任务').length, 0);
    r.click(r.findOne((n: any) => n.type === 'button' && n.props['aria-label'] === '发送时不检索本地记忆'));
    assert.deepEqual(enabled, [true]);
    r.update(h(Component, {...props, pageMode: 'vision', enableLocalMemory: true}));
    assert.equal(r.text(r.findOne((n: any) => n.type === 'h1')), 'Vision title');
    r.click(r.findOne((n: any) => n.type === 'button' && n.props['aria-label'] === '发送时检索本地记忆'));
    assert.deepEqual(enabled, [true, false]);
  } finally { r.unmount(); }
});

test('Agent mode indicator follows controlled mode on both visible route headers', () => {
  const Component = component('ConversationHeader');
  const props = {pageMode: 'text', webUIConfig: {components: {page_header: {display: true}}}, modelName: 'Qwen', agentMode: false, connectionStatus: 'connected', inputMode: 'keyboard', onVoiceModeChange: noop, voiceDisabled: false, canRecover: false, onRecover: noop, enableThinking: false, onToggleThinking: noop, enableLocalMemory: false, onToggleLocalMemory: noop, onOpenSettings: noop, onClear: noop};
  const r = createRenderer(h(Component, props));
  const badge = () => r.findAll((n: any) => n.type === 'span' && r.text(n).trim() === 'Agent');
  try {
    assert.equal(badge().length, 0);
    for (const pageMode of ['text', 'vision']) {
      r.update(h(Component, {...props, pageMode, agentMode: true}));
      assert.equal(badge().length, 1);
      assert.match(badge()[0].props.className, /bg-amber-100/);
      assert.equal(badge()[0].children.some((n: any) => n.type === 'svg'), true);
      r.update(h(Component, {...props, pageMode, agentMode: false}));
      assert.equal(badge().length, 0);
    }
    r.update(h(Component, {...props, agentMode: true, webUIConfig: {components: {page_header: {display: false}}}}));
    assert.equal(badge().length, 0, 'hidden page branding also hides its mode indicator');
  } finally { r.unmount(); }
});

const cycleConfig = {enabled: true, max_depth: 4, show_thumbnails: false, default_sequence: [{type: 'message', message: 'Scheduled work', photo: []}], default_scheduled_messages: []};
const inputActionProps = () => ({visible: true, cycleConfig, cycleStatus: 'idle', cycleEvents: [], showCycle: false, onToggleCycle: noop, onCycleStart: noop, onCycleStop: noop, messageTemplateMenu: h('button', {type: 'button'}, 'Common templates')});

test('common non-media actions retain templates and controlled Cycle toggle while all media inputs stay disabled', () => {
  const Component = component('ConversationTextInputActions');
  let toggled = 0;
  const props = {...inputActionProps(), onToggleCycle: () => { toggled += 1; }};
  const r = createRenderer(h(Component, props));
  try {
    assert.equal(button(r, '上传图片').props.disabled, true);
    assert.equal(button(r, '摄像头拍照').props.disabled, true);
    assert.equal(r.findAll((n: any) => n.type === 'input' && n.props.type === 'file').length, 0);
    assert.equal(r.findAll((n: any) => n.type === 'video').length, 0);
    assert.equal(button(r, 'Common templates').props.disabled, undefined);
    r.click(button(r, '编程循环'));
    assert.equal(toggled, 1);
    assert.equal(r.findAll((n: any) => n.props['data-testid'] === 'text-input-action-grid').length, 1);
    r.update(h(Component, {...props, visible: false}));
    assert.equal(text(r), '');
    r.update(h(Component, {...props, cycleConfig: {...cycleConfig, enabled: false}}));
    assert.equal(button(r, '编程循环').props.disabled, true);
    assert.equal(button(r, 'Common templates').props.disabled, undefined);
  } finally { r.unmount(); }
});

test('common non-media Cycle editor forwards validated no-photo programs and controlled stop/progress', async (t) => {
  const Component = component('ConversationTextInputActions');
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', {configurable: true, value: {getItem: () => null, setItem: noop}});
  t.after(() => {if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage); else Reflect.deleteProperty(globalThis, 'localStorage');});
  t.mock.timers.enable({apis: ['setTimeout']});
  const starts: unknown[][] = [];
  let stopped = 0;
  const props = {...inputActionProps(), showCycle: true, onCycleStart: (...args: unknown[]) => {starts.push(args);}, onCycleStop: () => {stopped += 1;}};
  const r = createRenderer(h(Component, props));
  try {
    assert.equal(r.findAll((n: any) => n.type === 'button' && /camera|相机|摄像头/.test(r.text(n)) && !n.props.disabled).length, 0);
    r.click(button(r, 'Start'));
    assert.deepEqual(starts, [[[{type: 'message', message: 'Scheduled work', photo: []}], [], true, false]]);
    await Promise.resolve(); t.mock.timers.runAll(); r.flush();
    r.update(h(Component, {...props, cycleStatus: 'running', cycleEvents: [{type: 'step_begin', step_type: 'message', path: [0]}]}));
    assert.match(text(r), /Cycle timeline/);
    r.click(button(r, 'Stop'));
    assert.equal(stopped, 1);
  } finally { r.unmount(); }
});

test('common non-media Cycle rejects a saved photo program without starting or changing browser data', (t) => {
  const Component = component('ConversationTextInputActions');
  const saved = JSON.stringify({steps: [{type: 'message', message: 'Keep saved task', photo: [0]}], scheduledMessages: [], shareContext: true, showAgentSteps: false});
  const writes: unknown[] = [];
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', {configurable: true, value: {getItem: () => saved, setItem: (...args: unknown[]) => writes.push(args)}});
  t.after(() => {if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage); else Reflect.deleteProperty(globalThis, 'localStorage');});
  let started = 0;
  const r = createRenderer(h(Component, {...inputActionProps(), showCycle: true, onCycleStart: () => {started += 1;}}));
  try {
    r.click(button(r, 'Start'));
    assert.equal(started, 0);
    assert.match(text(r), /steps\[0\]\.photo/);
    assert.match(text(r), /整次未启动/);
    assert.deepEqual(writes, []);
  } finally { r.unmount(); }
});

test('Vision route defers automation disposal to its page lifecycle cleanup', () => {
  const {readFileSync} = require('node:fs');
  const ts = require('typescript');
  const route = new URL('../../app/vision/page.tsx', import.meta.url);
  const source = ts.createSourceFile('vision/page.tsx', readFileSync(route, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const calls: any[] = [];
  const visit = (node: any) => {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 'useVisionAutomation') calls.push(node);
    ts.forEachChild(node, visit);
  };
  visit(source);
  assert.equal(calls.length, 1, 'Vision must retain one route-owned automation controller');
  const options = calls[0].arguments[0];
  assert.ok(options && ts.isObjectLiteralExpression(options), 'Vision automation lifecycle options must be explicit');
  const disposal = options.properties.find((property: any) => ts.isPropertyAssignment(property) && property.name.getText(source) === 'disposeOnUnmount');
  assert.equal(disposal?.initializer.kind, ts.SyntaxKind.FalseKeyword,
    'disposeOnUnmount must stay explicitly false so StrictMode/BFCache unmount uses the deferred route-owned cleanup');
});
