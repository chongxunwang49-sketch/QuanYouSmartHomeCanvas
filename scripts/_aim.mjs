
import { readFileSync, writeFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
const API='http://127.0.0.1/api/v1'
const api=async(p,b,t)=>{const r=await fetch(API+p,{method:b===undefined?'GET':'POST',
  headers:{'Content-Type':'application/json',...(t?{Authorization:`Bearer ${t}`}:{})},
  body:b===undefined?undefined:JSON.stringify(b)});return r.json()}
const src=readFileSync('tests/test_auth.py','utf8')
const m=src.match(/\[\(("?admin"?),\s*"([^"]+)"\)/)
const token=(await api('/auth/login',{username:m[1].replace(/"/g,''),password:m[2]})).data.access_token
const sleep=ms=>new Promise(r=>setTimeout(r,ms))

const layoutId='layout_20260926_c67632'
console.log('① 生成方案（复用已解析的户型）…')
const g=await api('/design/generate',{layout_id:layoutId,styles:['modern','nordic','chinese'],
  budget_grades:['economy','medium','high']},token)
if(g.code!==0){console.log('  失败:',g.msg); process.exit(1)}
let s; for(let i=0;i<70;i++){s=(await api(`/task/${g.data.task_id}/status`,undefined,token)).data
  process.stdout.write(`   ${(i+1)*5}s ${s.phase_text}      \r`)
  if(['completed','failed'].includes(s.status))break; await sleep(5000)}
console.log(`\n   ${s.status}  方案 ${(s.result?.plans||[]).length}`)

const list=await(await fetch('http://127.0.0.1:9222/json/list')).json()
const ws=new WebSocket(list.find(t=>t.type==='page').webSocketDebuggerUrl)
await new Promise((r,j)=>{ws.addEventListener('open',r,{once:true});ws.addEventListener('error',j,{once:true})})
let id=0;const pend=new Map()
ws.addEventListener('message',e=>{const m2=JSON.parse(e.data)
  if(m2.id&&pend.has(m2.id)){const{res,rej}=pend.get(m2.id);pend.delete(m2.id);m2.error?rej(new Error(JSON.stringify(m2.error))):res(m2.result)}})
const send=(mm,p={})=>new Promise((res,rej)=>{const n=++id;pend.set(n,{res,rej});ws.send(JSON.stringify({id:n,method:mm,params:p}))})
const ev=async e=>{const r=await send('Runtime.evaluate',{expression:e,awaitPromise:true,returnByValue:true});
  if(r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description||'JS'); return r.result.value}
await send('Page.enable');await send('Runtime.enable')
await send('Page.navigate',{url:'http://127.0.0.1/login'});await sleep(3000)
await ev(`localStorage.setItem('qy.access_token', ${JSON.stringify(token)}); sessionStorage.setItem('qy.generate.selected.${layoutId}','plan_modern_economy'); 'ok'`)
await send('Page.navigate',{url:`http://127.0.0.1/generate/walkthrough?layout=${layoutId}&task=${g.data.task_id}`})
await sleep(10000)
spawnSync('powershell.exe',['-NoProfile','-Command',`Add-Type -Namespace W -Name U -MemberDefinition '
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);'
$p=Get-Process msedge|Where-Object{$_.MainWindowHandle -ne 0}|Select-Object -First 1
if($p){[W.U]::ShowWindow($p.MainWindowHandle,3);[W.U]::ShowWindow($p.MainWindowHandle,9);[W.U]::SetForegroundWindow($p.MainWindowHandle)}`],{encoding:'utf8'})
await sleep(2500)
for(const t of ['mousePressed','mouseReleased']){await send('Input.dispatchMouseEvent',{type:t,x:800,y:560,button:'left',clickCount:1,buttons:t==='mousePressed'?1:0});await sleep(160)}
await sleep(2000)
// 扫视直到准星逮到家具
let hit=null
for(let i=0;i<40;i++){
  await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:800+ (i%2? 60:-60), y:560 - (i*6)})
  await sleep(90)
  const got=await ev(`(()=>{const p=[...document.querySelectorAll('p')].find(x=>/×.*×.*m/.test(x.textContent||''));return p?p.closest('div').textContent.replace(/\\s+/g,' '):''})()`)
  if(got){hit=got;break}
}
console.log('② 准星逮到:', hit||'(没逮到)')
const box=await ev(`(()=>{const p=[...document.querySelectorAll('p')].find(x=>/×.*×.*m/.test(x.textContent||''));
  if(!p) return 'none'; const d=p.closest('div'); const r=d.getBoundingClientRect();
  const cr=document.querySelector('span.rounded-full')?.getBoundingClientRect();
  return JSON.stringify({卡片:{left:Math.round(r.left),top:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height)},
    准星:cr?{cx:Math.round(cr.left+cr.width/2),cy:Math.round(cr.top+cr.height/2)}:null,
    画布中心:{x:Math.round(innerWidth/2),y:Math.round(innerHeight/2)}});})()`)
console.log('③ 几何:', box)
const r2=await send('Page.captureScreenshot',{format:'png'})
writeFileSync('logs/_aim.png',Buffer.from(r2.data,'base64'))
console.log('→ logs/_aim.png')
ws.close()