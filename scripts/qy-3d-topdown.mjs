
/**
 * 只做一件事：飞高、**低头**，从上面看格局 —— 这是需求方要的那个场景
 * 「启动飞行模式就是为了在高处看格局」。顺带对比行走模式下同一位置
 * 能不能看到（天花板会挡住）。
 */
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
const send=(method,params={})=>{const i=++id;ws.send(JSON.stringify({id:i,method,params}))
  return new Promise((res,rej)=>pending.set(i,{resolve:res,reject:rej}))}
const ev=async x=>{const r=await send('Runtime.evaluate',{expression:x,returnByValue:true,awaitPromise:true})
  if(r.exceptionDetails)return '(异常) '+JSON.stringify(r.exceptionDetails).slice(0,300);return r.result.value}
const shot=async n=>{const r=await send('Page.captureScreenshot',{format:'png'})
  writeFileSync(`${REPO}/logs/${n}.png`,Buffer.from(r.data,'base64'));console.log('  截图 → logs/'+n+'.png')}
const key=(code,k,type)=>send('Input.dispatchKeyEvent',{type,code,key:k,windowsVirtualKeyCode:k.charCodeAt(0)})
const tap=async(c,k)=>{await key(c,k,'keyDown');await key(c,k,'keyUp');await new Promise(r=>setTimeout(r,300))}
const mouse=async(dy,dx=0)=>{await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:900,y:500,deltaX:dx,deltaY:dy,buttons:0})
  await new Promise(r=>setTimeout(r,60))}
const hud=()=>ev(`[...document.querySelectorAll('p,dd,dt,li,span')].map(e=>e.textContent.replace(/\\s+/g,' ').trim())`)

await send('Page.enable'); await send('Runtime.enable'); await send('Page.bringToFront')
await ev(`localStorage.setItem('qy.access_token',${JSON.stringify(token)});true`)
await send('Page.navigate',{url:`http://127.0.0.1/generate/walkthrough?layout=${layoutId}`})
await new Promise(r=>setTimeout(r,6000))
await ev(`(()=>{const c=document.querySelector('canvas');c.scrollIntoView({block:'center'});return true})()`)
await new Promise(r=>setTimeout(r,500)); await send('Page.bringToFront'); await new Promise(r=>setTimeout(r,400))

const box=JSON.parse(await ev(`(()=>{const r=document.querySelector('canvas').getBoundingClientRect()
  return JSON.stringify({x:r.x+r.width/2,y:r.y+r.height/2})})()`))
for(const type of ['mouseMoved','mousePressed','mouseReleased'])
  await send('Input.dispatchMouseEvent',{type,x:box.x,y:box.y,button:'left',
    clickCount:type==='mouseMoved'?0:1,buttons:type==='mousePressed'?1:0})
await new Promise(r=>setTimeout(r,1200))
console.log('指针已锁定:', await ev('!!document.pointerLockElement'))

// ── 对照：行走模式下从同一位置往上看，天花板的颜色会占满上半屏 ──
console.log('① 行走模式（天花板显示）—— 抬头看')
for(let i=0;i<40;i++) await mouse(-12)          // 视线往上
await new Promise(r=>setTimeout(r,400))
await shot('t1-walk-lookup')
console.log('   视线已抬到上限附近')

// 复位视角：往下
for(let i=0;i<60;i++) await mouse(12)
await new Promise(r=>setTimeout(r,300))

// ── 切飞行、升高、低头俯瞰 ──
console.log('② 切飞行模式')
await tap('KeyG','g')
await new Promise(r=>setTimeout(r,500))
for(let i=0;i<120;i++) await key('Space',' ','keyDown')
await new Promise(r=>setTimeout(r,2600))
for(let i=0;i<120;i++) await key('Space',' ','keyUp')
await new Promise(r=>setTimeout(r,500))
console.log('   天花板:', (await hud()).find(t=>t.includes('飞行模式自动隐藏')) ?? '(HUD 没写)')

console.log('③ 低头俯瞰')
for(let i=0;i<26;i++) await mouse(18)
await new Promise(r=>setTimeout(r,500))
await shot('t2-fly-topdown')
console.log('   门提示:', (await hud()).find(t=>t.includes('准星')||t.includes('最近的一扇')) ?? '(够不着门)')

console.log('④ 按 F 开关门（这次连 span 一起查）')
await tap('KeyF','f')
await new Promise(r=>setTimeout(r,300))
const lines = await hud()
console.log('   瞬时提示:', lines.find(t=>t==='门已打开'||t==='门已关上') ?? '(没抓到瞬时提示)')
console.log('   门面板状态:', lines.find(t=>t.includes('当前状态')) ?? '(没有门面板)')
await shot('t3-after-f')

console.log('⑤ 再按一次 F')
await tap('KeyF','f')
await new Promise(r=>setTimeout(r,300))
console.log('   门面板状态:', (await hud()).find(t=>t.includes('当前状态')) ?? '(没有门面板)')
await shot('t4-after-f2')
process.exit(0)