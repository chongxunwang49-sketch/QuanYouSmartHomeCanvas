
import { spawnSync } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'
const V=JSON.parse(readFileSync('/tmp/verify.json','utf8'))
const APP='http://127.0.0.1/'
const list=await(await fetch('http://127.0.0.1:9222/json/list')).json()
const page=list.find(t=>t.type==='page')
const ws=new WebSocket(page.webSocketDebuggerUrl)
await new Promise((r,j)=>{ws.addEventListener('open',r,{once:true});ws.addEventListener('error',j,{once:true})})
let id=0;const pend=new Map()
ws.addEventListener('message',e=>{const m=JSON.parse(e.data)
  if(m.id&&pend.has(m.id)){const{res,rej}=pend.get(m.id);pend.delete(m.id);m.error?rej(new Error(JSON.stringify(m.error))):res(m.result)}})
const send=(method,params={})=>new Promise((res,rej)=>{const n=++id;pend.set(n,{res,rej});ws.send(JSON.stringify({id:n,method,params}))})
const ev=async e=>(await send('Runtime.evaluate',{expression:e,awaitPromise:true,returnByValue:true})).result.value
const sleep=ms=>new Promise(r=>setTimeout(r,ms))
const shot=async p=>{const r=await send('Page.captureScreenshot',{format:'png'});writeFileSync(p,Buffer.from(r.data,'base64'))}

await send('Page.enable');await send('Runtime.enable')
await send('Page.navigate',{url:APP});await sleep(2500)
await ev(`localStorage.setItem('qy.access_token', ${JSON.stringify(V.token)})`)
await ev(`sessionStorage.setItem('qy.generate.selected.${V.layoutId}', ${JSON.stringify(V.planId)})`)
await send('Page.navigate',{url:`${APP}generate/walkthrough?layout=${V.layoutId}`})
await sleep(9000)
await ev(`localStorage.setItem('qy.access_token', ${JSON.stringify(V.token)})`)
await sleep(500)
console.log('路由:',await ev('location.pathname'))
console.log('HUD:',await ev(`document.body.innerText.replace(/\\s+/g,' ').match(/\\d+ 间房[^A-Z]*/)?.[0]?.slice(0,90)`))
// 提窗口
spawnSync('powershell.exe',['-NoProfile','-Command',`Add-Type -Namespace W -Name U -MemberDefinition '
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);'
$p=Get-Process msedge|Where-Object{$_.MainWindowHandle -ne 0}|Select-Object -First 1
if($p){[W.U]::ShowWindow($p.MainWindowHandle,3);[W.U]::ShowWindow($p.MainWindowHandle,9);[W.U]::SetForegroundWindow($p.MainWindowHandle)}`],{encoding:'utf8'})
await sleep(1500)
// 点画布进入
for (const type of ['mousePressed','mouseReleased']) {
  await send('Input.dispatchMouseEvent',{type,x:800,y:560,button:'left',clickCount:1,buttons:type==='mousePressed'?1:0}); await sleep(150)
}
await sleep(2500)
console.log('已进入漫游:', await ev(`!document.body.innerText.includes('点击进入漫游')`))
await shot('logs/_v1-freeview.png')
// 转向：左右移动鼠标找家具
let best=null
for (let i=0;i<26;i++){
  await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:800+i*30,y:560}); await sleep(70)
  const got = await ev(`(()=>{const e=[...document.querySelectorAll('p')].find(x=>/×.*×.*m/.test(x.textContent||''));return e?e.textContent.replace(/\\s+/g,' '):''})()`)
  if (got) { best=got; break }
}
console.log('准星指向的家具:', best || '(没瞄到)')
await shot('logs/_v2-aim.png')
console.log('截图 → logs/_v1-freeview.png / logs/_v2-aim.png')
ws.close()