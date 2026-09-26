
/** AC-10 走一遍界面：解析 → 矢量图页 → 点地面 → 换材料 → 图上换色 + 造价。 */
import { readFileSync, writeFileSync } from 'node:fs'
const CDP='http://127.0.0.1:9222', REPO='C:/Users/DELL/AppData/Local/Temp/qy'
const API='http://127.0.0.1:8000/api/v1'
const src=readFileSync(`${REPO}/tests/test_auth.py`,'utf8')
const m=src.match(/\[\(("?admin"?),\s*"([^"]+)"\)/)
const api=async(p,b,t)=>(await fetch(API+p,{method:b?'POST':'GET',
  headers:{'Content-Type':'application/json',...(t?{Authorization:'Bearer '+t}:{})},
  body:b?JSON.stringify(b):undefined})).json()
const token=(await api('/auth/login',{username:m[1].replace(/"/g,''),password:m[2]})).data.access_token

const ONLY_TASK = process.argv[2]
console.log('① 跑一次真实解析（约 60 秒）')
const img=readFileSync(`${REPO}/演示素材/户型图/03-三室两厅-98平.png`).toString('base64')
const parsed=await api('/layout/parse',{image:img,image_media_type:'image/png'},token)
const tid=parsed.data.task_id
let lid=''
for(let i=0;i<40;i++){ await new Promise(r=>setTimeout(r,3000))
  const st=(await api(`/task/${tid}/status`,null,token)).data
  if(st?.status==='completed'||st?.status==='failed'){ lid=st?.result?.layout_id??''; console.log('   状态',st.status,'layout',lid); break } }
if(!lid){ console.log('解析失败'); process.exit(1) }

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
const wait=async(e,n=30)=>{for(let i=0;i<n;i++){await new Promise(r=>setTimeout(r,1000));if(await ev(e))return true}return false}

await send('Page.enable'); await send('Runtime.enable'); await send('Page.bringToFront')
await send('Emulation.setDeviceMetricsOverride',{width:1500,height:1100,deviceScaleFactor:1,mobile:false})
await ev(`localStorage.setItem('qy.access_token',${JSON.stringify(token)});true`)
await send('Page.reload'); await new Promise(r=>setTimeout(r,2500))

console.log('② 打开矢量图页')
await send('Page.navigate',{url:`http://127.0.0.1/parse/drawing?task=${tid}`})
if(!await wait(`!!document.querySelector('.plan-svg polygon.room')`)){ console.log('   ⚠️ 没等到矢量图'); process.exit(1) }
console.log('   有地面材质面板:', await ev(`document.body.innerText.includes('地面材质替换')`))
console.log('   材料卡片数:', await ev(`
  (()=>{const t=document.body.innerText;const i=t.indexOf('种可选');return i<0?'?':t.slice(i-4,i+3)})()`))
await shot('f1-before')

console.log('③ 在图上点一块地面（room-0 或第一块地面热区）')
const clicked = await ev(`
  (()=>{
    const el=document.querySelector('.plan-svg .hotspot')
    if(!el) return '(没有热区)'
    const all=[...document.querySelectorAll('.plan-svg .hotspot')]
    // 后端给的地面热区是前 N 块；直接点第一块
    all[0].dispatchEvent(new MouseEvent('click',{bubbles:true}))
    return '已点第一块热区'
  })()`)
console.log('   ', clicked)
await new Promise(r=>setTimeout(r,600))
console.log('   面板里选中的房间:', await ev(`
  (()=>{const t=document.body.innerText;const i=t.indexOf('画布上选中的是');return i<0?'(没选中 ⚠️)':t.slice(i,i+16).replace(/\\s+/g,' ')})()`))

console.log('④ 选一种材料')
const applied = await ev(`
  (()=>{
    const btns=[...document.querySelectorAll('button')]
      .filter(b => b.textContent.includes('元/㎡') && b.textContent.includes('全友'))
    if(!btns.length) return '(找不到材料按钮)'
    const b = btns[btns.length-1]
    const label = b.textContent.replace(/\\s+/g,' ').trim().slice(0,44)
    b.click()
    return '点了：' + label
  })()`)
console.log('   ', applied)
await wait(`document.body.innerText.includes('已换')`, 20)
console.log('   已换清单:', await ev(`
  (()=>{const t=document.body.innerText;const i=t.indexOf('已换（');return i<0?'(没有 ⚠️)':t.slice(i,i+70).replace(/\\s+/g,' ')})()`))
await new Promise(r=>setTimeout(r,1200))
await shot('f2-after')

console.log('⑤ 图上的填充色变了吗')
console.log('   room-replaced 数量:', await ev(`document.querySelectorAll('.plan-svg polygon.room-replaced').length`))
console.log('   room-selected 数量:', await ev(`document.querySelectorAll('.plan-svg polygon.room-selected').length`))
process.exit(0)