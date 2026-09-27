
import { readFileSync } from 'node:fs'
const V=JSON.parse(readFileSync('C:/tmp/verify.json','utf8'))
const list=await(await fetch('http://127.0.0.1:9222/json/list')).json()
const ws=new WebSocket(list.find(t=>t.type==='page').webSocketDebuggerUrl)
await new Promise((r,j)=>{ws.addEventListener('open',r,{once:true});ws.addEventListener('error',j,{once:true})})
let id=0;const pend=new Map()
ws.addEventListener('message',e=>{const m=JSON.parse(e.data)
  if(m.id&&pend.has(m.id)){const{res,rej}=pend.get(m.id);pend.delete(m.id);m.error?rej(new Error(JSON.stringify(m.error))):res(m.result)}})
const send=(m,p={})=>new Promise((res,rej)=>{const n=++id;pend.set(n,{res,rej});ws.send(JSON.stringify({id:n,method:m,params:p}))})
const ev=async e=>{const r=await send('Runtime.evaluate',{expression:e,awaitPromise:true,returnByValue:true});
  if(r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description||'JS'); return r.result.value}
await send('Runtime.enable')
console.log('rAF 实测:', JSON.stringify(await ev(`
new Promise(res=>{let n=0;const t0=performance.now();
 function loop(){n++; if(performance.now()-t0<3000) requestAnimationFrame(loop);
   else res({raf_fps:+(n/((performance.now()-t0)/1000)).toFixed(1),
             badge:([...document.querySelectorAll('span')].find(s=>/fps/.test(s.textContent))||{}).textContent});}
 requestAnimationFrame(loop); setTimeout(()=>res({timeout:true}),9000);})`)))
console.log('渲染统计(3 秒内变化):', await ev(`(async()=>{const d=window.__qy3d;
 const a={...d.renderer.info.render}; await new Promise(r=>setTimeout(r,3000));
 const b={...d.renderer.info.render};
 return JSON.stringify({before:a, after:b, 每秒帧数:+((b.frame-a.frame)/3).toFixed(1)});})()`))
console.log('相机朝向:', await ev(`(()=>{const c=window.__qy3d.rig.camera;
 const v=new (window.__qy3d.THREE||Object)(); return JSON.stringify({rotY:+c.rotation.y.toFixed(3), rotX:+c.rotation.x.toFixed(3)});})()`))
console.log('可见对象数:', await ev(`(()=>{const d=window.__qy3d;let vis=0,tot=0;
 d.scene.traverse(o=>{if(o.isMesh){tot++;if(o.visible)vis++;}}); return vis+'/'+tot;})()`))
ws.close()