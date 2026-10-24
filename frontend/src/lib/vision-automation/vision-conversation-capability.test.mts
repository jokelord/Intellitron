import test from 'node:test';
import assert from 'node:assert/strict';
import { hasVisionConversationCapability } from './use-vision-conversation-adapter.ts';
test('direct Vision route requires confirmed vision model and enabled server vision capability',()=>{
  assert.equal(hasVisionConversationCapability(undefined),false);
  assert.equal(hasVisionConversationCapability({model:{model_type:'text'},vision:{enabled:true}}),false);
  assert.equal(hasVisionConversationCapability({model:{model_type:'vision'},vision:{enabled:false}}),false);
  assert.equal(hasVisionConversationCapability({model:{model_type:'vision'},vision:{enabled:true}}),true);
});
