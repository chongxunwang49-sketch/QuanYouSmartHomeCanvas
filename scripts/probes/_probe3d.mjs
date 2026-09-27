
import { readFileSync } from 'node:fs'
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
const ev=async e=>{const r=await send('Runtime.evaluate',{expression:e,awaitPromise:true,returnByValue:true});
  if(r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description||'JS'); return r.result.value}
const sleep=ms=>new Promise(r=>setTimeout(r,ms))
await send('Page.enable');await send('Runtime.enable')
await send('Page.navigate',{url:'http://127.0.0.1/'});await sleep(1800)
await ev(`localStorage.setItem('qy.access_token', ${JSON.stringify(V.token)})`)
await ev(`sessionStorage.setItem('qy.generate.selected.${V.layoutId}', ${JSON.stringify(V.planId)})`)
await send('Page.navigate',{url:`http://127.0.0.1/generate/walkthrough?layout=${V.layoutId}&task=${V.taskId}`})
await sleep(8000)
spawnSync('powershell.exe',['-NoProfile','-Command',`Add-Type -Namespace W -Name U -MemberDefinition '
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);'
$p=Get-Process msedge|Where-Object{$_.MainWindowHandle -ne 0}|Select-Object -First 1
if($p){[W.U]::ShowWindow($p.MainWindowHandle,3);[W.U]::ShowWindow($p.MainWindowHandle,9);[W.U]::SetForegroundWindow($p.MainWindowHandle)}`],{encoding:'utf8'})
await sleep(2000)
console.log(await ev(`(()=>{const d=window.__qy3d; if(!d) return '探针没挂上';
  const cam=d.rig.camera;
  let meshes=0, maxDim=0, maxName='';
  d.scene.traverse(o=>{ if(o.isMesh){ meshes++;
    const g=o.geometry; if(g&&g.parameters){ const p=g.parameters;
      const m=Math.max(p.width||p.radius||0, p.height||0, p.depth||0);
      if(m>maxDim){maxDim=m; maxName=o.name||o.parent?.name||'?'} } } });
  return JSON.stringify({
    camera:{x:+cam.position.x.toFixed(2), y:+cam.position.y.toFixed(2), z:+cam.position.z.toFixed(2),
            fov:cam.fov, near:cam.near, far:cam.far},
    player: d.rig.planPosition ? d.rig.planPosition().map(v=>+v.toFixed(2)) : null,
    meshes, maxDim:+maxDim.toFixed(2), maxName,
    children: d.scene.children.map(c=>c.name||c.type),
    info: d.renderer.info.render,
  }, null, 1)})()`))
ws.close()