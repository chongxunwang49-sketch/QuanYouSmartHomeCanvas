
/** 开局第一帧是什么样 —— **一个键都不按**。验证"自由视角当默认"的落点。 */
import { readFileSync, writeFileSync } from 'node:fs'
const CDP='http://127.0.0.1:9222', REPO='C:/Users/DELL/AppData/Local/Temp/qy'
const API='http://127.0.0.1:8000/api/v1'
const src=readFileSync(`${REPO}/tests/test_auth.py`,'utf8')
const m=src.match(/\[\(("?admin"?),\s*"([^"]+)"\)/)
const login=await(await fetch(`${API}/auth/login`,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({username:m[1].replace(/"/g,''),password:m[2]})})).json()
const token=login.data.access_token
const layoutId=process.argv[2]
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
const wait=async(e,n=45)=>{for(let i=0;i<n;i++){await new Promise(r=>setTimeout(r,i===0?400:1000));if(await ev(e))return true}return false}

await send('Page.enable'); await send('Runtime.enable'); await send('Page.bringToFront')
await send('Emulation.setDeviceMetricsOverride',{width:1500,height:900,deviceScaleFactor:1,mobile:false})
await ev(`localStorage.setItem('qy.access_token',${JSON.stringify(token)});true`)
await send('Page.reload'); await new Promise(r=>setTimeout(r,2500))

console.log('① 打开含家具的 3D，**不按任何键**')
await send('Page.navigate',{url:`http://127.0.0.1/generate/walkthrough?layout=${layoutId}&task=${process.argv[3]||''}`})
if(!await wait('!!document.querySelector("canvas")')){console.log('   ⚠️ 没有 canvas：',
  (await ev('document.body.innerText')).replace(/\s+/g,' ').slice(0,160)); process.exit(1)}
await new Promise(r=>setTimeout(r,1500))
await shot('q1-default-first-frame')

const st = await ev(`(()=>{
  const btn=[...document.querySelectorAll('button')].find(b=>/升空俯瞰|下到地面行走|仅自由视角/.test(b.textContent))
  const hdr=[...document.querySelectorAll('p')].map(e=>e.textContent.replace(/\\s+/g,' ').trim())
  return JSON.stringify({
    按钮: btn?btn.textContent.trim():'(没有)',
    头部: hdr.find(t=>t.includes('间房'))||'',
    引导: hdr.find(t=>t.includes('看整个格局')||t.includes('贴着地面走'))||'(引导层没显示，已锁定？)',
  })})()`)
console.log('  ', st)

console.log('② 点进去看引导层（说明"这个视角是干什么的"）')
await shot('q2-before-lock')

console.log('③ 进漫游后界面上的快速前往是否可点')
const box=JSON.parse(await ev(`(()=>{const r=document.querySelector('canvas').getBoundingClientRect()
  return JSON.stringify({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)})})()`))
await ev(`(()=>{document.querySelector('canvas').scrollIntoView({block:'center'});return true})()`)
await new Promise(r=>setTimeout(r,500))
for(const type of ['mouseMoved','mousePressed','mouseReleased'])
  await send('Input.dispatchMouseEvent',{type,x:box.x,y:box.y,button:'left',
    clickCount:type==='mouseMoved'?0:1,buttons:type==='mousePressed'?1:0})
await new Promise(r=>setTimeout(r,1300))
console.log('   锁定:', await ev('!!document.pointerLockElement'))
console.log('   快速前往可点数:', await ev(`
  [...document.querySelectorAll('button')].filter(b => /^(主卧|次卧|儿童房|客厅|餐厅|厨房|阳台|卫生间|玄关)$/.test(b.textContent.trim()) && !b.disabled).length`),
  '/', await ev(`[...document.querySelectorAll('button')].filter(b => /^(主卧|次卧|儿童房|客厅|餐厅|厨房|阳台|卫生间|玄关)$/.test(b.textContent.trim())).length`))
await ev(`window.scrollTo(0,0)`); await new Promise(r=>setTimeout(r,300))
await shot('q3-locked-fly')
process.exit(0)