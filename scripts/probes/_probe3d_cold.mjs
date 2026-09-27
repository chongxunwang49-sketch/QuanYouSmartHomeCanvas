/**
 * 冷启动 + 帧率：判断「3D 慢」到底是**加载慢**还是**渲染慢**。
 *
 *   ① 清缓存后重新打开同一个页面，量到画出来为止（拆开看是哪个 chunk 在花时间）
 *   ② 画出来之后量 3 秒内的实际帧率（软件 WebGL 会明显低）
 *   ③ 读界面上自己报的 GPU / 统计文案
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
await send('Network.enable')
await send('Emulation.setDeviceMetricsOverride',
  { width: 1680, height: 1000, deviceScaleFactor: 1, mobile: false })

const boot = async () => {
  await send('Page.navigate', { url: APP + 'login' })
  await sleep(2000)
  return ev(`(async()=>{
    const r = await fetch('/api/v1/auth/login',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:'vip',password:'vip123'})});
    const j = await r.json();
    localStorage.setItem('qy.access_token', j.data.access_token);
    sessionStorage.setItem('qy.generate.selected.${LAYOUT}', '${PLAN}');
    return j.code===0 ? 'ok' : 'ERR';
  })()`)
}

const URL3D = `${APP}generate/walkthrough?layout=${LAYOUT}&task=${TASK}`
const READY = `Boolean(document.querySelector('canvas'))
  && !document.body.innerText.includes('正在准备 3D 场景')`

async function run(label) {
  const t0 = Date.now()
  await send('Page.navigate', { url: URL3D })
  let ready = -1
  for (let i = 0; i < 400; i++) {
    await sleep(100)
    try { if (await ev(READY)) { ready = Date.now() - t0; break } } catch {}
  }
  const res = await ev(`JSON.stringify(
    performance.getEntriesByType('resource')
      .filter(e=>/three|SceneViewer|index-|vendor|walkable|furniture/.test(e.name))
      .map(e=>({n:e.name.split('/').pop().slice(0,34),
                ms:Math.round(e.duration),
                kb:Math.round((e.transferSize||0)/1024)}))
      .sort((a,b)=>b.ms-a.ms).slice(0,8))`)
  console.log(`\n【${label}】${ready >= 0 ? `画出来 ${ready} ms` : '超时'}`)
  console.log('  ', res)
  return ready
}

console.log('登录:', await boot())

await run('热缓存（上次已访问）')

await send('Network.clearBrowserCache')
await send('Network.setCacheDisabled', { cacheDisabled: true })
await run('冷缓存（清空 + 禁用缓存）')
await send('Network.setCacheDisabled', { cacheDisabled: false })

// 帧率
const fps = await ev(`new Promise(res=>{
  let n=0; const t0=performance.now();
  function loop(){ n++; if (performance.now()-t0<3000) requestAnimationFrame(loop);
    else res(+(n/((performance.now()-t0)/1000)).toFixed(1)); }
  requestAnimationFrame(loop); setTimeout(()=>res(-1), 9000);})`)
console.log(`\nrAF 帧率（3 秒均值）: ${fps} fps`)

const gpuTxt = await ev(`(()=>{const t=document.body.innerText;
  const m=t.match(/(软件|WebGL|GPU|显卡)[^\\n]{0,90}/g);
  return JSON.stringify(m ? m.slice(0,4) : t.slice(0,200));})()`)
console.log('界面上的 GPU 相关文案:', gpuTxt)

const shot = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync('logs/_timing-cold.png', Buffer.from(shot.data, 'base64'))
console.log('截图 → logs/_timing-cold.png')
ws.close()
