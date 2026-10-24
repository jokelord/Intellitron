import test from 'node:test';
import assert from 'node:assert/strict';
import { createConversationTurnMessages, snapshotConversationDraft } from './use-conversation-send.ts';
test('queued turn snapshots images rather than borrowing a future draft',()=>{
  const images=['first']; const draft=snapshotConversationDraft(' hello ',images); images[0]='future';
  assert.deepEqual(draft,{text:'hello',images:['first']});
});
test('common turn creation retains routing placeholder and structured Agent run identity',()=>{
  let id=0; const turn=createConversationTurnMessages({userContent:'question',chatSessionId:'chat-1',initialRouting:true,createId:()=>String(++id),createStepId:()=> 'dispatch-1',now:()=>new Date(0)});
  assert.equal(turn.userMessage.content,'question');assert.equal(turn.assistantMessage.chatResume?.sessionId,'chat-1');
  assert.deepEqual(turn.assistantMessage.agentSteps,[{id:'dispatch-1',step_type:'dispatch',dispatch_status:'running',dispatch_stage:'routing',dispatch_kind:'routing',display_slot:'before_body'}]);
  assert.equal(turn.agentRun.assistantMessageId,turn.assistantMessage.id);assert.equal(turn.agentRun.projectedAssistantMessageIds.size,0);
});
