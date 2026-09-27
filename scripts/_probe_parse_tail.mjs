/**
 * 量「后端跑完 → 界面显示完成」之间的尾巴有多长。
 *
 * 这是需求方说的那段等待：跑完之后屏幕上还写着"解析中…"、入口还没出现。
 *
 * 做法：真的跑一次解析（上传演示户型图），脚本**自己**每 300ms 直接问一次后端，
 * 记下后端第一次报 completed 的时刻；同时盯着界面出现「识别完成」的时刻。
 * 两者之差就是那段尾巴 —— 与轮询间隔直接相关。
 *
 * 用法: node scripts/_probe_parse_tail.mjs [图片路径]
 */
import { writeFileSync } from 'node:fs'

const IMG = process.argv[2] || 'C:/Users/DELL/Desktop/全友·智绘家QuanYou Smart HomeCanvas/演示素材/户型图/03-三室两厅-98平.png'
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
await send('DOM.enable')
await send('Emulation.setDeviceMetricsOverride',
  { width: 1680, height: 1000, deviceScaleFactor: 1, mobile: false })

await send('Page.navigate', { url: APP + 'login' })
await sleep(1500)
const token = await ev(`(async()=>{
  const r=await fetch('/api/v1/auth/login',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:'vip',password:'vip123'})});
  const j=await r.json();
  localStorage.setItem('qy.access_token', j.data.access_token);
  return j.data.access_token;})()`)

await send('Page.navigate', { url: APP + 'parse' })
await sleep(3000)

// 塞文件进那个隐藏的 input
const doc = await send('DOM.getDocument', { depth: -1 })
const node = await send('DOM.querySelector', {
  nodeId: doc.root.nodeId, selector: 'input[type=file]',
})
console.log('文件输入框 nodeId:', node.nodeId)
await send('DOM.setFileInputFiles', { nodeId: node.nodeId, files: [IMG] })
await sleep(1200)
console.log('已选图:', await ev(`document.body.innerText.match(/[^\\n]*\\.(png|jpg|jpeg)[^\\n]*/i)?.[0] || '（没看到文件名）'`))

// 记下这次解析的 task_id：从地址栏 query 里读
const t0 = Date.now()
await ev(`(()=>{const b=[...document.querySelectorAll('button')]
  .find(x=>/开始解析/.test(x.textContent||'')); b && b.click(); return true;})()`)

let taskId = ''
for (let i = 0; i < 60 && !taskId; i++) {
  await sleep(300)
  taskId = await ev(`new URLSearchParams(location.search).get('task') || ''`)
}
console.log('task_id:', taskId || '（没拿到）')
if (!taskId) { ws.close(); process.exit(1) }

// 并行的两条线：我自己问后端 / 界面上的完成标志
async function askBackend() {
  const r = await ev(`(async()=>{
    const t=localStorage.getItem('qy.access_token');
    const r=await fetch('/api/v1/task/${taskId}/status',{headers:{Authorization:'Bearer '+t}});
    const j=await r.json();
    return JSON.stringify({status:j.data&&j.data.status, eta:j.data&&j.data.eta_seconds, phase:j.data&&j.data.phase_text});})()`)
  return JSON.parse(r)
}

let backendDoneAt = -1
let backendEta = null
/**
 * 界面侧的就绪信号用**路由跳转**判定，不用文案。
 *
 * 解析成功后应用会自己 `router.push('/parse/overview')` —— 那是应用在
 * 「轮询看到 completed」那一瞬间做的事，正是要量的时刻。
 * 起初这里找的是页面上「识别完成」四个字，结果永远找不到：
 * 那是**上传页**的卡片文案，而跳转之后我们已经在「识别总览」页了。
 */
const uiDone = (async () => {
  for (let i = 0; i < 1800; i++) {
    await sleep(100)
    try {
      const p = await ev('location.pathname')
      if (p && p.includes('/parse/overview')) return Date.now()
    } catch { /* 切页忽略 */ }
  }
  return -1
})()

while (Date.now() - t0 < 180_000) {
  const s = await askBackend()
  if (s.eta !== null && s.eta !== undefined) backendEta = s.eta
  if (s.status === 'completed' || s.status === 'failed') {
    backendDoneAt = Date.now()
    break
  }
  await sleep(300)
}
const uiAt = await uiDone
console.log(`\n后端报完成: +${((backendDoneAt - t0) / 1000).toFixed(1)}s（后端 ETA 约 ${backendEta}s）`)
console.log(`界面显示完成: +${((uiAt - t0) / 1000).toFixed(1)}s`)
console.log(`→ 尾巴（界面落后后端）: ${uiAt > 0 && backendDoneAt > 0 ? ((uiAt - backendDoneAt) / 1000).toFixed(1) + ' s' : '未测到'}`)

const s = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync('logs/_parse-tail.png', Buffer.from(s.data, 'base64'))
ws.close()
