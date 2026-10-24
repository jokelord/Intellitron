import test from 'node:test';
import assert from 'node:assert/strict';
import { runChatConversationTransport } from './conversation-transport.ts';
import { DEFAULT_SETTINGS } from '../message-types.ts';

test('shared Chat transport keeps explicit wire fields and applies deltas only to current attachment', async () => {
  let current = true;
  let captured: any[] = [];
  let messages: any[] = [{ id: 'assistant', role: 'assistant', content: '', timestamp: new Date(), isStreaming: true }];
  const setMessages = (update: any) => { messages = typeof update === 'function' ? update(messages) : update; };
  const turn = { pageMode:'text' as const, requestId:'request-123', controller:new AbortController(), interactionMode:'chat' as const,
    sessionId:'chat-1', historyMessages:[], userContent:'hello', agentText:'hello', preserveFullHistory:false, hasUserText:true,
    userMessage:{id:'user'}, assistantMessage:{id:'assistant'}, agentRun:{assistantMessageId:'assistant'} } as any;
  await runChatConversationTransport({turn, settings:{...DEFAULT_SETTINGS,systemPrompt:''}, maxContextLength:1024, runtimeContext:null,
    localMemoryRequestsRef:{current:new Map()}, chatHistoryRequestsRef:{current:new Map()}, localMemoryEnabled:false,
    setMessages, newlyCompactMarkedRef:{current:new Set()}, isCurrent:()=>current,
    onDone:()=>{},onError:()=>{},client:{chatStream:async (...args:any[])=>{captured=args;args[2]('first');current=false;args[2]('stale');}},
  });
  assert.deepEqual(captured[0], [{role:'user',content:'hello'}]);
  assert.equal(captured[1].request_id,'request-123');assert.equal(captured[1].session_id,'chat-1');
  assert.equal(captured[1].enable_local_memory,false); assert.equal('vision_automation' in captured[1],false);
  assert.equal(messages[0].content,'first');
});

test('shared Agent transport retains multimodal messages and projects with the canonical handler', async () => {
  const { runAgentConversationTransport } = await import('./conversation-transport.ts');
  const { createConversationTurnMessages } = await import('./use-conversation-send.ts');
  const userContent=[{type:'image_url',image_url:{url:'data:image/png;base64,AA'}},{type:'text',text:'inspect'}];
  const created=createConversationTurnMessages({userContent,initialRouting:false});
  let messages=[created.userMessage,created.assistantMessage];let captured:any;
  const runRef={current:created.agentRun};
  const setMessages=(update:any)=>{messages=typeof update==='function'?update(messages):update;};
  const environment={activeRunRef:runRef,setMessages,setAgentSessionId:()=>{},resetStreamingState:()=>{},clearStoppedState:()=>{},clearAnswerNowFlag:()=>{},clearStopFlag:()=>{},getTerminalBehavior:()=>({suppressErrorMessage:false,resetStreamingState:true,resetStoppedState:true,clearAnswerNowFlag:true}),newlyCompactMarkedRef:{current:new Set<string>()},defaultReasoningCollapsed:true,now:()=>new Date(0),createId:()=> 'generated'};
  await runAgentConversationTransport({turn:{pageMode:'vision',requestId:'image-request-1',controller:new AbortController(),interactionMode:'agent',sessionId:'canonical-agent',historyMessages:[],userContent,agentText:'inspect',images:['data:image/png;base64,AA'],preserveFullHistory:false,hasUserText:true,...created},settings:DEFAULT_SETTINGS,maxContextLength:1024,localMemoryRequestsRef:{current:new Map()},chatHistoryRequestsRef:{current:new Map()},localMemoryEnabled:false,isCurrent:()=>true,environment,onDone:()=>{},onError:()=>{},client:{agentChatStream:async (...args:any[])=>{captured=args[0];args[1]({step_type:'thought',content:'thinking',partial:true});args[2]('answer');}}});
  assert.deepEqual(captured.context_messages,[{role:'user',content:userContent}]);
  assert.deepEqual(captured.images,['data:image/png;base64,AA']);assert.equal(captured.message,'inspect');
  assert.equal(captured.session_id,'canonical-agent');assert.equal('vision_automation' in captured,false);
  assert.equal(messages[1].content,'answer');assert.equal(messages[1].agentSteps?.[0].content,'thinking');
});

test('aborted UI payloads are ignored while stable lifecycle still releases request bookkeeping', async () => {
  const controller=new AbortController(), local={current:new Map<string,boolean>()}, history={current:new Map<string,string|undefined>()};
  const { AutomationTerminalGate } = await import('../vision-automation/page-integration.ts');
  const gate = new AutomationTerminalGate('stop-request-1');
  const sibling = new AutomationTerminalGate('sibling-request');
  let content='', terminals=0;
  await runChatConversationTransport({turn:{pageMode:'vision',requestId:'stop-request-1',controller,interactionMode:'chat',sessionId:'chat-1',historyMessages:[],userContent:'hello',agentText:'hello',preserveFullHistory:false,hasUserText:true,userMessage:{id:'user'},assistantMessage:{id:'assistant'},agentRun:{assistantMessageId:'assistant'}} as any,
    settings:{...DEFAULT_SETTINGS,systemPrompt:''},maxContextLength:1024,runtimeContext:null,localMemoryRequestsRef:local,chatHistoryRequestsRef:history,localMemoryEnabled:false,isCurrent:()=>true,newlyCompactMarkedRef:{current:new Set()},setMessages:update=>{const next=(update as Function)([{id:'assistant',role:'assistant',content}]);content=next[0].content;},onDone:()=>{throw new Error('late candidate must not reset UI');},onError:()=>{},lifecycle:{onTransportEof:async()=>{
      const status=await gate.pollForTerminal(async()=>({requestId:'stop-request-1',status:'unknown'}));
      assert.equal(status.kind,'failed');
    },onStableTerminal:result=>{terminals++;gate.markStableTerminal(result);}},client:{chatStream:async(...args:any[])=>{
      args[2]('before');controller.abort();args[2]('late');args[3]();
      gate.markCandidateComplete();
      await args[7].onTransportEof();
      assert.equal(gate.modelBusy,true);assert.equal(sibling.modelBusy,true);
      await args[7].onStableTerminal({requestId:'stop-request-1',status:'terminal'});
    }}});
  assert.equal(content,'before');assert.equal(terminals,1);assert.equal(local.current.size,0);assert.equal(history.current.size,0);
  assert.equal(gate.modelBusy,false);assert.equal(sibling.modelBusy,true);
});

for (const provided of [false, true]) test(`Agent acceptance updates ${provided ? 'provided' : 'default'} handlers before lifecycle exactly once`, async () => {
  const { runAgentConversationTransport } = await import('./conversation-transport.ts');
  const { createTextAgentSessionHandlers } = await import('../text-agent-session-handlers.ts');
  const order: string[] = [];
  const environment: any = {
    activeRunRef: { current: null }, setMessages: () => {}, setAgentSessionId: () => {},
    resetStreamingState: () => {}, clearStoppedState: () => {}, clearAnswerNowFlag: () => {}, clearStopFlag: () => {},
    getTerminalBehavior: () => ({}), newlyCompactMarkedRef: { current: new Set() }, defaultReasoningCollapsed: false,
    onAgentToolConfigurationAccepted: () => order.push('accepted-lock'),
  };
  const handlers = createTextAgentSessionHandlers(environment, environment.activeRunRef);
  let current = true;
  const event = { type: 'request_accepted', requestId: 'accepted-1', sessionId: 'agent-1', toolConfiguration: {schema_version:1,catalog_revision:'catalog',manager_agent_tools:[],basic_agent_tools:[],locked:true,fingerprint:'lock'} };
  await runAgentConversationTransport({
    turn: { pageMode: 'text', requestId: 'accepted-1', controller: new AbortController(), interactionMode: 'agent', sessionId: 'agent-1', historyMessages: [], userContent: 'hi', agentText: 'hi', preserveFullHistory: false, hasUserText: true, userMessage: {id:'u'}, assistantMessage: {id:'a'}, agentRun: {assistantMessageId:'a'} } as any,
    settings: DEFAULT_SETTINGS, maxContextLength: 1024, localMemoryRequestsRef: {current:new Map()}, chatHistoryRequestsRef: {current:new Map()}, localMemoryEnabled:false,
    environment, ...(provided ? {handlers} : {}), isCurrent: () => current, onDone: () => {}, onError: () => {},
    lifecycle: {onRequestAccepted: () => {order.push('lifecycle');}},
    client: {agentChatStream: async (...args: any[]) => {
      await args[8].onRequestAccepted(event);
      current = false;
      await args[8].onRequestAccepted(event);
    }},
  });
  assert.deepEqual(order, ['accepted-lock', 'lifecycle']);
});
