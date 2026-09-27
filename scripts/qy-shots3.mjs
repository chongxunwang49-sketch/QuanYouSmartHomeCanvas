
/** 三页截图：知识库 / 数据分析 / 工作台。登录取种子口令，**不打印凭据**。 */
import { readFileSync, writeFileSync } from 'node:fs'
const CDP='http://127.0.0.1:9222', REPO='C:/Users/DELL/AppData/Local/Temp/qy'
const API='http://127.0.0.1:8000/api/v1'
const src=readFileSync(`${REPO}/tests/test_auth.py`,'utf8')
const m=src.match(/\[\(("?admin"?),\s*"([^"]+)"\)/)
const login=await(await fetch(`${API}/auth/login`,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({username:m[1].replace(/"/g,''),password:m[2]})})).json()
const token=login.data?.access_token
if(!token) throw new Error('登录失败')
const l=await(await fetch(`${CDP}/json/list`)).json()
const page=l.find(t=>t.type==='page'&&t.url.startsWith('http://127.0.0.1')&&t.webSocketDebuggerUrl)
const ws=new WebSocket(page.webSocketDebuggerUrl)
await new Promise(r=>ws.addEventListener('open',r,{once:true}))
let id=0; const pending=new Map()
ws.addEventListener('message',e=>{const g=JSON.parse(e.data);const p=pending.get(g.id)
  if(p){pending.delete(g.id);g.error?p.reject(new Error(JSON.stringify(g.error))):p.resolve(g.result)}})
const send=(mm,pp={})=>{const i=++id;ws.send(JSON.stringify({id:i,method:mm,params:pp}))
  return new Promise((res,rej)=>pending.set(i,{resolve:res,reject:rej}))}
const ev=async x=>{const r=await send('Runtime.evaluate',{expression:x,returnByValue:true,awaitPromise:true})
  return r.exceptionDetails?'(异常) '+JSON.stringify(r.exceptionDetails).slice(0,200):r.result.value}
const shot=async n=>{const r=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true})
  writeFileSync(`${REPO}/logs/${n}.png`,Buffer.from(r.data,'base64'));console.log('  截图 → logs/'+n+'.png')}
await send('Page.enable'); await send('Runtime.enable'); await send('Page.bringToFront')
await send('Emulation.setDeviceMetricsOverride',{width:1500,height:1000,deviceScaleFactor:1,mobile:false})
await ev(`localStorage.setItem('qy.access_token',${JSON.stringify(token)});true`)
await send('Page.reload'); await new Promise(r=>setTimeout(r,2500))

for (const [path,name,waitFor] of [
  ['/knowledge','k1-knowledge','!!document.querySelector("table")'],
  ['/analytics','k2-analytics','!!document.body.innerText.includes("后端性能指标")'],
  ['/','k3-dashboard','!!document.body.innerText.includes("累计")'],
]) {
  await send('Page.navigate',{url:'http://127.0.0.1'+path})
  await new Promise(r=>setTimeout(r,3000))
  for(let i=0;i<12;i++){ if(await ev(waitFor)) break; await new Promise(r=>setTimeout(r,1000)) }
  await new Promise(r=>setTimeout(r,1500))
  console.log('①', path)
  // 检查有没有把对象直接插值出来
  const obj = await ev(`document.body.innerText.includes('[object Object]')`)
  console.log('   出现 [object Object]:', obj ? '⚠️ 有' : '没有 ✓')
  const undef = await ev(`(document.body.innerText.match(/undefined/g)||[]).length`)
  console.log('   出现 undefined:', undef, undef ? '⚠️' : '✓')
  await shot(name)
}
process.exit(0)