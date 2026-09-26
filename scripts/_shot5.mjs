
import { readFileSync, writeFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
const V=JSON.parse(readFileSync('C:/tmp/verify.json','utf8'))
const list=await(await fetch('http://127.0.0.1:9222/json/list')).json()
const page=list.find(t=>t.type==='page')
const ws=new WebSocket(page.webSocketDebuggerUrl)
await new Promise((r,j)=>{ws.addEventListener('open',r,{once:true});ws.addEventListener('error',j,{once:true})})
let id=0;const pend=new Map()
ws.addEventListener('message',e=>{const m=JSON.parse(e.data)
  if(m.id&&pend.has(m.id)){const{res,rej}=pend.get(m.id);pend.delete(m.id);m.error?rej(new Error(JSON.stringify(m.error))):res(m.result)}})
const send=(m,p={})=>new Promise((res,rej)=>{const n=++id;pend.set(n,{res,rej});ws.send(JSON.stringify({id:n,method:m,params:p}))})
const ev=async e=>(await send('Runtime.evaluate',{expression:e,awaitPromise:true,returnByValue:true})).result.value
const sleep=ms=>new Promise(r=>setTimeout(r,ms))
const shot=async p=>{const r=await send('Page.captureScreenshot',{format:'png'});writeFileSync(p,Buffer.from(r.data,'base64'))}
const front=()=>spawnSync('powershell.exe',['-NoProfile','-Command',`Add-Type -Namespace W -Name U -MemberDefinition '
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);'
$p=Get-Process msedge|Where-Object{$_.MainWindowHandle -ne 0}|Select-Object -First 1
if($p){[W.U]::ShowWindow($p.MainWindowHandle,3);[W.U]::ShowWindow($p.MainWindowHandle,9);[W.U]::SetForegroundWindow($p.MainWindowHandle)}`],{encoding:'utf8'})
await send('Page.enable');await send('Runtime.enable')
await send('Page.navigate',{url:'http://127.0.0.1/'});await sleep(1800)
await ev(`localStorage.setItem('qy.access_token', ${JSON.stringify(V.token)})`)
await ev(`sessionStorage.setItem('qy.generate.selected.${V.layoutId}', ${JSON.stringify(V.planId)})`)
await send('Page.navigate',{url:`http://127.0.0.1/generate/walkthrough?layout=${V.layoutId}&task=${V.taskId}`})
await sleep(8000); front(); await sleep(1500)
for(const t of ['mousePressed','mouseReleased']){await send('Input.dispatchMouseEvent',{type:t,x:800,y:560,button:'left',clickCount:1,buttons:t==='mousePressed'?1:0});await sleep(150)}
await sleep(1500)
const key=async(code,n=1)=>{for(let i=0;i<n;i++){await ev(`document.dispatchEvent(new KeyboardEvent('keydown',{code:'${code}',bubbles:true}));window.dispatchEvent(new KeyboardEvent('keydown',{code:'${code}',bubbles:true}))`);await sleep(150)}
  await ev(`document.dispatchEvent(new KeyboardEvent('keyup',{code:'${code}',bubbles:true}));window.dispatchEvent(new KeyboardEvent('keyup',{code:'${code}',bubbles:true}))`);await sleep(150)}
console.log('按钮文字:', await ev(`[...document.querySelectorAll('button')].map(b=>b.textContent.trim()).filter(t=>/G|俯瞰|地面/.test(t)).join(' | ')`))
await key('KeyG'); await sleep(2000)
await shot('logs/_v5-walk.png'); console.log('→ logs/_v5-walk.png（贴地行走）')
await key('KeyW',4); await sleep(800)
await shot('logs/_v6-walked.png'); console.log('→ logs/_v6-walked.png（往前走）')
console.log('当前位置:', await ev(`(document.body.innerText.match(/当前位置\\s*([^\\n]+)/)||[])[1]`))
ws.close()