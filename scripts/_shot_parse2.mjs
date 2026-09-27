/**
 * 户型解析页（上传页）两个状态的截图 + 两栏底边是否对齐的实测。
 *
 * 用法: node scripts/_shot_parse2.mjs [layoutId] [parseTaskId]
 */
import { writeFileSync } from 'node:fs'

const LAYOUT = process.argv[2] || 'layout_20260927_4fb0cc'
const TASK = process.argv[3] || 'task_20260927_743019a3'
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
  if (r.exceptionDetails) throw new Error('JS: ' + (r.exceptionDetails.exception?.description || '').slice(0, 160))
  return r.result.value
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const frame = () => send('Page.captureScreenshot', { format: 'jpeg', quality: 30 })

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
  return true;})()`)

// 两栏底边对齐的实测
const measure = `(()=>{
  const grid=document.querySelector('main .grid');
  const cols=[...grid.children].map(c=>{const r=c.getBoundingClientRect();
    return {top:Math.round(r.top), bottom:Math.round(r.bottom), h:Math.round(r.height)};});
  const cards=[...grid.children].map(c=>c.querySelectorAll(':scope > .card').length);
  return JSON.stringify({左栏:cols[0], 右栏:cols[1],
    底边差:Math.abs(cols[0].bottom-cols[1].bottom), 各栏卡片数:cards});})()`

async function shot(label, url) {
  await send('Page.navigate', { url })
  for (let i = 0; i < 60; i++) { await sleep(150); if (i % 5 === 0) await frame() }
  await sleep(600)
  console.log(`\n【${label}】`)
  console.log('  ', await ev(measure))
  const s = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(`logs/_parse2-${label}.png`, Buffer.from(s.data, 'base64'))
  console.log(`  截图 → logs/_parse2-${label}.png`)
}

await shot('result', `${APP}parse?layout=${LAYOUT}&task=${TASK}`)
ws.close()
