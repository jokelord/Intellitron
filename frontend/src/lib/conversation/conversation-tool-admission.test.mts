import test from 'node:test';
import assert from 'node:assert/strict';
import { rejectConversationToolAdmission, type ConversationToolAdmissionState } from './conversation-tool-admission.ts';
import { DEFAULT_SETTINGS, type ChatSettings } from '../message-types.ts';
import { reduceAgentToolSessionState, EMPTY_AGENT_TOOL_SESSION_STATE, type AgentBusinessToolCatalog } from '../agent-tool-configuration.ts';

const catalog: AgentBusinessToolCatalog = {schema_version:1,catalog_revision:'revision-2',tools:[{name:'read_file',order:0}],default_profile_tools:{manager_agent:['read_file'],basic_agent:[]}};
const config = {agent:{business_tool_catalog:catalog.tools,tool_catalog_revision:catalog.catalog_revision,tool_configuration_schema_version:1,default_profile_tools:catalog.default_profile_tools}};
function harness() {
  let tools=reduceAgentToolSessionState(EMPTY_AGENT_TOOL_SESSION_STATE,{type:'begin',sessionId:'session-1',requestId:'request-1',candidate:{schema_version:1,catalog_revision:'revision-1',manager_agent_tools:[],basic_agent_tools:[]}});
  let settings={...DEFAULT_SETTINGS,temperature:0},defaults:ChatSettings|null={...DEFAULT_SETTINGS};
  let currentCatalog:AgentBusinessToolCatalog|null=null;
  const notices:string[]=[];
  const state:ConversationToolAdmissionState={dispatchAgentToolState:action=>{tools=reduceAgentToolSessionState(tools,action);},
    setAgentToolCatalog:value=>{currentCatalog=typeof value==='function'?value(currentCatalog ?? catalog):value;},
    setSettings:value=>{settings=typeof value==='function'?value(settings):value;},
    setServerDefaults:value=>{defaults=typeof value==='function'?value(defaults):value;},
    unknownAgentToolNoticeShownRef:{current:false},setAgentToolNotice:value=>{if(typeof value==='string')notices.push(value);}};
  return {state,getTools:()=>tools,getSettings:()=>settings,getDefaults:()=>defaults,getCatalog:()=>currentCatalog,notices};
}
for(const code of ['tool_configuration_locked','invalid_tool_configuration'] as const){
  test(`${code} rejects only the captured request without refreshing catalog`,async()=>{
    const h=harness();let reads=0;
    const result=rejectConversationToolAdmission({state:h.state,sessionId:'session-1',requestId:'request-1',rejection:{code,message:'server detail'},isCurrent:()=>true,getConfig:async()=>{reads++;return config;}});
    await result.refresh;
    assert.equal(result.isToolConfigurationError,true);assert.notEqual(result.visibleMessage,'server detail');
    assert.equal(h.getTools().sessions['session-1'],undefined);assert.equal(reads,0);assert.equal(h.getCatalog(),null);
  });
}
test('catalog change refreshes defaults, preserves cached selections and reports unknown IDs once',async()=>{
  const h=harness();
  const options={state:h.state,sessionId:'session-1',requestId:'request-1',rejection:{code:'tool_catalog_changed',message:'changed'},isCurrent:()=>true,getConfig:async()=>config,getCachedSettings:()=>({managerAgentTools:[],basicAgentTools:['removed']})};
  const result=rejectConversationToolAdmission(options);await result.refresh;
  assert.equal(h.getCatalog()?.catalog_revision,'revision-2');
  assert.deepEqual(h.getSettings().managerAgentTools,[]);assert.deepEqual(h.getSettings().basicAgentTools,[]);
  assert.equal(h.getSettings().temperature,0);assert.deepEqual(h.getDefaults()?.managerAgentTools,[]);
  assert.equal(h.notices.length,1);assert.match(h.notices[0],/removed/);
  await rejectConversationToolAdmission(options).refresh;assert.equal(h.notices.length,1);
});
test('stale asynchronous catalog response cannot write the replacement owner',async()=>{
  const h=harness();let current=true;let resolve!:(value:typeof config)=>void;
  const result=rejectConversationToolAdmission({state:h.state,sessionId:'session-1',requestId:'request-1',rejection:{code:'tool_catalog_changed',message:'changed'},isCurrent:()=>current,getConfig:()=>new Promise(r=>{resolve=r;})});
  current=false;resolve(config);await result.refresh;
  assert.equal(h.getCatalog(),null);assert.equal(h.notices.length,0);assert.equal(h.getSettings().temperature,0);
});
test('ordinary request rejection keeps exact server text and does not read catalog',async()=>{
  const h=harness();const result=rejectConversationToolAdmission({state:h.state,sessionId:'session-1',requestId:'request-1',rejection:{code:'admission_busy',message:'exact server detail'},isCurrent:()=>true,getConfig:()=>{throw new Error('unexpected catalog read');}});
  await result.refresh;assert.equal(result.isToolConfigurationError,false);assert.equal(result.visibleMessage,'exact server detail');
});
