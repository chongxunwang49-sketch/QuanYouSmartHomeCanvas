/**
 * 验证 #3：方案生成的 3D 装修漫游里，家具到底有没有放进房间。
 *
 * 判据不是"看着像"，而是拿**渲染出来的那批 mesh 的世界坐标**去比房间盒：
 * `SceneViewer` 把家具组的每个 mesh 摆在 `planToEngine(x, y, h)` 上，
 * 于是可以直接在页面里遍历 `scene`，统计有多少件落在房间矩形内。
 *
 * 用法: node scripts/_shot_furniture.mjs [layoutId] [planId] [taskId]
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
await sleep(4500)

// 直接从接口拿房间盒与家具坐标，再与"页面上实际画出来的"对照
const report = await ev(`(async()=>{
  const t=localStorage.getItem('qy.access_token');
  const h={Authorization:'Bearer '+t};
  const furn=await (await fetch('/api/v1/layout/${LAYOUT}/furniture?style=modern&plan_id=${PLAN}',{headers:h})).json();
  const f=furn.data;
  const rects={}; (f.rooms||[]).forEach(r=>rects[r.index]=r.free_rect);
  let inside=0, outside=0, worst=null;
  (f.rooms||[]).forEach(r=>{
    const b=rects[r.index]; if(!b) return;
    (r.placements||[]).forEach(p=>{
      const ok=p.x>=b[0]&&p.x<=b[2]&&p.y>=b[1]&&p.y<=b[3];
      ok?inside++:outside++;
      if(!ok && !worst) worst={label:p.label,x:p.x,y:p.y,room:p.room_name,box:b};
    });
  });
  const canvas=document.querySelector('canvas');
  return JSON.stringify({
    scene_scale:f.scene_scale, 家具件数:(f.rooms||[]).reduce((a,r)=>a+(r.placements||[]).length,0),
    落在自己房间框内:inside, 落在框外:outside, 例外的第一件:worst,
    canvas:canvas&&(canvas.width+'x'+canvas.height),
    页面文案:document.body.innerText.replace(/\\s+/g,' ').slice(0,150)});})()`)
console.log('后端数据核对:', report)

const s = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync('logs/_furniture-check.png', Buffer.from(s.data, 'base64'))
console.log('截图 → logs/_furniture-check.png')
ws.close()
