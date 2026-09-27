/**
 * 验证「鼠标指到家具上，右上方弹出它的名字」。
 *
 * ⚠️ 两个实测踩过的点：
 *   ① **要强制出帧。** 建场景挂在 `requestAnimationFrame` 上，而 Edge 窗口被挡住时
 *      rAF 不跑 —— 光等着，句柄 60 秒都不出现；定时截一张图立刻就好。
 *   ② **别靠「投影 + 祈祷它在画面里」**。直接把相机挪到某件家具正上方俯视它：
 *      家具必然落在画面正中，射线一定打得到。
 *
 * 判据：鼠标移过去之后，DOM 里出现一张写着**这件家具名字**的卡片。
 *
 * 用法: node scripts/probes/_probe_furniture_card.mjs [layoutId] [planId] [taskId]
 */
import { writeFileSync } from 'node:fs'

const LAYOUT = process.argv[2] || 'layout_20260927_4fb0cc'
const PLAN = process.argv[3] || 'plan_modern_economy'
const TASK = process.argv[4] || ''
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
    expression: expr, awaitPromise: true, returnByValue: true })
  if (r.exceptionDetails) {
    throw new Error('JS: ' + (r.exceptionDetails.exception?.description || '').slice(0, 160))
  }
  return r.result.value
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const frame = () => send('Page.captureScreenshot', { format: 'jpeg', quality: 20 })

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

const url = `${APP}generate/walkthrough?layout=${LAYOUT}` + (TASK ? `&task=${TASK}` : '')
await send('Page.navigate', { url })

const t0 = Date.now()
let ready = false
for (let i = 0; i < 200; i++) {
  await sleep(150)
  if (i % 7 === 0) await frame()
  try { if (await ev('Boolean(window.__qy3d)')) { ready = true; break } } catch { /* 导航中 */ }
}
console.log(`调试句柄就绪: ${ready}（等了 ${((Date.now() - t0) / 1000).toFixed(1)}s）`)
if (!ready) { console.log('❌ 3D 一直没建起来'); ws.close(); process.exit(1) }

// 把相机挪到某件家具正上方俯视它
const aim = await ev(`(()=>{
  const d=window.__qy3d, THREE=d.THREE;
  let target=null;
  d.scene.traverse(o=>{
    if(target) return;
    if(!o.isMesh || !o.userData || !o.userData.furniture) return;
    const w=new THREE.Vector3(); o.getWorldPosition(w);
    target={label:o.userData.furniture.label, spec:o.userData.furniture.spec_id,
            wx:w.x, wz:w.z, wy:w.y};
  });
  if(!target) return 'null';
  const rig=d.rig;
  rig.px = target.wx;          // 户型坐标 = (x, -z)，见 three/coords 的 planToEngine
  rig.py = -target.wz;
  rig.flyHeight = target.wy + 3.5;
  rig.pitch = -Math.PI/2 + 0.05;   // 近乎正俯视
  return JSON.stringify(target);})()`)
if (aim === 'null') { console.log('❌ 场景里没有家具 mesh'); ws.close(); process.exit(1) }
const target = JSON.parse(aim)
console.log('瞄准的家具:', target.label, `(${target.spec})`)

for (let i = 0; i < 20; i++) { await sleep(100); await frame() }

const center = JSON.parse(await ev(`(()=>{
  const r=document.querySelector('canvas').getBoundingClientRect();
  return JSON.stringify({x:Math.round(r.left+r.width/2), y:Math.round(r.top+r.height/2)});})()`))

await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: center.x, y: center.y, button: 'none' })
await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: center.x + 1, y: center.y + 1, button: 'none' })
await sleep(600)
await frame()

const card = await ev(`(()=>{
  const hit=[...document.querySelectorAll('div')]
    .filter(e=>{const t=(e.textContent||'').replace(/\\s+/g,' ');
      // 卡片的形状：一行"房名 · W×Dm · 高 Hm"，整体很短（假设面板那种几百字的不算）
      return /·\\s*高 [0-9.]+m/.test(t) && t.length < 200;})
    .map(e=>(e.textContent||'').replace(/\\s+/g,' ').trim());
  return JSON.stringify({找到卡片: hit.length>0, 卡片文字: hit.slice(0,1)});})()`)
const parsed = JSON.parse(card)
const ok = parsed.找到卡片 && parsed.卡片文字.join(' ').includes(target.label)
// ⚠️ 判据要**具体到这张卡**，不能用「含『高 x』」这种宽正则 ——
//    页面上别处（户型假设面板）也有「层高 2.8m」，会误判成"找到了卡片"。
//    实测就被这个假阳性坑过一次：截图里卡片明明在，脚本却报 ❌。
console.log('\n卡片:', card)
console.log(ok
  ? `✅ 鼠标指到家具时弹出了「${target.label}」的说明卡`
  : '❌ 没弹出卡片（或卡片里不是这件家具）')

const s = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync('logs/_furniture-card.png', Buffer.from(s.data, 'base64'))
console.log('截图 → logs/_furniture-card.png')
ws.close()
