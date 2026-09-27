/**
 * 量「从结果页点到 3D 画出来」这段等待 —— 这正是需求方说的"跑完到能点开 3D 之间的等待"。
 *
 * 三个口径分开量：
 *   ① 冷启动（清了缓存）：路由分块 + SceneViewer + three + 两个接口 + 建场景
 *   ② 热启动（同一会话再点一次）
 *   ③ 只把 three 那一段预热之后（模拟"悬停就预取"）
 *
 * 用法: node scripts/probes/_probe3d_click.mjs [layoutId] [parseTaskId]
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
await send('Network.enable')
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

const READY = `Boolean(document.querySelector('canvas'))
  && !document.body.innerText.includes('正在准备 3D 场景')`

/** 进结果页 → 点 3D 漫游 → 量到画出来 */
async function clickTo3D(label) {
  await send('Page.navigate', { url: `${APP}parse?layout=${LAYOUT}&task=${TASK}` })
  await sleep(3000)
  const t0 = Date.now()
  const clicked = await ev(`(()=>{
    const a=[...document.querySelectorAll('a')].find(x=>/3D 漫游/.test(x.textContent||''));
    if(!a) return '没找到 3D 漫游 入口';
    a.click(); return 'ok';})()`)
  if (clicked !== 'ok') { console.log(`【${label}】${clicked}`); return -1 }
  let ready = -1
  for (let i = 0; i < 300; i++) {
    await sleep(50)
    try { if (await ev(READY)) { ready = Date.now() - t0; break } } catch {}
  }
  const res = await ev(`JSON.stringify(performance.getEntriesByType('resource')
    .filter(e=>/three|SceneViewer|walkable|furniture|ParseWalkthrough/.test(e.name))
    .map(e=>({n:e.name.split('/').pop().slice(0,32), ms:Math.round(e.duration),
              kb:Math.round((e.transferSize||0)/1024)}))
    .sort((a,b)=>b.ms-a.ms).slice(0,6))`)
  console.log(`\n【${label}】点下去到画出来：${ready >= 0 ? ready + ' ms' : '超时'}`)
  console.log('  ', res)
  const s = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(`logs/_click3d-${label}.png`, Buffer.from(s.data, 'base64'))
  return ready
}

await send('Network.clearBrowserCache')
await send('Network.setCacheDisabled', { cacheDisabled: true })
const cold = await clickTo3D('冷缓存')
await send('Network.setCacheDisabled', { cacheDisabled: false })
const warm = await clickTo3D('热缓存')
console.log(`\n汇总：冷 ${cold} ms / 热 ${warm} ms`)
ws.close()
