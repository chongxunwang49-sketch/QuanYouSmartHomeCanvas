
import { readFileSync } from 'node:fs'
const list=await(await fetch('http://127.0.0.1:9222/json/list')).json()
const page=list.find(t=>t.type==='page')
const ws=new WebSocket(page.webSocketDebuggerUrl)
await new Promise((r,j)=>{ws.addEventListener('open',r,{once:true});ws.addEventListener('error',j,{once:true})})
let id=0;const pend=new Map();const logs=[]
ws.addEventListener('message',e=>{const m=JSON.parse(e.data)
  if(m.id&&pend.has(m.id)){const{res,rej}=pend.get(m.id);pend.delete(m.id);m.error?rej(new Error(JSON.stringify(m.error))):res(m.result)}
  else if(m.method==='Runtime.consoleAPICalled'||m.method==='Log.entryAdded')logs.push(m)})
const send=(method,params={})=>new Promise((res,rej)=>{const n=++id;pend.set(n,{res,rej});ws.send(JSON.stringify({id:n,method,params}))})
const ev=async e=>(await send('Runtime.evaluate',{expression:e,awaitPromise:true,returnByValue:true})).result.value
await send('Runtime.enable');await send('Log.enable')
console.log('画布:', await ev(`(()=>{const c=document.querySelector('canvas');return c?c.width+'x'+c.height:'无';})()`))
console.log('家具卡是否显示:', await ev(`document.body.innerText.includes('家具已全部摆下')||document.body.innerText.includes('摆不下')`))
console.log('页面文本片段:', await ev(`document.body.innerText.replace(/\\s+/g,' ').slice(0,400)`))
console.log('\n控制台（近 20 条）:')
for(const e of logs.slice(-20)){const p=e.params
  console.log(' ', p.entry?`[${p.entry.level}] ${p.entry.text}`:`[${p.type}] ${(p.args||[]).map(a=>a.value??a.description).join(' ').slice(0,220)}`)}
ws.close()