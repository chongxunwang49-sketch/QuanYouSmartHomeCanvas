
/** 通过界面真的入库一次（然后清理），验证表单到接口这条线。 */
import { readFileSync, writeFileSync } from 'node:fs'
const CDP='http://127.0.0.1:9222', REPO='C:/Users/DELL/AppData/Local/Temp/qy'
const API='http://127.0.0.1:8000/api/v1'
const src=readFileSync(`${REPO}/tests/test_auth.py`,'utf8')
const m=src.match(/\[\(("?admin"?),\s*"([^"]+)"\)/)
const login=await(await fetch(`${API}/auth/login`,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({username:m[1].replace(/"/g,''),password:m[2]})})).json()
const token=login.data.access_token
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
  return r.exceptionDetails?'(异常) '+JSON.stringify(r.exceptionDetails).slice(0,300):r.result.value}
const shot=async n=>{const r=await send('Page.captureScreenshot',{format:'png'})
  writeFileSync(`${REPO}/logs/${n}.png`,Buffer.from(r.data,'base64'));console.log('  截图 → logs/'+n+'.png')}

await send('Page.enable'); await send('Runtime.enable'); await send('Page.bringToFront')
await send('Emulation.setDeviceMetricsOverride',{width:1500,height:1000,deviceScaleFactor:1,mobile:false})
await ev(`localStorage.setItem('qy.access_token',${JSON.stringify(token)});true`)
await send('Page.reload'); await new Promise(r=>setTimeout(r,2000))
await send('Page.navigate',{url:'http://127.0.0.1/knowledge'})
for(let i=0;i<15;i++){ await new Promise(r=>setTimeout(r,1000)); if(await ev('!!document.querySelector("table")')) break }

console.log('① 表单初始状态')
console.log('   入库按钮 disabled:', await ev(`
  (()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='入库');return b?b.disabled:'(没找到按钮)'})()`))
console.log('   置灰原因:', await ev(`
  [...document.querySelectorAll('p')].map(e=>e.textContent.trim()).find(t=>t.includes('还没有填标题'))??'(没显示原因 ⚠️)'`))

console.log('② 填标题 + 正文')
const TITLE='界面入库验证'
const BODY='# 界面入库验证\n\n' + '这是一段通过前端表单提交的验证正文，用来确认表单到接口这条线是通的。'.repeat(6)
await ev(`
  (()=>{
    const set=(el,v)=>{el.focus();const d=Object.getOwnPropertyDescriptor(el.constructor.prototype,'value');
      d.set.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}))}
    const ins=[...document.querySelectorAll('input.field')]
    set(ins[0], ${JSON.stringify(TITLE)})
    set(ins[1], '验证, 界面')
    const ta=document.querySelector('textarea'); set(ta, ${JSON.stringify(BODY)})
    return true
  })()`)
await new Promise(r=>setTimeout(r,400))
console.log('   入库按钮 disabled:', await ev(`
  (()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='入库');return b?b.disabled:'?'})()`))
await shot('u1-form-filled')

console.log('③ 点入库')
await ev(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='入库');b.click();return true})()`)
for(let i=0;i<40;i++){ await new Promise(r=>setTimeout(r,1000))
  if(await ev(`document.body.innerText.includes('已写入')`)) break }
console.log('   结果面板:', await ev(`
  (()=>{const t=document.body.innerText;const i=t.indexOf('已写入');return i<0?'(没有成功面板 ⚠️)':t.slice(i,i+60).replace(/\\s+/g,' ')})()`))
await shot('u2-uploaded')

console.log('④ 清单里出现了吗')
console.log('   ', await ev(`
  document.body.innerText.includes(${JSON.stringify(TITLE)}) ? '出现了 ✓' : '⚠️ 没出现'`))
process.exit(0)