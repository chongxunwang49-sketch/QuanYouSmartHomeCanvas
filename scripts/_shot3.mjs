
import { readFileSync, writeFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
const V=JSON.parse(readFileSync('/tmp/verify.json','utf8'))
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
// 先取一个 parse 任务 id（从库里查不到，用一个已知可用的：同户型的 walkthrough 不带 plan）
await send('Page.navigate',{url:`http://127.0.0.1/parse?task=${V.parseTask||''}`});await sleep(500)
await ev(`localStorage.setItem('qy.access_token', ${JSON.stringify(V.token)})`)
await send('Page.navigate',{url:'http://127.0.0.1/generate/walkthrough?layout='+V.layoutId+'&task='+V.taskId})
await sleep(8000); front(); await sleep(2000)
for(const type of ['mousePressed','mouseReleased']){await send('Input.dispatchMouseEvent',{type,x:800,y:560,button:'left',clickCount:1,buttons:type==='mousePressed'?1:0});await sleep(150)}
await sleep(2000)
// 抬高视角看全景
for(let i=0;i<10;i++){await ev(`document.dispatchEvent(new KeyboardEvent('keydown',{code:'Space',bubbles:true}))`);await sleep(120)}
await ev(`document.dispatchEvent(new KeyboardEvent('keyup',{code:'Space',bubbles:true}))`)
await sleep(1500); await shot('logs/_v3-up.png')
console.log('抬升后截图 → logs/_v3-up.png')
// 读画布像素：看是不是一片纯色
const px=await ev(`(()=>{const c=document.querySelector('canvas');if(!c)return 'no canvas';
  const g=c.getContext('webgl2')||c.getContext('webgl');if(!g)return 'no gl';
  const w=c.width,h=c.height;const buf=new Uint8Array(4*9);const out=[];
  for(const [rx,ry] of [[0.5,0.5],[0.2,0.2],[0.8,0.8],[0.5,0.8],[0.2,0.8]]){
    const b=new Uint8Array(4); g.readPixels(Math.floor(w*rx),Math.floor(h*ry),1,1,g.RGBA,g.UNSIGNED_BYTE,b);
    out.push([...b].join(','));}
  return c.width+'x'+c.height+' 采样:'+out.join(' | ');})()`)
console.log('画布采样:', px)
ws.close()