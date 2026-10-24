import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import test, {type TestContext} from 'node:test';
const require = createRequire(import.meta.url);
const {installTask6TypeScriptHook} = require('../vision-automation/react-component-renderer.cjs');
installTask6TypeScriptHook(fileURLToPath(new URL('../../../', import.meta.url)));
const React = require('react');
const {HostElement,createRouteRenderer} = require('./route-renderer.cjs');
const Module = require('node:module');
const turn = () => new Promise<void>(resolve => setImmediate(resolve));
let currentRouter: any;
const oldLoad = Module._load;
let TextRoute: any, VisionRoute: any;
try {
  Module._load = function(request: string, ...args: unknown[]) {
    if(request === 'next/navigation') return {useRouter: () => currentRouter};
    return oldLoad.call(this,request,...args);
  };
  TextRoute = require('../../app/page.tsx').default;
  VisionRoute = require('../../app/vision/page.tsx').default;
} finally {Module._load = oldLoad;}

type Mode = 'text'|'vision';
function routeHarness(t: TestContext, pageMode: Mode, agentMode: boolean, modelType: Mode = pageMode, options: {recover?: boolean; recoveryFailure?: '404'|'transient'; cycleEnabled?: boolean; cycleProgram?: {steps:any[];scheduledMessages:any[]}; missingRecoveryRequestId?: boolean; delayAcceptance?: boolean; abortAware?: boolean; stopPayloadError?: boolean; stopRejected?: boolean; agentIdleTimeoutSeconds?: number} = {}) {
  const storage = new Map<string,string>();
  const localStorage = {getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string,value: string) => storage.set(key,value), removeItem: (key: string) => storage.delete(key)};
  // Literal cache keys exercise the existing presence-precedence contract.
  storage.set('llm-chat-settings',JSON.stringify({agentMode,enterToSend:true,enableThinking:false,enableLocalMemory:false,systemPrompt:'Route system',managerAgentTools:['read_file'],basicAgentTools:['read_file'],temperature:0,topP:.9,topK:20,repetitionPenalty:1,maxTokens:512}));
  if (options.cycleProgram) storage.set('cycle-editor-state', JSON.stringify({...options.cycleProgram,shareContext:true,showAgentSteps:false}));
  const recoveredSessionId = agentMode ? '11111111-1111-4111-8111-111111111111' : 'chat-recovered';
  const recoveredRequestId = 'recovered-current-request';
  if (options.recover) storage.set(`llm-session-hub-bookmark:${pageMode}`, JSON.stringify({schemaVersion:1,sessionId:recoveredSessionId,pageMode,interactionMode:agentMode?'agent':'chat',createdAt:1}));
  const calls: {path:string,body:any,signal?:AbortSignal|null}[] = [];
  const accepted: any[] = [], statuses: string[] = [], navigation: string[] = [];
  let mediaCalls = 0;
  const config = {
    model:{model_name:'Route Qwen',model_type:modelType,max_context_length:4096},
    inference:{temperature:.6,top_p:.95,top_k:50,repetition_penalty:1,max_tokens:1024},
    agent:{enabled:agentMode,entry_agent_name:'coordinator_1',tool_configuration_schema_version:1,tool_catalog_revision:'route-tools',business_tool_catalog:[{name:'read_file',order:0}],default_profile_tools:{manager_agent:['read_file'],basic_agent:['read_file']}},
    cycle:{enabled:options.cycleEnabled ?? false,default_sequence:[],default_scheduled_messages:[]},
    runtime_prompts:{enable_runtime_time_context:false},
    frontend:{chat_stream_idle_timeout_seconds:30,agent_stream_idle_timeout_seconds:options.agentIdleTimeoutSeconds ?? 30,cycle_stream_idle_timeout_seconds:30},
    webUI:{components:{page_header:{display:true,text_title:'Text route',vision_title:'Vision route'},message_templates:{items:['Route template']}},skin:{id:'default'}},
    ...(modelType === 'vision' ? {vision:{enabled:true,max_image_size:10485760,supported_formats:'png,jpg',max_image_tokens:1000,camera_max_dimension:1024,capture_dimension:1280}} : {}),
  };
  const originalGlobals = new Map<string,PropertyDescriptor|undefined>();
  const setGlobal = (key:string,value:unknown) => {originalGlobals.set(key,Object.getOwnPropertyDescriptor(globalThis,key));Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});};
  const pageDocument = Object.assign(new EventTarget(), {visibilityState:'visible',activeElement:null,body:new HostElement('body'),documentElement:new HostElement('html'),hasFocus:()=>true,querySelector:()=>null,querySelectorAll:()=>[],createElement:(type:string)=>new HostElement(type)});
  const timers = new Set<ReturnType<typeof setTimeout>>();
  const intervals = new Map<number,() => void>();
  let intervalId = 0;
  const browser = Object.assign(new EventTarget(), {localStorage,sessionStorage:localStorage,location:{protocol:'http:',hostname:'localhost',port:'3000',href:'http://localhost:3000'+(pageMode==='vision'?'/vision':'/'),reload() {}},innerWidth:1024,innerHeight:768,matchMedia:()=>({matches:false,addEventListener() {},removeEventListener() {}}),setInterval:(callback:()=>void)=>{intervals.set(++intervalId,callback);return intervalId;},clearInterval:(id:number)=>intervals.delete(id),setTimeout:(callback:()=>void,ms:number)=>{const timer=setTimeout(callback,ms);timers.add(timer);return timer;},clearTimeout:(timer:ReturnType<typeof setTimeout>)=>{clearTimeout(timer);timers.delete(timer);},confirm:()=>false});
  setGlobal('window',browser);setGlobal('document',pageDocument);setGlobal('localStorage',localStorage);setGlobal('sessionStorage',localStorage);
  setGlobal('navigator',{mediaDevices:{getUserMedia:async()=>{mediaCalls++;throw new Error('Route test must not access media');},enumerateDevices:async()=>{mediaCalls++;return [];}}});
  setGlobal('HTMLElement',HostElement);setGlobal('Element',HostElement);setGlobal('getComputedStyle',(node:any)=>({overflowY:node.style.overflowY??'visible'}));
  setGlobal('requestAnimationFrame',(callback:()=>void)=>{callback();return 1;});setGlobal('cancelAnimationFrame',()=>{});
  currentRouter = {prefetch:(path:string)=>navigation.push(`prefetch:${path}`),replace:(path:string)=>navigation.push(`replace:${path}`),push:(path:string)=>navigation.push(`push:${path}`)};
  const json = (value:unknown) => new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
  const sessionSequences=new Map<string,number>();
  let requestCount = 0;
  let releaseAcceptance: (() => void) | undefined;
  let holdResponses = false;
  const held: {controller:ReadableStreamDefaultController,body:any,agent:boolean,index:number,nextSequence:number}[] = [];
  const deleted: string[] = [];
  let cycleStream: ReadableStreamDefaultController | undefined;
  let cycleRunId: string | undefined;
  let cycleSequence = 0;
  const emitCycle = (event:any) => cycleStream!.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({...event,cycle_run_id:cycleRunId,event_sequence:++cycleSequence,request_fingerprint:'cycle-fingerprint'})}\n\n`));
  let recoveryFetches=0;
  let recoverySignal:AbortSignal|null|undefined;
  let recoveryStream:ReadableStreamDefaultController|undefined;
  let recoverySequence=0;
  const emitRecovered=(payload:any)=>{if(options.missingRecoveryRequestId) delete payload.request_id;recoveryStream!.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({...payload,event_sequence:++recoverySequence})}\n\n`));};
  const responseFor = (body:any,agent:boolean,signal?:AbortSignal|null) => {
    const index = ++requestCount;
    const sequenceBase=sessionSequences.get(body.session_id)??0;
    const requestAccepted = {event_sequence:1,type:'request_accepted',request_id:body.request_id,session_id:body.session_id,status:'accepted',...(agent&&body.tool_configuration?{tool_configuration:{...body.tool_configuration,locked:true,fingerprint:'route-fingerprint'}}:{})};
    accepted.push(requestAccepted);
    const answer = `Visible answer ${index}`;
    const payloads = agent ? [requestAccepted,{event_sequence:2,type:'timeline_event',event:{kind:'turn_started',session_id:body.session_id,turn_index:index,user_content:body.message,images:[],mode:'tool_calling'}},{event_sequence:3,type:'content',content:answer},{event_sequence:4,type:'done',session_id:body.session_id}] : [requestAccepted,{event_sequence:2,type:'timeline_event',event:{kind:'message_delta',source:'chat',display:'chat_bubble',text:answer,partial:true}},{event_sequence:3,type:'done',session_id:body.session_id}];
    for(const payload of payloads)payload.event_sequence+=sequenceBase;
    sessionSequences.set(body.session_id,sequenceBase+payloads.length);
    return new Response(new ReadableStream({start(controller){
      if(holdResponses){
        const initial=payloads.slice(0,-1);
        for(const payload of initial){
          const output=payload.type==='content'?{...payload,content:`Partial answer ${index}`}:payload.type==='timeline_event'&&payload.event.kind==='message_delta'?{...payload,event:{...payload.event,text:`Partial answer ${index}`}}:payload;
          controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(output)}\n\n`));
        }
        held.push({controller,body,agent,index,nextSequence:sequenceBase+payloads.length});
        if(options.abortAware) signal?.addEventListener('abort',()=>controller.error(signal.reason ?? new DOMException('Aborted','AbortError')),{once:true});
      }else{for(const payload of payloads)controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(payload)}\n\n`));controller.close();}
    }}),{status:200,headers:{'Content-Type':'text/event-stream'}});
  };
  setGlobal('fetch',async(input:string|URL|Request,init?:RequestInit)=>{
    const path=new URL(String(input),'http://localhost').pathname;
    if(path==='/config')return json(config);
    if(path==='/health')return json({status:'ok'});
    if(path==='/v1/models')return json({data:[{id:'Route Qwen'}]});
    if(/^\/v1\/session\/[^/]+\/events$/.test(path)){
      recoveryFetches++; recoverySignal=init?.signal;
      if(recoveryFetches===1 && options.recoveryFailure==='404') return new Response('{}',{status:404});
      if(recoveryFetches===1 && options.recoveryFailure==='transient') throw new Error('Recovery transport offline');
      recoverySequence=0;
      return new Response(new ReadableStream({start(controller){
        recoveryStream=controller;
        for(const [index,requestId] of [[1,'recovered-completed-request'],[2,recoveredRequestId]] as const){
          if (!options.missingRecoveryRequestId) emitRecovered({type:'request_accepted',request_id:requestId,session_id:recoveredSessionId,status:'accepted'});
          emitRecovered({type:'timeline_event',event:{kind:'turn_started',source:'chat',session_id:recoveredSessionId,turn_index:index,user_content:`Recovered task ${index}`,images:[],mode:'tool_calling'}});
          emitRecovered(agentMode?{type:'content',content:`Recovered answer ${index}`}:{type:'timeline_event',event:{kind:'message_delta',source:'chat',display:'chat_bubble',text:`Recovered answer ${index}`,partial:true}});
          if(index===1)emitRecovered({type:'done',request_id:requestId,session_id:recoveredSessionId});
        }
        sessionSequences.set(recoveredSessionId,recoverySequence);
      }}),{status:200,headers:{'Content-Type':'text/event-stream'}});
    }

    if (path === '/v1/cycle/start' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body));
      calls.push({path,body,signal:init.signal});
      cycleRunId = body.cycle_run_id;
      return new Response(new ReadableStream({start(controller) {cycleStream=controller;emitCycle({type:'cycle_started'});}}), {status:200,headers:{'Content-Type':'text/event-stream'}});
    }
    if (path === '/v1/cycle/stop' && init?.method === 'POST') {
      calls.push({path,body:JSON.parse(String(init.body)),signal:init.signal});
      emitCycle({type:'cycle_stopped'});cycleStream!.close();return json({status:'stopped'});
    }
    if(path.startsWith('/v1/request-status/')){const id=decodeURIComponent(path.split('/').at(-1)!);statuses.push(id);return json({request_id:id,status:'terminal'});}
    if(init?.method==='DELETE'&&path.startsWith('/v1/session/')){deleted.push(path);return json({status:'deleted'});}
    if(init?.method==='POST'){const body=JSON.parse(String(init.body));calls.push({path,body,signal:init.signal});if(path==='/v1/agent/chat'||path==='/v1/chat/completions') {
      if (options.delayAcceptance) return await new Promise<Response>(resolve => {releaseAcceptance=()=>resolve(responseFor(body,path==='/v1/agent/chat',init.signal));});
      return responseFor(body,path==='/v1/agent/chat',init.signal);
    }if(path==='/v1/agent/stop'||path==='/v1/chat/stop'){
      if(options.stopPayloadError){const entry=held[0];entry.controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({event_sequence:entry.nextSequence++,type:'error',error:'Agent execution interrupted'})}\n\n`));await turn();}
      return options.stopRejected ? new Response('{}',{status:502}) : json({status:'stopped'});
    }}
    throw new Error(`Unexpected route network I/O: ${path}`);
  });
  const renderer = createRouteRenderer(React.createElement(pageMode==='text'?TextRoute:VisionRoute));
  t.after(async()=>{if(releaseAcceptance){const release=releaseAcceptance;releaseAcceptance=undefined;release();for(let i=0;i<8;i++){await turn();renderer.flush();}}renderer.unmount();await turn();for(const timer of timers)clearTimeout(timer);for(const[key,descriptor]of originalGlobals){if(descriptor)Object.defineProperty(globalThis,key,descriptor);else Reflect.deleteProperty(globalThis,key);}});
  const settle = async()=>{for(let i=0;i<8;i++){await turn();renderer.flush();}};
  const text = ()=>renderer.container.children.map(renderer.text).join('');
  const submit = async(value:string)=>{const input=renderer.findOne((node:any)=>node.type==='textarea'&&String(node.props.className).includes('chat-input-textarea'));renderer.act(()=>input.props.onChange({target:{value,selectionStart:value.length}}));renderer.click(renderer.findOne((node:any)=>node.type==='button'&&node.props['aria-label']==='发送'));await settle();};
  return {renderer,settle,submit,text,calls,recoveredSessionId,recoveredRequestId,
    restore:async()=>{renderer.click(renderer.findOne((node:any)=>node.type==='button'&&renderer.text(node)==='恢复运行中的任务'));await settle();},
    changeMode:async()=>{renderer.click(renderer.findOne((node:any)=>node.type==='button'&&node.props['aria-label']==='设置'));renderer.click(renderer.findOne((node:any)=>node.type==='button'&&renderer.text(node)==='高级设置'));const toggle=renderer.findOne((node:any)=>node.type==='input'&&node.props.type==='checkbox');renderer.act(()=>toggle.props.onChange({target:{checked:!agentMode}}));renderer.click(renderer.findOne((node:any)=>node.type==='button'&&renderer.text(node)==='确认'));await settle();},
    releaseAcceptance:()=>{assert.ok(releaseAcceptance);const release=releaseAcceptance;releaseAcceptance=undefined;release();},
    recoveryFetches:()=>recoveryFetches,recoverySignal:()=>recoverySignal,
    finishRecovery:(content:string)=>{emitRecovered(agentMode?{type:'content',content}:{type:'timeline_event',event:{kind:'message_delta',source:'chat',display:'chat_bubble',text:content,partial:true}});emitRecovered({type:'done',request_id:recoveredRequestId,session_id:recoveredSessionId});recoveryStream!.close();},
    emitRecoveredDomain:emitRecovered,
    emitHistoricalTerminal:()=>emitRecovered({type:'done',request_id:'recovered-completed-request',session_id:recoveredSessionId}),
    pageHide:()=>browser.dispatchEvent(new Event('pagehide')),
    unmount:()=>renderer.unmount(),
    openCycle:async()=>{
      renderer.click(renderer.findOne((node:any)=>node.type==='button'&&node.props['aria-label']==='展开输入操作'));
      renderer.click(renderer.findOne((node:any)=>node.type==='button'&&renderer.text(node)==='编程循环'));
      await settle();
    },
    startCycle:async()=>{renderer.click(renderer.findOne((node:any)=>node.type==='button'&&renderer.text(node)==='Start'));await settle();},
    advanceScheduledCycle:()=>emitCycle({type:'step_begin',track:'scheduled',step_type:'message',schedule_id:'schedule-1',occurrence_index:1,detail:{message:'Scheduled delivery'}}),
    accepted,statuses,storage,navigation,deleted,mediaCalls:()=>mediaCalls,
    holdResponses(){holdResponses=true;},
    failHeld(index:number,error:Error){held[index].controller.error(error);},
    emitHeld(index:number,payload:any){const entry=held[index];entry.controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({...payload,event_sequence:entry.nextSequence++})}\n\n`));},
    finishHeld(index:number,content:string){const entry=held[index];assert.ok(entry);const payloads=[entry.agent?{event_sequence:entry.nextSequence,type:'content',content}:{event_sequence:entry.nextSequence,type:'timeline_event',event:{kind:'message_delta',source:'chat',display:'chat_bubble',text:content,partial:true}},{event_sequence:entry.nextSequence+1,type:'done',session_id:entry.body.session_id}];for(const payload of payloads)entry.controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(payload)}\n\n`));entry.controller.close();},
  };
}

for(const pageMode of ['text','vision'] as const)for(const agentMode of [false,true]){
  test(`${pageMode} route ${agentMode?'Agent':'Chat'} submits through actual client and projects one answer`,async(t)=>{
    const h=routeHarness(t,pageMode,agentMode);await h.settle();
    await h.submit('First request');
    assert.equal(h.calls.length,1,h.text());
    const first=h.calls[0];assert.equal(first.path,agentMode?'/v1/agent/chat':'/v1/chat/completions');
    assert.match(first.body.request_id,/^[A-Za-z0-9][A-Za-z0-9._~:-]{7,127}$/);
    if(agentMode)assert.match(first.body.session_id,/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
    else assert.match(first.body.session_id,/^chat-/);
    assert.equal(h.accepted[0].request_id,first.body.request_id);assert.equal(h.accepted[0].session_id,first.body.session_id);
    assert.equal(h.text().split('First request').length-1,1,h.text());assert.equal(h.text().split('Visible answer 1').length-1,1,h.text());
    assert.equal(first.body.enable_thinking,false);assert.equal(first.body.enable_local_memory,false);assert.equal('vision_automation'in first.body,false);
    if(agentMode){assert.equal(first.body.message,'First request');assert.deepEqual(first.body.context_messages,[{role:'user',content:'First request'}]);assert.deepEqual(first.body.tool_configuration,{schema_version:1,catalog_revision:'route-tools',manager_agent_tools:['read_file'],basic_agent_tools:['read_file']});}
    else{assert.deepEqual(first.body.messages,[{role:'system',content:'Route system'},{role:'user',content:'First request'}]);assert.equal(first.body.temperature,0);}
    await h.submit('Second request');assert.equal(h.calls.length,2);assert.equal(h.calls[1].body.session_id,first.body.session_id);assert.notEqual(h.calls[1].body.request_id,first.body.request_id);
    if(agentMode)assert.equal('tool_configuration'in h.calls[1].body,false);
    assert.equal(h.text().split('Visible answer 2').length-1,1,h.text());assert.equal(h.mediaCalls(),0);
    const bookmark=JSON.parse(h.storage.get(`llm-session-hub-bookmark:${pageMode}`)!);
    assert.equal(bookmark.pageMode,pageMode);assert.equal(bookmark.interactionMode,agentMode?'agent':'chat');assert.equal(bookmark.sessionId,first.body.session_id);
    assert.equal(h.storage.has(`llm-session-hub-bookmark:${pageMode==='text'?'vision':'text'}`),false);
  });
}

for(const agentMode of [false,true])test(`direct Vision with text backend keeps ordinary ${agentMode?'Agent':'Chat'} input and has no media controls`,async(t)=>{
  const h=routeHarness(t,'vision',agentMode,'text');await h.settle();
  h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&node.props['aria-label']==='展开输入操作'));
  assert.equal(h.renderer.findAll((node:any)=>node.type==='input'&&node.props.type==='file').length,0);
  assert.equal(h.renderer.findAll((node:any)=>node.type==='button'&&(/上传图片|拍照|摄像头/.test(String(node.props.title??node.props['aria-label']??'')))&&!node.props.disabled).length,0);
  await h.submit('Text on direct Vision');
  assert.equal(h.calls.length,1,h.text());assert.equal(h.mediaCalls(),0);
  assert.equal(h.text().split('Visible answer 1').length-1,1,h.text());
  assert.equal('images'in h.calls[0].body,false);
});

for(const pageMode of ['text','vision'] as const)for(const agentMode of [false,true])test(`${pageMode} ${agentMode?'Agent':'Chat'} Stop targets its accepted active request without stop-all`,async(t)=>{
  const h=routeHarness(t,pageMode,agentMode);await h.settle();h.holdResponses();await h.submit('Long request');
  const body=h.calls[0].body;
  assert.match(h.text(),/Partial answer 1/);
  h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&node.props.title==='停止'));await h.settle();
  const stop=h.calls.find(call=>call.path===(agentMode?'/v1/agent/stop':'/v1/chat/stop'));
  assert.ok(stop,h.text());
  if(agentMode)assert.equal(stop.body.session_id,body.session_id);else assert.deepEqual(stop.body,{request_id:body.request_id});
  assert.equal(h.calls.some(call=>call.path.includes('stop-all')),false);
  assert.equal(h.calls[0].signal?.aborted,true,'Stop must close its captured browser transport');
  h.finishHeld(0,'Late answer after stop');await h.settle();
  assert.doesNotMatch(h.text(),/Late answer after stop/);
  await h.submit('After stop request');
  const inference=h.calls.filter(call=>call.path==='/v1/agent/chat'||call.path==='/v1/chat/completions');
  assert.equal(inference.length,2,'stable old terminal must not strand the next ordinary admission');
  assert.equal(inference[1].body.session_id,body.session_id);
  if(agentMode)assert.equal('tool_configuration'in inference[1].body,false);
  h.finishHeld(1,' completed after stop');await h.settle();
  assert.match(h.text(),/Partial answer 2 completed after stop/);
  assert.equal(h.renderer.findAll((node:any)=>node.type==='button'&&node.props.title==='停止').length,0);
});

for(const pageMode of ['text','vision'] as const)for(const agentMode of [false,true])test(`${pageMode} ${agentMode?'Agent':'Chat'} Clear rejects old stream deltas and terminal while a replacement is active`,async(t)=>{
  const h=routeHarness(t,pageMode,agentMode);await h.settle();h.holdResponses();await h.submit('Old task');
  const old=h.calls[0].body;
  h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&node.props['aria-label']==='清空对话'));await h.settle();
  assert.doesNotMatch(h.text(),/Old task|Partial answer 1/);
  await h.submit('Replacement task');
  const next=h.calls.find((call,index)=>index>0&&(call.path==='/v1/agent/chat'||call.path==='/v1/chat/completions'))!.body;
  assert.notEqual(next.session_id,old.session_id);assert.notEqual(next.request_id,old.request_id);
  h.finishHeld(0,'Late old delta');await h.settle();
  assert.doesNotMatch(h.text(),/Late old delta|Old task/);
  assert.equal(h.renderer.findAll((node:any)=>node.type==='button'&&node.props.title==='停止').length,1,'old terminal must not release replacement busy ownership');
  h.finishHeld(1,' Current tail');await h.settle();
  assert.equal(h.text().split('Replacement task').length-1,1,h.text());assert.match(h.text(),/Partial answer 2 Current tail/);
});

for(const pageMode of ['text','vision'] as const)for(const agentMode of [false,true]) {
  test(`${pageMode} recovered ${agentMode?'Agent':'Chat'} Stop targets the current journal request and retires its subscriber`,async t=>{
    const h=routeHarness(t,pageMode,agentMode,pageMode,{recover:true});await h.settle();await h.restore();
    assert.match(h.text(),/Recovered answer 2/);
    h.emitHistoricalTerminal();await h.settle();
    assert.equal(h.renderer.findAll((node:any)=>node.type==='button'&&node.props.title==='停止').length,1,'historical done must not unlock current recovery');
    h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&node.props.title==='停止'));await h.settle();
    const stop=h.calls.find(call=>call.path===(agentMode?'/v1/agent/stop':'/v1/chat/stop'));
    assert.ok(stop,h.text());
    if(agentMode){assert.equal(stop.body.session_id,h.recoveredSessionId);assert.equal(stop.body.expected_request_id,h.recoveredRequestId);}
    else assert.equal(stop.body.request_id,h.recoveredRequestId);
    assert.equal(h.recoverySignal()?.aborted,true);
    h.holdResponses();await h.submit('New after recovery stop');
    h.finishRecovery('Stale recovered tail');await h.settle();
    assert.doesNotMatch(h.text(),/Stale recovered tail/);
    assert.equal(h.renderer.findAll((node:any)=>node.type==='button'&&node.props.title==='停止').length,1);
    h.finishHeld(0,' replacement done');await h.settle();
  });
  test(`${pageMode} recovered ${agentMode?'Agent':'Chat'} mode switch preserves producer and blocks late replay writes`,async t=>{
    const h=routeHarness(t,pageMode,agentMode,pageMode,{recover:true});await h.settle();await h.restore();
    const bookmark=h.storage.get(`llm-session-hub-bookmark:${pageMode}`);
    await h.changeMode();
    assert.equal(h.recoverySignal()?.aborted,true);
    assert.equal(h.storage.get(`llm-session-hub-bookmark:${pageMode}`),bookmark);
    assert.equal(h.calls.some(call=>call.path.includes('/stop')),false);assert.equal(h.deleted.length,0);
    h.holdResponses();await h.submit('Replacement after mode switch');
    h.finishRecovery('Late recovery overwrite');await h.settle();
    assert.doesNotMatch(h.text(),/Late recovery overwrite/);assert.match(h.text(),/Replacement after mode switch/);
    h.finishHeld(0,' done');await h.settle();
  });
  for(const failure of ['404','transient'] as const)test(`${pageMode} ${agentMode?'Agent':'Chat'} recovery ${failure} exposes settlement and preserves retry ownership`,async t=>{
    const h=routeHarness(t,pageMode,agentMode,pageMode,{recover:true,recoveryFailure:failure});await h.settle();await h.restore();
    assert.match(h.text(),failure==='404'?/Saved session has expired/:/Recovery transport offline/);
    assert.equal(h.renderer.findAll((node:any)=>node.type==='button'&&h.renderer.text(node)==='恢复运行中的任务').length,1);
    if(failure==='404'){
      h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&h.renderer.text(node)==='Dismiss'));await h.settle();
      assert.equal(h.storage.has(`llm-session-hub-bookmark:${pageMode}`),false);assert.equal(h.deleted.length,0);
    }else{
      assert.ok(h.storage.get(`llm-session-hub-bookmark:${pageMode}`));
      h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&h.renderer.text(node)==='Retry'));await h.settle();
      assert.equal(h.recoveryFetches(),2);assert.match(h.text(),/Recovered answer 2/);
    }
  });
}

const scheduledProgram = { steps: [], scheduledMessages: [{id:'schedule-1',time:'23:59:59',recurrence:'once',day_count:1,message:'Scheduled delivery',photo:[]}] };
for (const agentMode of [false, true]) test(`direct Vision text backend retains templates and plain scheduled Cycle in ${agentMode?'Agent':'Chat'}`, async t => {
  const h = routeHarness(t, 'vision', agentMode, 'text', {cycleEnabled:true,cycleProgram:scheduledProgram});
  await h.settle();await h.openCycle();
  assert.equal(h.renderer.findAll((node:any)=>node.type==='input'&&node.props.type==='file').length,0);
  assert.equal(h.renderer.findAll((node:any)=>node.type==='input'&&node.props.placeholder==='e.g. 0, 1').length,0);
  h.renderer.click(h.renderer.findOne((node:any)=>node.props['data-testid']==='message-template-trigger'));
  h.renderer.click(h.renderer.findOne((node:any)=>node.props['data-testid']==='message-template-item-0'));
  assert.equal(h.renderer.findOne((node:any)=>node.type==='textarea'&&String(node.props.className).includes('chat-input-textarea')).props.value,'Route template');
  await h.startCycle();
  const start = h.calls.find(call=>call.path==='/v1/cycle/start');assert.ok(start,h.text());
  assert.deepEqual(start.body.scheduled_messages,scheduledProgram.scheduledMessages);
  assert.equal('pageMode' in start.body,false);assert.equal(h.mediaCalls(),0);
  const bookmark = h.storage.get('cycle-active-run:v2:vision');assert.ok(bookmark);
  h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&node.props['aria-label']==='清空对话'));await h.settle();
  assert.equal(h.storage.get('cycle-active-run:v2:vision'),bookmark);
  assert.equal(start.signal?.aborted,false,'Clear must retain the running Cycle subscription');
  assert.equal(h.calls.some(call=>call.path==='/v1/cycle/stop'),false);
  h.advanceScheduledCycle();await h.settle();
  assert.match(h.text(),/Scheduled delivery/,'scheduled user message still projects after Clear');
  assert.ok(JSON.parse(h.storage.get('cycle-active-run:v2:vision')!).cursor>JSON.parse(bookmark!).cursor,'running journal still advances after Clear');
  h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&h.renderer.text(node)==='Stop'));await h.settle();
  assert.ok(h.calls.find(call=>call.path==='/v1/cycle/stop'));assert.equal(h.storage.has('cycle-active-run:v2:vision'),false);
});

test('direct Vision text backend rejects saved photo Cycle without media or stored program changes',async t=>{
  const program={steps:[{type:'message',message:'photo task',photo:[0]}],scheduledMessages:[]};
  const h=routeHarness(t,'vision',false,'text',{cycleEnabled:true,cycleProgram:program});await h.settle();
  const saved=h.storage.get('cycle-editor-state');await h.openCycle();await h.startCycle();
  assert.match(h.text(),/steps\[0\]\.photo/);assert.equal(h.calls.some(call=>call.path==='/v1/cycle/start'),false);
  assert.equal(h.storage.get('cycle-editor-state'),saved);assert.equal(h.storage.has('cycle-active-run:v2:vision'),false);assert.equal(h.mediaCalls(),0);
});

test('Vision Clear cancels pending camera start before it posts a Cycle',async t=>{
  const h=routeHarness(t,'vision',false,'vision',{cycleEnabled:true,cycleProgram:{steps:[{type:'message',message:'photo task',photo:[0]}],scheduledMessages:[]}});
  await h.settle();await h.openCycle();await h.startCycle();
  assert.ok(h.storage.get('cycle-active-run:v2:vision'));
  h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&node.props['aria-label']==='清空对话'));await h.settle();
  assert.equal(h.storage.has('cycle-active-run:v2:vision'),false);
  assert.equal(h.calls.some(call=>call.path==='/v1/cycle/start'||call.path==='/v1/cycle/stop'),false);assert.equal(h.mediaCalls(),0);
});

for(const agentMode of [false,true])for(const boundary of ['pagehide','unmount'] as const)test(`Vision recovered ${agentMode?'Agent':'Chat'} ${boundary} detaches without stopping or deleting producer`,async t=>{
  const h=routeHarness(t,'vision',agentMode,'vision',{recover:true});await h.settle();await h.restore();
  const bookmark=h.storage.get('llm-session-hub-bookmark:vision');
  if(boundary==='pagehide')h.pageHide();else h.unmount();
  await h.settle();assert.equal(h.recoverySignal()?.aborted,true);
  assert.equal(h.calls.some(call=>call.path.includes('/stop')),false);assert.equal(h.deleted.length,0);
  assert.equal(h.storage.get('llm-session-hub-bookmark:vision'),bookmark);
});

for(const pageMode of ['text','vision'] as const)test(`${pageMode} recovered Chat missing identity keeps attachment busy with a diagnostic`,async t=>{
  const h=routeHarness(t,pageMode,false,pageMode,{recover:true,missingRecoveryRequestId:true});await h.settle();await h.restore();
  h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&node.props.title==='停止'));await h.settle();
  assert.match(h.text(),/request identity|request ID/);assert.equal(h.recoverySignal()?.aborted,false);
  assert.equal(h.calls.some(call=>call.path==='/v1/chat/stop'),false);
  assert.equal(h.renderer.findAll((node:any)=>node.type==='button'&&node.props.title==='停止').length,1);
});

for (const pageMode of ['text', 'vision'] as const) test(`${pageMode} recovered legacy Agent may Stop by session when journal has no request identity`, async t => {
  const h = routeHarness(t, pageMode, true, pageMode, {recover:true,missingRecoveryRequestId:true});
  await h.settle();await h.restore();
  h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&node.props.title==='停止'));await h.settle();
  const stop = h.calls.find(call=>call.path==='/v1/agent/stop');assert.ok(stop,h.text());
  assert.deepEqual(stop.body,{session_id:h.recoveredSessionId});
  assert.equal(h.calls.some(call=>call.path.includes('stop-all')),false);assert.equal(h.recoverySignal()?.aborted,true);
  h.holdResponses();await h.submit('After legacy recovery stop');
  h.finishRecovery('Late legacy recovery tail');await h.settle();
  assert.doesNotMatch(h.text(),/Late legacy recovery tail/);assert.match(h.text(),/After legacy recovery stop/);
  h.finishHeld(0,' completed');await h.settle();
});

// SessionProducerPublisher.accept adds request_id only to acceptance; publish
// forwards turn/domain records unchanged (backend/session_hub_runtime.py).
for (const pageMode of ['text', 'vision'] as const) for (const agentMode of [false, true]) {
  test(`${pageMode} ${agentMode?'Agent':'Chat'} recovery production wire projects idless domains and keeps targeted Stop identity`, async t => {
    const h = routeHarness(t,pageMode,agentMode,pageMode,{recover:true});await h.settle();await h.restore();
    assert.match(h.text(),/Recovered task 1/);assert.match(h.text(),/Recovered answer 1/);
    assert.match(h.text(),/Recovered task 2/);assert.match(h.text(),/Recovered answer 2/);
    h.emitRecoveredDomain({type:'timeline_event',event:{kind:'local_memory_retrieved',session_id:h.recoveredSessionId,turn_index:2,chunks:[{text:'Production idless memory'}]}});
    h.emitRecoveredDomain({type:'timeline_event',event:{kind:'compact_started',summary_message_id:'compact-production',metadata:{trigger:'auto',mode:agentMode?'agent':'chat'}}});
    h.emitRecoveredDomain({type:'timeline_event',event:{kind:'compact_delta',summary_message_id:'compact-production',text:'Production idless summary',partial:true}});
    h.emitRecoveredDomain({type:'timeline_event',event:{kind:'compact_completed',summary_message_id:'compact-production',text:'Production idless summary',partial:false,metadata:{}}});
    h.emitRecoveredDomain(agentMode?{type:'content',request_id:'recovered-completed-request',content:'Explicit stale domain'}:{type:'timeline_event',request_id:'recovered-completed-request',event:{kind:'message_delta',source:'chat',display:'chat_bubble',text:'Explicit stale domain',partial:true}});
    await h.settle();
    h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&h.renderer.text(node).startsWith('查看本地记忆')));await h.settle();
    assert.match(h.text(),/Production idless memory/);assert.match(h.text(),/Production idless summary/);
    assert.doesNotMatch(h.text(),/Explicit stale domain/);
    h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&node.props.title==='停止'));await h.settle();
    const stop = h.calls.find(call=>call.path===(agentMode?'/v1/agent/stop':'/v1/chat/stop'));assert.ok(stop);
    assert.equal(agentMode?stop.body.expected_request_id:stop.body.request_id,h.recoveredRequestId);
    h.finishRecovery('Detached idless domain');await h.settle();assert.doesNotMatch(h.text(),/Detached idless domain/);
  });
  test(`${pageMode} ${agentMode?'Agent':'Chat'} recovery production terminal settles after idless result`, async t => {
    const h=routeHarness(t,pageMode,agentMode,pageMode,{recover:true});await h.settle();await h.restore();
    h.finishRecovery(' Idless final tail');await h.settle();
    assert.match(h.text(),/Recovered answer 2 Idless final tail/);
    assert.equal(h.renderer.findAll((node:any)=>node.type==='button'&&node.props.title==='停止').length,0);
    await h.submit('After production recovered terminal');assert.match(h.text(),/Visible answer 1/);
  });
}

for (const pageMode of ['text','vision'] as const) for (const agentMode of [false,true]) test(`${pageMode} ${agentMode?'Agent':'Chat'} Restore cannot steal a pending ordinary admission`, async t => {
  const h=routeHarness(t,pageMode,agentMode,pageMode,{delayAcceptance:true});await h.settle();
  await h.submit('Ordinary admission still pending');
  assert.equal(h.accepted.length,0);assert.equal(h.calls.length,1);
  const request=h.calls[0], bookmark=h.storage.get(`llm-session-hub-bookmark:${pageMode}`);
  const restore=h.renderer.findAll((node:any)=>node.type==='button'&&h.renderer.text(node)==='恢复运行中的任务');
  assert.equal(restore.length,1,'provisional bookmark exposes Restore before acceptance');
  h.renderer.click(restore[0]);await h.settle();
  assert.equal(h.recoveryFetches(),0,'busy ordinary owner must prevent a second Hub attachment');
  assert.match(h.text(),/Ordinary admission still pending/);
  assert.equal(request.signal?.aborted,false);assert.equal(h.storage.get(`llm-session-hub-bookmark:${pageMode}`),bookmark);
  assert.equal(h.calls.some(call=>call.path.includes('/stop')),false);assert.equal(h.deleted.length,0);
  h.releaseAcceptance();await h.settle();
  assert.equal(h.accepted.length,1);assert.equal(h.accepted[0].request_id,request.body.request_id);
  assert.match(h.text(),/Visible answer 1/);assert.match(h.text(),/Ordinary admission still pending/);
  assert.equal(h.renderer.findAll((node:any)=>node.type==='button'&&node.props.title==='停止').length,0);
});

for(const pageMode of ['text','vision'] as const)test(`${pageMode} Agent explicit Stop during a tool call does not offer transport Retry`,async t=>{
  const h=routeHarness(t,pageMode,true,pageMode,{abortAware:true,stopPayloadError:pageMode==='vision'});await h.settle();h.holdResponses();await h.submit('Inspect during tool call');
  h.emitHeld(0,{type:'agent_step',step_type:'tool_call',name:'read_file',input:{path:'pending.txt'},partial:true});await h.settle();
  h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&node.props.title==='停止'));await h.settle();
  assert.doesNotMatch(h.text(),/Request timeout or aborted|Retry/,h.text());
  await h.submit('After intentional stop');
  assert.equal(h.calls.filter(call=>call.path==='/v1/agent/chat').length,2);
  h.finishHeld(1,' completed next request');await h.settle();
  assert.match(h.text(),/Partial answer 2 completed next request/);
  assert.doesNotMatch(h.text(),/Request timeout or aborted|Retry/);
});

for(const pageMode of ['text','vision'] as const)test(`${pageMode} Agent genuine stream failure still offers recovery Retry`,async t=>{
  const h=routeHarness(t,pageMode,true,pageMode,{abortAware:true});await h.settle();h.holdResponses();await h.submit('Transport failure');
  h.failHeld(0,new Error('Network disconnected'));await h.settle();
  assert.match(h.text(),/Network disconnected/);
  assert.equal(h.renderer.findAll((node:any)=>node.type==='button'&&h.renderer.text(node)==='Retry').length,1);
});
for(const pageMode of ['text','vision'] as const)test(`${pageMode} Agent inactivity abort still offers recovery Retry`,async t=>{
  const h=routeHarness(t,pageMode,true,pageMode,{abortAware:true,agentIdleTimeoutSeconds:.025});await h.settle();h.holdResponses();
  t.mock.timers.enable({apis:['setTimeout']});await h.submit('Idle pending request');
  t.mock.timers.tick(25);await h.settle();
  assert.equal(h.calls[0].signal?.aborted,true);
  assert.match(h.text(),/Request timeout or aborted/);
  assert.equal(h.renderer.findAll((node:any)=>node.type==='button'&&h.renderer.text(node)==='Retry').length,1);
});
test('Vision failed Stop preserves genuine producer error and later transport recovery',async t=>{
  const h=routeHarness(t,'vision',true,'vision',{abortAware:true,stopPayloadError:true,stopRejected:true});await h.settle();h.holdResponses();await h.submit('Stop rejection');
  h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&node.props.title==='停止'));await h.settle();
  assert.equal(h.calls[0].signal?.aborted,false,'Rejected server Stop must not close browser transport');
  assert.match(h.text(),/Agent execution interrupted/);
  h.failHeld(0,new DOMException('Idle timeout','AbortError'));await h.settle();
  assert.match(h.text(),/Request timeout or aborted/);
  assert.equal(h.renderer.findAll((node:any)=>node.type==='button'&&h.renderer.text(node)==='Retry').length,1);
});

for(const pageMode of ['text','vision'] as const)for(const boundary of ['Clear','mode switch'] as const)test(`${pageMode} Agent ${boundary} ignores abort-aware stale transport errors`,async t=>{
  const h=routeHarness(t,pageMode,true,pageMode,{abortAware:true});await h.settle();h.holdResponses();await h.submit('Retired Agent task');
  if(boundary==='Clear'){
    h.renderer.click(h.renderer.findOne((node:any)=>node.type==='button'&&node.props['aria-label']==='清空对话'));await h.settle();
  }else await h.changeMode();
  assert.equal(h.calls[0].signal?.aborted,true);
  assert.doesNotMatch(h.text(),/Request timeout or aborted|Retry/);
  await h.submit('Current task after retirement');
  h.finishHeld(1,' completed current task');await h.settle();
  assert.match(h.text(),/Partial answer 2 completed current task/);
  assert.doesNotMatch(h.text(),/Request timeout or aborted|Retry/);
});

test('successful Stop cannot reclassify a transport already aborted by inactivity',()=>{
  const {abortStoppedConversation,wasConversationStopped}=require('./conversation-stop.ts');
  const controller=new AbortController();
  const timeoutReason=new DOMException('Inactivity timeout','AbortError');
  controller.abort(timeoutReason);
  abortStoppedConversation(controller);
  assert.equal(controller.signal.reason,timeoutReason);
  assert.equal(wasConversationStopped(controller),false,'an existing timeout must remain eligible for recovery Retry');
});
