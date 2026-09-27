/** 矮窗口下的逐页截图，用于人眼判断"底部够不够得到"。 */
import { writeFileSync } from 'node:fs'

const HEIGHT = Number(process.argv[2] || 520)
const WIDTH = Number(process.argv[3] || 1280)
const PORT = 9222
const APP = 'http://127.0.0.1/'
const ROUTES = [['/login', 'login'], ['/parse', 'parse'], ['/generate', 'generate'],
                ['/review', 'review'], ['/knowledge', 'knowledge']]

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

await send('Page.enable')
await send('Runtime.enable')
await send('Emulation.setDeviceMetricsOverride',
  { width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false })
await send('Page.navigate', { url: APP + 'login' })
await sleep(1500)
await ev(`(async()=>{
  const r=await fetch('/api/v1/auth/login',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:'admin',password:'admin123'})});
  const j=await r.json();
  localStorage.setItem('qy.access_token', j.data.access_token);
  return true;})()`)

for (const [route, name] of ROUTES) {
  await send('Page.navigate', { url: APP.replace(/\/$/, '') + route })
  await sleep(2600)
  const s = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(`logs/_short-${name}.png`, Buffer.from(s.data, 'base64'))
  console.log(`logs/_short-${name}.png`)
}
ws.close()
