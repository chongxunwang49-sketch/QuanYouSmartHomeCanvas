/**
 * 量「方案 · 3D 装修漫游」这一页：从导航到场景画出来用了多久，时间花在哪。
 *
 * 为什么量这个页：它是两个 3D 页里更重的一个（多一次家具请求 + 家具几何）。
 * 解析页用的是同一个 `SceneViewer`，少一次家具请求，只会更快。
 *
 * 就绪判据：canvas 出现 **且** 「正在准备 3D 场景…」那行字消失。
 *
 * 用法（先起带 9222 的 Edge）：
 *   D:/VibeCoding/NodeJS/node.exe scripts/probes/_probe3d_timing.mjs [layoutId] [planId]
 */
import { writeFileSync } from 'node:fs'

const LAYOUT = process.argv[2] || 'layout_20260927_4fb0cc'
const PLAN = process.argv[3] || 'plan_modern_economy'
const PORT = 9222
const APP = 'http://127.0.0.1/'

const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
const page = list.find((t) => t.type === 'page')
const ws = new WebSocket(page.webSocketDebuggerUrl)
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
await sleep(2500)
const boot = await ev(`(async()=>{
  const r = await fetch('/api/v1/auth/login',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:'vip',password:'vip123'})});
  const j = await r.json();
  if (j.code!==0) return 'ERR:'+j.msg;
  localStorage.setItem('qy.access_token', j.data.access_token);
  sessionStorage.setItem('qy.generate.selected.${LAYOUT}', '${PLAN}');
  return 'ok';
})()`)
console.log('准备:', boot)

const READY = `Boolean(document.querySelector('canvas'))
  && !document.body.innerText.includes('正在准备 3D 场景')`

const t0 = Date.now()
await send('Page.navigate', { url: `${APP}generate/walkthrough?layout=${LAYOUT}&task=task_20260927_e1b871b4` })
let ready = -1
for (let i = 0; i < 400; i++) {
  await sleep(100)
  try { if (await ev(READY)) { ready = Date.now() - t0; break } } catch {}
}
console.log(ready >= 0 ? `\n画出来用了 ${ready} ms` : '\n30s 内没画出来')

const perf = await ev(`JSON.stringify(
  performance.getEntriesByType('resource')
    .filter(e=>/walkable|furniture|three|SceneViewer|index-|vendor/.test(e.name))
    .map(e=>({n:e.name.split('/').pop().slice(0,40), ms:Math.round(e.duration),
              kb:Math.round((e.transferSize||e.decodedBodySize||0)/1024)}))
    .sort((a,b)=>b.ms-a.ms).slice(0,10))`)
console.log('主要请求:', perf)

const shape = await ev(`(()=>{const c=document.querySelector('canvas');
  if(!c) return '无 canvas';
  const txt=document.body.innerText;
  return JSON.stringify({canvas:c.width+'x'+c.height,
    有家具提示:/摆不下|件家具|家具/.test(txt),
    可见文案:txt.replace(/\\s+/g,' ').slice(0,180)});})()`)
console.log('页面状态:', shape)

const shot = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync('logs/_timing-generate.png', Buffer.from(shot.data, 'base64'))
console.log('截图 → logs/_timing-generate.png')
ws.close()
