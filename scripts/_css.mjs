
import { writeFileSync } from 'node:fs'
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
console.log(await ev(`(()=>{
  // 准星所在的那个定位父容器
  const cross=[...document.querySelectorAll('span')].find(s=>/rounded-full/.test(s.className||'') && /ring-/.test(s.className||''));
  const host=cross && cross.parentElement;
  if(!host) return '找不到准星的父容器';
  // 用**与真实卡片完全相同的 class** 造一个替身，只换内容以便辨认
  const probe=document.createElement('div');
  probe.id='__probe';
  probe.className='pointer-events-none absolute left-1/2 top-1/2 z-10 translate-x-[14px] translate-y-[calc(-100%-14px)] max-w-[240px] rounded-xl bg-wood-dark/85 px-3 py-2 backdrop-blur-sm';
  probe.innerHTML='<p style="font-size:13px;font-weight:600">占位</p><p style="font-size:10px">客厅 · 0.50×0.50×0.55 m</p><p style="font-size:10px">放在房间中部</p><p style="font-size:10px">按 prefer=any 选的位置</p>';
  host.appendChild(probe);
  const r=probe.getBoundingClientRect();
  const c=cross.getBoundingClientRect();
  const cx=c.left+c.width/2, cy=c.top+c.height/2;
  const out={
    准星中心:[Math.round(cx),Math.round(cy)],
    卡片:[Math.round(r.left),Math.round(r.top),Math.round(r.right),Math.round(r.bottom)],
    卡片宽高:[Math.round(r.width),Math.round(r.height)],
    水平间隙:Math.round(r.left-cx),
    竖直间隙:Math.round(cy-r.bottom),
    压住准星了吗:!(r.left>cx||r.right<cx||r.top>cy||r.bottom<cy)?'压住了 ✗':'没有 ✓',
    在画布内吗:(r.left>0&&r.right<innerWidth&&r.top>0&&r.bottom<innerHeight)?'是 ✓':'溢出了 ✗',
  };
  probe.remove();
  return JSON.stringify(out,null,1);
})()`))
ws.close()