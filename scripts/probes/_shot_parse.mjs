/**
 * 户型解析页（上传页）两种状态的截图：等待中 / 已有识别结果。
 * 顺带量一下左右两栏的高度差 —— 「右侧大片空洞」就是它。
 *
 * 用法: node scripts/probes/_shot_parse.mjs [layoutId] [parseTaskId]
 */
import { writeFileSync } from 'node:fs'

const LAYOUT = process.argv[2] || 'layout_20260927_4fb0cc'
const TASK = process.argv[3] || 'task_20260927_322241e4'
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
const ok = await ev(`(async()=>{
  const r=await fetch('/api/v1/auth/login',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:'vip',password:'vip123'})});
  const j=await r.json();
  localStorage.setItem('qy.access_token', j.data.access_token);
  return j.code===0;})()`)
console.log('登录:', ok)

const measure = `(()=>{
  const grid=document.querySelector('main .grid');
  if(!grid) return '没有找到两栏网格';
  const cols=[...grid.children];
  const h=cols.map(c=>Math.round(c.getBoundingClientRect().height));
  const r=grid.getBoundingClientRect();
  return JSON.stringify({左栏高:h[0], 右栏高:h[1], 高度差:h[0]-h[1],
    网格高:Math.round(r.height), 视口:innerHeight});})()`

async function shot(label, url) {
  await send('Page.navigate', { url })
  await sleep(3500)
  const m = await ev(measure)
  console.log(`\n【${label}】${m}`)
  const s = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(`logs/_parse-${label}.png`, Buffer.from(s.data, 'base64'))
  console.log(`  截图 → logs/_parse-${label}.png`)
}

await shot('waiting', APP + 'parse')
await shot('with-result', `${APP}parse?layout=${LAYOUT}&task=${TASK}`)
ws.close()
