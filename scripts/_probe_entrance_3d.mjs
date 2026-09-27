/**
 * 把 3D 相机搬到**入户门正对面**，照一张：那面外墙上到底有没有一个门洞。
 *
 *     node scripts/_probe_entrance_3d.mjs <parse_task_id> [outName]
 *
 * 起因：需求方说"3D 小屋没有通外的大门，而平面图上有"。几何那边量出来
 * 入户门是有洞的（`_probe_entrance_door.py` ③），所以要眼见为实：
 * 相机从房子外面正对入户门所在的外墙，看那面墙上是不是真的通了。
 *
 * 用 `window.__qy3d` 这个调试句柄直接摆相机（SceneViewer 里挂的）。
 */
import { writeFileSync } from 'node:fs'

const TASK = process.argv[2]
const NAME = process.argv[3] || 'entrance'
if (!TASK) throw new Error('用法：node scripts/_probe_entrance_3d.mjs <parse_task_id> [outName]')

const ROOT = 'C:/Users/DELL/Desktop/全友·智绘家QuanYou Smart HomeCanvas'
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
const ev = async (e) => (await send('Runtime.evaluate', {
  expression: e, awaitPromise: true, returnByValue: true })).result?.value
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const frame = () => send('Page.captureScreenshot', { format: 'jpeg', quality: 30 })
const shot = async (name) => {
  const s = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(`${ROOT}/logs/_3d-${name}.png`, Buffer.from(s.data, 'base64'))
  console.log(`  截图 → logs/_3d-${name}.png`)
}

await send('Page.enable')
await send('Runtime.enable')
await send('Emulation.setDeviceMetricsOverride',
  { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })

await send('Page.navigate', { url: APP + 'login' })
await sleep(1500)
await ev(`(async()=>{
  const r=await fetch('/api/v1/auth/login',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:'vip',password:'vip123'})});
  const j=await r.json();
  localStorage.setItem('qy.access_token', j.data.access_token);
  sessionStorage.clear();
  sessionStorage.setItem('qy.poll.parse', '${TASK}');
  return true;})()`)

await send('Page.navigate', { url: APP + 'parse/walkthrough' })
for (let i = 0; i < 40; i++) { await sleep(500); await frame() }
await sleep(1500)
console.log('页面文案：', await ev('document.body.innerText.replace(/\\s+/g," ").slice(0,150)'))
console.log('有调试句柄：', await ev('!!window.__qy3d'))

// 入户门的位置（来自 _probe_entrance_door.py 的 ①/② 表）—— 直接读页面上的 walkable
const info = await ev(`(()=>{
  const h=window.__qy3d; if(!h) return {err:'没有 __qy3d'};
  return {doors: (h.handles.walk?.doors||[]).map(d=>[d.index, d.passable, d.position[0], d.position[1], d.width_m]),
          rooms: (h.handles.walk?.rooms||[]).length,
          mode: h.handles.walk?.ok ? 'walk' : 'fly'};})()`)
console.log('walkable:', JSON.stringify(info))

// 把相机搬到入户门正对面（外墙法向朝外 4m 处），看向门
const placed = await ev(`(()=>{
  const h=window.__qy3d; if(!h) return 'no handle';
  const doors=h.handles.walk?.doors||[];
  const ent = doors.find(d=>!d.passable) || doors[0];
  if(!ent) return 'no door';
  // 场景坐标 (x,y) → three 的世界坐标，用与 scene.ts 相同的换算：x→x, y→z
  const x=ent.position[0], y=ent.position[1];
  // 找离门最近的墙法向，决定从哪一侧看
  const n=ent.normal||[0,1];
  const cam=h.renderer.domElement ? h.THREE : null;
  const camObj = h.scene.children.find(o=>o.isCamera) || null;
  // SceneViewer 用的相机在 rig 上
  const c = h.rig && h.rig.camera ? h.rig.camera : camObj;
  if(!c) return 'no camera';
  const dist=4.5, hgt=1.6;
  c.position.set(x + n[0]*dist, hgt, y + n[1]*dist);
  c.lookAt(x, 1.0, y);
  return JSON.stringify({door:[x,y], normal:n, camPos:[c.position.x,c.position.y,c.position.z]});
})()`)
console.log('相机：', placed)
for (let i = 0; i < 6; i++) { await sleep(300); await frame() }
await shot(`${NAME}-outside`)

// 再来一张从室内往入户门看的
await ev(`(()=>{
  const h=window.__qy3d; const doors=h.handles.walk.doors||[];
  const ent = doors.find(d=>!d.passable) || doors[0];
  const x=ent.position[0], y=ent.position[1], n=ent.normal||[0,1];
  const c = h.rig && h.rig.camera ? h.rig.camera : null;
  if(!c) return 'no camera';
  c.position.set(x - n[0]*3.0, 1.6, y - n[1]*3.0);
  c.lookAt(x, 1.0, y);
  return true;})()`)
for (let i = 0; i < 6; i++) { await sleep(300); await frame() }
await shot(`${NAME}-inside`)
ws.close()
