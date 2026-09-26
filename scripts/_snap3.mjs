
import { readFileSync, writeFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
const V=JSON.parse(readFileSync('C:/tmp/verify.json','utf8'))
const list=await(await fetch('http://127.0.0.1:9222/json/list')).json()
const ws=new WebSocket(list.find(t=>t.type==='page').webSocketDebuggerUrl)
await new Promise((r,j)=>{ws.addEventListener('open',r,{once:true});ws.addEventListener('error',j,{once:true})})
let id=0;const pend=new Map()
ws.addEventListener('message',e=>{const m=JSON.parse(e.data)
  if(m.id&&pend.has(m.id)){const{res,rej}=pend.get(m.id);pend.delete(m.id);m.error?rej(new Error(JSON.stringify(m.error))):res(m.result)}})
const send=(m,p={})=>new Promise((res,rej)=>{const n=++id;pend.set(n,{res,rej});ws.send(JSON.stringify({id:n,method:m,params:p}))})
const sleep=ms=>new Promise(r=>setTimeout(r,ms))
await send('Page.enable'); await send('Runtime.enable')
// 登录态与选定方案**已经在浏览器里**（前面几轮写进去的），只需重载
await send('Page.navigate',{url:`http://127.0.0.1/generate/walkthrough?layout=${V.layoutId}&task=${V.taskId}`})
await sleep(10000)
spawnSync('powershell.exe',['-NoProfile','-Command',`Add-Type -Namespace W -Name U -MemberDefinition '
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);'
$p=Get-Process msedge|Where-Object{$_.MainWindowHandle -ne 0}|Select-Object -First 1
if($p){[W.U]::ShowWindow($p.MainWindowHandle,3);[W.U]::ShowWindow($p.MainWindowHandle,9);[W.U]::SetForegroundWindow($p.MainWindowHandle)}`],{encoding:'utf8'})
await sleep(2500)
for(const t of ['mousePressed','mouseReleased']){await send('Input.dispatchMouseEvent',{type:t,x:800,y:560,button:'left',clickCount:1,buttons:t==='mousePressed'?1:0});await sleep(150)}
await sleep(2500)
const r=await send('Page.captureScreenshot',{format:'png'})
writeFileSync('logs/_v8-final.png',Buffer.from(r.data,'base64'))
console.log('→ logs/_v8-final.png')
ws.close()