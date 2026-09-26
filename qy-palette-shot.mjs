
/**
 * 跑一次真实生成 → 选定一套 → 飞高俯瞰 → 截图。
 * 用来对比新调色板前后的观感（旧图：logs/d2-fly-topdown.png）。
 */
import { readFileSync, writeFileSync } from 'node:fs'
const CDP='http://127.0.0.1:9222', REPO='C:/Users/DELL/AppData/Local/Temp/qy'
const API='http://127.0.0.1:8000/api/v1'
const src=readFileSync(`${REPO}/tests/test_auth.py`,'utf8')
const m=src.match(/\[\(("?admin"?),\s*"([^"]+)"\)/)
const api=async(p,b,t)=>(await fetch(API+p,{method:b?'POST':'GET',
  headers:{'Content-Type':'application/json',...(t?{Authorization:'Bearer '+t}:{})},
  body:b?JSON.stringify(b):undefined})).json()
const token=(await api('/auth/login',{username:m[1].replace(/"/g,''),password:m[2]})).data.access_token
const layoutId=process.argv[2]

console.log('① 生成一次（约 100 秒）')
const gen=await api('/design/generate',{layout_id:layoutId,
  styles:['modern','nordic','chinese'],budget_grades:['economy','medium','high']},token)
const tid=gen.data.task_id
let plans=[]
for(let i=0;i<80;i++){
  await new Promise(r=>setTimeout(r,5000))
  const st=(await api(`/task/${tid}/status`,null,token)).data
  plans=st?.result?.plans??[]
  if(st?.status==='completed'||st?.status==='failed'){console.log('   状态',st.status,'方案',plans.length);break}
}
const picked=plans.find(p=>p.style==='modern')??plans[0]
console.log('   选定',picked?.plan_id)

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
const shot=async n=>{const r=await send('Page.captureScreenshot',{format:'png'})
  writeFileSync(`${REPO}/logs/${n}.png`,Buffer.from(r.data,'base64'));console.log('  截图 → logs/'+n+'.png')}
const key=(c,k,t)=>send('Input.dispatchKeyEvent',{type:t,code:c,key:k,windowsVirtualKeyCode:k.charCodeAt(0)})
const tap=async(c,k)=>{await key(c,k,'keyDown');await key(c,k,'keyUp');await new Promise(r=>setTimeout(r,400))}
const wait=async(expr,n=40)=>{for(let i=0;i<n;i++){await new Promise(r=>setTimeout(r,1000));if(await ev(expr))return true}return false}

await send('Page.enable'); await send('Runtime.enable'); await send('Page.bringToFront')
await ev(`localStorage.setItem('qy.access_token',${JSON.stringify(token)});true`)
await send('Page.reload'); await new Promise(r=>setTimeout(r,2500))

console.log('② 打开方案对比页并点「选定」')
await send('Page.navigate',{url:`http://127.0.0.1/generate/plans?layout=${layoutId}&task=${tid}`})
await wait('document.querySelectorAll("article").length >= 2')
console.log('   方案卡:', await ev('document.querySelectorAll("article").length'))
console.log('   点选定:', await ev(`(()=>{const b=[...document.querySelectorAll('button')]
  .find(x=>x.textContent.trim()==='选定'); if(!b)return false; b.click(); return true})()`))
await new Promise(r=>setTimeout(r,900))
console.log('   已选定标记:', await ev(`[...document.querySelectorAll('span')].some(x=>x.textContent.includes('已选定'))`))

console.log('③ 进含家具的 3D（带上 task，这样会话里有方案）')
await send('Page.navigate',{url:`http://127.0.0.1/generate/walkthrough?layout=${layoutId}&task=${tid}`})
if(!await wait('!!document.querySelector("canvas")')){
  console.log('   ⚠️ 没等到 canvas，页面文字：',
    (await ev('document.body.innerText')).replace(/\s+/g,' ').slice(0,180)); process.exit(1)
}
console.log('   家具 HUD:', await ev(`[...document.querySelectorAll('p')].map(e=>e.textContent.trim())
  .find(t=>t.includes('摆不下')||t.includes('空房子'))??'(无)'`))
await ev(`(()=>{const c=document.querySelector('canvas');c.scrollIntoView({block:'center'});return true})()`)
await new Promise(r=>setTimeout(r,600)); await send('Page.bringToFront'); await new Promise(r=>setTimeout(r,400))
const box=JSON.parse(await ev(`(()=>{const r=document.querySelector('canvas').getBoundingClientRect()
  return JSON.stringify({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)})})()`))
for(const type of ['mouseMoved','mousePressed','mouseReleased'])
  await send('Input.dispatchMouseEvent',{type,x:box.x,y:box.y,button:'left',
    clickCount:type==='mouseMoved'?0:1,buttons:type==='mousePressed'?1:0})
await new Promise(r=>setTimeout(r,1200))
console.log('   指针锁定:', await ev('!!document.pointerLockElement'))

console.log('④ 站立视角先来一张')
await shot('p1-eye-level')

console.log('⑤ 切飞行 + 升顶 + 低头俯瞰')
await tap('KeyG','g')
for(let i=0;i<160;i++) await key('Space',' ','keyDown')
await new Promise(r=>setTimeout(r,3000))
for(let i=0;i<160;i++) await key('Space',' ','keyUp')
await new Promise(r=>setTimeout(r,400))
let my=box.y
for(let i=0;i<30;i++){my+=26;await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:box.x,y:my,buttons:0})
  await new Promise(r=>setTimeout(r,40))}
await new Promise(r=>setTimeout(r,600))
console.log('   天花板:', await ev(`(()=>{const dt=[...document.querySelectorAll('dt')]
  .find(d=>d.textContent.trim()==='天花板');return dt?dt.nextElementSibling.textContent.replace(/\\s+/g,' ').slice(0,10):'?'})()`))
await shot('p2-fly-topdown')
process.exit(0)