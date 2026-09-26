
import { writeFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
const list=await(await fetch('http://127.0.0.1:9222/json/list')).json()
const ws=new WebSocket(list.find(t=>t.type==='page').webSocketDebuggerUrl)
await new Promise((r,j)=>{ws.addEventListener('open',r,{once:true});ws.addEventListener('error',j,{once:true})})
let id=0;const pend=new Map()
ws.addEventListener('message',e=>{const m=JSON.parse(e.data)
  if(m.id&&pend.has(m.id)){const{res,rej}=pend.get(m.id);pend.delete(m.id);m.error?rej(new Error(JSON.stringify(m.error))):res(m.result)}})
const send=(m,p={})=>new Promise((res,rej)=>{const n=++id;pend.set(n,{res,rej});ws.send(JSON.stringify({id:n,method:m,params:p}))})
const ev=async e=>(await send('Runtime.evaluate',{expression:e,awaitPromise:true,returnByValue:true})).result.value
const sleep=ms=>new Promise(r=>setTimeout(r,ms))
await send('Page.enable');await send('Runtime.enable')
spawnSync('powershell.exe',['-NoProfile','-Command',`Add-Type -Namespace W -Name U -MemberDefinition '
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);'
$p=Get-Process msedge|Where-Object{$_.MainWindowHandle -ne 0}|Select-Object -First 1
if($p){[W.U]::ShowWindow($p.MainWindowHandle,3);[W.U]::ShowWindow($p.MainWindowHandle,9);[W.U]::SetForegroundWindow($p.MainWindowHandle)}`],{encoding:'utf8'})
await sleep(1500)
console.log('可见:', await ev(`JSON.stringify({hidden:document.hidden})`))
for(const t of ['mousePressed','mouseReleased']){await send('Input.dispatchMouseEvent',{type:t,x:800,y:560,button:'left',clickCount:1,buttons:t==='mousePressed'?1:0});await sleep(150)}
await sleep(1200)
// 把视点调正：抬头看向房屋内部（俯角减小）
for(let i=0;i<6;i++){await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:800,y:560-i*18});await sleep(80)}
await sleep(1200)
const r=await send('Page.captureScreenshot',{format:'png'})
writeFileSync('logs/_v7-live.png',Buffer.from(r.data,'base64'))
console.log('→ logs/_v7-live.png  相机:', await ev(`(()=>{const c=window.__qy3d.rig.camera;return c.position.toArray().map(v=>+v.toFixed(1)).join(',')+' 朝向 y'+c.rotation.y.toFixed(2)+' x'+c.rotation.x.toFixed(2)})()`))
console.log('渲染:', await ev(`JSON.stringify(window.__qy3d.renderer.info.render)`))
ws.close()