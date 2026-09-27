/**
 * 用 `window.__qy3d` 句柄核对**场景里真的画出来的东西**：
 *   · 相机开局在哪（高度 / 俯角）
 *   · 场景里有多少 mesh，其中带 `userData.furniture` 的有几件
 *   · 每件家具世界坐标在不在它自己那间房的矩形里（渲染侧，不是接口侧）
 * 最后把相机按到正俯视，截一张能一眼看全的图。
 *
 * 用法: node scripts/probes/_probe3d_scene.mjs [layoutId] [planId] [taskId]
 */
import { writeFileSync } from 'node:fs'

const LAYOUT = process.argv[2] || 'layout_20260927_4fb0cc'
const PLAN = process.argv[3] || 'plan_modern_economy'
const TASK = process.argv[4] || 'task_20260927_e1b871b4'
const PORT = 9222
const APP = 'http://127.0.0.1/'

const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
const ws = new WebSocket(list.find((t) => t.type === 'page').webSocketDebuggerUrl)
await new Promise((r, j) => {
  ws.addEventListener('open', r, { once: true })
  ws.addEventListener('error', j, { once: true })
})
let id = 0
const pend = new Map()
ws.addEventListener('message', (e) => {
  const m = JSON.parse(e.data)
  if (m.id && pend.has(m.id)) {
    const { res, rej } = pend.get(m.id)
    pend.delete(m.id)
    m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result)
  }
})
const send = (method, params = {}) =>
  new Promise((res, rej) => {
    const n = ++id
    pend.set(n, { res, rej })
    ws.send(JSON.stringify({ id: n, method, params }))
  })
const ev = async (expr) => {
  const r = await send('Runtime.evaluate', {
    expression: expr, awaitPromise: true, returnByValue: true,
  })
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || 'JS')
  return r.result.value
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

await send('Page.enable')
await send('Runtime.enable')
await send('Emulation.setDeviceMetricsOverride',
  { width: 1680, height: 1000, deviceScaleFactor: 1, mobile: false })

await send('Page.navigate', { url: APP + 'login' })
await sleep(1500)
await ev(`(async()=>{
  const r=await fetch('/api/v1/auth/login',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:'vip',password:'vip123'})});
  const j=await r.json();
  localStorage.setItem('qy.access_token', j.data.access_token);
  sessionStorage.setItem('qy.generate.selected.${LAYOUT}','${PLAN}');
  return true;})()`)

await send('Page.navigate', {
  url: `${APP}generate/walkthrough?layout=${LAYOUT}&task=${TASK}`,
})
for (let i = 0; i < 100; i++) {
  await sleep(150)
  if (await ev('Boolean(window.__qy3d)')) break
}
console.log('调试句柄就绪:', await ev('Boolean(window.__qy3d)'))

console.log('\n相机开局:', await ev(`(()=>{const d=window.__qy3d;
 const p=d.rig.camera.position, r=d.rig;
 return JSON.stringify({x:+p.x.toFixed(2), y:+p.y.toFixed(2), z:+p.z.toFixed(2),
   pitch_deg:+(r.pitch*180/Math.PI).toFixed(1), mode:r.mode,
   户型跨度:+(Math.max(d.handles.bounds.sizeX, d.handles.bounds.sizeZ)).toFixed(2)});})()`))

console.log('\n场景与家具:', await ev(`(()=>{const d=window.__qy3d;
 let mesh=0, furn=0, noUserData=0;
 const boxes={}; (d.furniture?.rooms||[]).forEach(r=>boxes[r.index]=r.free_rect);
 const outList=[];
 d.scene.traverse(o=>{ if(!o.isMesh) return; mesh++;
   const info=o.userData && o.userData.furniture;
   if(info){ furn++;
     const w=new d.THREE.Vector3(); o.getWorldPosition(w);
     const b=boxes[info.room_index] ||
       (d.furniture.rooms||[]).find(r=>r.name===info.room_name)?.free_rect;
     if(b){
       const tol=1.2;   // 家具自身尺寸 + 贴墙，给 1.2m 容差
       // ⚠️ 世界 z 与图纸 y **反号**（planToEngine: [x, h, -y]）。这里要换回来再比，
       //    否则每一件都会被判成"在房间外" —— 我第一次就踩了这个坑。
       const py = -w.z;
       const ok = w.x>=b[0]-tol && w.x<=b[2]+tol && py>=b[1]-tol && py<=b[3]+tol;
       if(!ok) outList.push({label:info.label, room:info.room_name,
         x:+w.x.toFixed(2), y:+(py).toFixed(2), box:b.map(v=>+v.toFixed(1))});
     } else noUserData++;
   }});
 return JSON.stringify({场景mesh总数:mesh, 带家具标记:furn, 无房间框:noUserData,
   明显在房间外:outList.length, 例子:outList.slice(0,3)});})()`))

// 强制正俯视，截一张看得全的图
await ev(`(()=>{const r=window.__qy3d.rig;
 r.flyHeight = Math.max(r.camera.position.y, 18);
 r.pitch = -Math.PI/2 + 0.02; return true;})()`)
await sleep(700)
const s = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync('logs/_3d-topdown.png', Buffer.from(s.data, 'base64'))
console.log('\n俯视截图 → logs/_3d-topdown.png')
ws.close()
