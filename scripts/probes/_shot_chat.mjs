/** 智友问答页的截图（顺便确认侧栏里那一项在"方案生成"与"知识库管理"之间）。 */
import { writeFileSync } from 'node:fs'

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
  sessionStorage.setItem('qy.generate.selected.layout_20260927_4fb0cc','plan_modern_economy');
  return true;})()`)

await send('Page.navigate', { url: APP + 'chat' })
for (let i = 0; i < 50; i++) { await sleep(160); if (i % 5 === 0) await frame() }
await sleep(800)

console.log('侧栏菜单顺序:', await ev(`JSON.stringify(
  [...document.querySelectorAll('aside a')].map(a=>a.textContent.trim()).filter(Boolean).slice(0,8))`))
console.log('页面文案:', (await ev('document.body.innerText.replace(/\\s+/g," ").slice(0,220)')))
const s = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync('logs/_chat.png', Buffer.from(s.data, 'base64'))
console.log('截图 → logs/_chat.png')
ws.close()
