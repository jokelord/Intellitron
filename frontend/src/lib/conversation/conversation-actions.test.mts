import test from 'node:test';
import assert from 'node:assert/strict';
import { retireConversationMode } from './conversation-actions.ts';
import { SessionHubLiveAttachmentRegistry } from '../session-hub-recovery.ts';

test('mode retirement aborts only captured subscriber and rejects stale ownership', async () => {
  const registry = new SessionHubLiveAttachmentRegistry();
  const old = registry.attach('old-session');
  const controller = new AbortController();
  const transport = { current: controller as AbortController | null };
  let resets=0;
  retireConversationMode({ registry, transportRef:transport, reset:()=>{resets++;transport.current=null;} });
  const replacement=registry.attach('new-session');
  assert.equal(controller.signal.aborted,true);assert.equal(registry.isCurrent(old),false);
  assert.equal(registry.isCurrent(replacement),true);assert.equal(resets,1);
});

test('recovered Agent Stop marks its captured run before detach and retains every stopped bubble', async () => {
  const { finalizeConversationStop } = await import('./conversation-actions.ts');
  const { createTextAgentSessionHandlers } = await import('../text-agent-session-handlers.ts');
  const { getStreamingTerminalBehavior } = await import('../answer-now-ui.ts');
  const run = {assistantMessageId:'root',projectedAssistantMessageIds:new Set(['projected']),executionAssistantMessageId:'execution',stopWasClicked:false,answerNowWasClicked:false};
  const active = {current:run as typeof run|null}, runRef = {current:run as typeof run|null};
  let messages:any[] = ['root','projected','execution'].map(id=>({id,role:'assistant',content:'',timestamp:new Date(),isStreaming:true,agentSteps:[{id:`${id}-dispatch`,step_type:'dispatch',dispatch_status:'running',partial:true}]}));
  let stopped:string|null=null, stoppedIds:string[]=[];
  const setMessages = (update:any) => {messages=typeof update==='function'?update(messages):update;};
  const handlers = createTextAgentSessionHandlers({
    activeRunRef:active,setMessages,setAgentSessionId:()=>{},resetStreamingState:()=>{},
    clearStoppedState:()=>{stopped=null;stoppedIds=[];},clearAnswerNowFlag:()=>{},clearStopFlag:()=>{},
    getTerminalBehavior:getStreamingTerminalBehavior,newlyCompactMarkedRef:{current:new Set()},defaultReasoningCollapsed:false,now:()=>new Date(),createId:()=> 'unused',
  },runRef);
  const state:any={
    activeAgentRunRef:active,stopWasClickedRef:{current:false},messagesRef:{current:messages},setMessages,
    setStoppedMessageId:(id:string|null)=>{stopped=id;},setStoppedTrackedMessageIds:(ids:string[])=>{stoppedIds=ids;},
    recoveryCoordinatorRef:{current:{detach:()=>{assert.equal(run.stopWasClicked,true,'detach must see Stop intent');handlers.onDetached();}}},
  };
  finalizeConversationStop(state,'agent');
  assert.equal(active.current,null,'real recovery detach clears the active run');
  assert.equal(stopped,'root');assert.deepEqual(stoppedIds,['root','projected','execution']);
  assert.equal(messages.every(message=>!message.isStreaming&&!message.agentSteps[0].partial),true);
  assert.equal(messages.find(message=>message.id==='execution').agentSteps[0].dispatch_status,'completed');
  assert.equal(getStreamingTerminalBehavior(run).resetStoppedState,false,'stopped bubble IDs drive the existing stopped presentation');
});
