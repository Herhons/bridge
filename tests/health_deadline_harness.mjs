import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const [apiUrl, schedulerUrl, panelPath] = process.argv.slice(2);
const realTimer = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms, ...args) => realTimer(fn, Math.min(ms, 10), ...args);
const api = await import(apiUrl);
const {PollScheduler} = await import(schedulerUrl);
const never = () => new Promise(() => {});
let lastSignal;
const response = (status=200, json=async()=>({ok:true,version:'0.0.9'})) => ({status,ok:status<400,json});
const fetchWith = fn => { globalThis.fetch = async (_url, options) => {lastSignal=options.signal;return fn();}; };
async function timeoutCase(fn, operation) {
  fetchWith(fn);
  await assert.rejects(operation(), error => error.kind==='offline' && error.code==='timeout');
  assert.equal(lastSignal.aborted, true);
}
await timeoutCase(never, api.health);
await timeoutCase(() => response(200, never), api.health);
await timeoutCase(() => response(200, () => new Promise(r=>realTimer(()=>r({ok:true}),30))), api.health);
await timeoutCase(() => response(200, never), () => api.endpoints('fixture'));
await timeoutCase(() => response(200, never), () => api.job('fixture','job'));
fetchWith(() => response());
assert.equal((await api.health()).ok,true);
await new Promise(r=>realTimer(r,20));
assert.equal(lastSignal.aborted,false);
fetchWith(() => response(200,async()=>{throw new SyntaxError('bad');}));
await assert.rejects(api.health(), error=>error.code==='invalid_json');
fetchWith(() => response(401,async()=>({error:'bad_token'})));
await assert.rejects(api.endpoints('fixture'), error=>error.kind==='unauthorized' && error.status===401);
fetchWith(() => response(404,async()=>({error:'not_found'})));
await assert.rejects(api.job('fixture','missing'), error=>error.kind==='not_found');

// A body deadline releases the scheduler's single busy tick; later ticks run.
let ticks=0;
fetchWith(() => response(200,never));
const poll = new PollScheduler(async()=>{ticks++;await api.job('fixture','job');},{interval:1});
poll.start();
await new Promise(r=>realTimer(r,65));
poll.stop();
assert.ok(ticks>=2);
await new Promise(r=>realTimer(r,15));
assert.equal(poll.busy,false);

// Execute the actual initialize body with fake storage/DOM/render dependencies.
const source = fs.readFileSync(panelPath,'utf8');
const begin = source.indexOf('async function initialize() {');
const body = source.slice(begin,source.lastIndexOf('void initialize();'));
const events=[];
let release;
const pendingEndpoint = new Promise(r=>{release=r;});
const context = {lockStorage:async()=>{},document:{body:{classList:{add(){}}}},restore:x=>x,
  readLocal:async key=>key==='jobs'?[]:'',DRAFT_KEY:'draft',TOKEN_KEY:'token',JOBS_KEY:'jobs',
  // T4-2：initialize 现在还会读这三个 key（开关 / 工作流镜像 / 错误计数）。
  // 这里给的是空值，等价于"首次安装"：开关取默认 true、镜像为 null、无历史错误。
  TEST_KEY:'testMode',WORKFLOW_KEY:'workflow',ERRORS_KEY:'errors',
  pruneJobs:x=>x,trackedJobs:[],token:'',state:{prompt:''},$:()=>({}),setPairStatus(){},
  bindEvents(){},renderMode(){},renderSeats(){},renderRoles(){},renderAttachments(){},
  // renderWorkflow / loadWorkflow 定义在 initialize 之前，不在本 harness 提取的代码段里，
  // 所以必须在这里给替身，否则 initialize 一调就 ReferenceError。
  renderEndpointList(){},renderJobs(){},renderOutput(){},renderWorkflow(){},toast(){},
  loadWorkflow:async()=>{},
  loadEndpoints:()=>pendingEndpoint,refreshHealth:async()=>{events.push('health');},
  isActive:()=>false,poll:{start(){events.push('start');}},ACTIVE_INTERVAL_MS:1,IDLE_INTERVAL_MS:2,tick:async()=>{}};
vm.createContext(context);
vm.runInContext(body,context);
const initial = context.initialize();
await new Promise(r=>realTimer(r,1));
assert.ok(events.includes('health'));
assert.ok(!events.includes('start'));
release();
await initial;
assert.ok(events.includes('start'));
console.log(JSON.stringify({ok:true,deadlineCases:5,classificationCases:3,pollTicks:ticks,initializeHealthConcurrent:true}));
