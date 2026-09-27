/**
 * 矮窗口下登录页左栏的三段（品牌行 / 标题 / 图片来源声明）会不会被裁掉。
 *
 * 起因：内容层从 `justify-between` 改成了"品牌行贴顶 + 标题紧跟 + 声明 mt-auto"，
 * 声明是靠自动外边距压到底的 —— 窗口一矮，自动外边距先被吃掉，
 * 再矮就轮到声明被裁。左栏本身是 `overflow-hidden`（图墙必须裁），裁掉就真看不见了。
 *
 *     node scripts/probes/_probe_login_short.mjs
 */
import { writeFileSync } from 'node:fs'

const ROOT = 'C:/Users/DELL/Desktop/全友·智绘家QuanYou Smart HomeCanvas'
const SIZES = [[1680, 1000], [1440, 800], [1280, 700], [1280, 600], [1280, 520]]
const PORT = 9222

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

await send('Page.enable')
await send('Runtime.enable')

for (const [w, h] of SIZES) {
  await send('Emulation.setDeviceMetricsOverride',
    { width: w, height: h, deviceScaleFactor: 1, mobile: false })
  await send('Page.navigate', { url: 'http://127.0.0.1/login' })
  await new Promise((r) => setTimeout(r, 1800))
  for (let i = 0; i < 8; i++) await send('Page.captureScreenshot', { format: 'jpeg', quality: 30 })
  const info = await ev(`(()=>{
    const left=document.querySelector('.login-left');
    if(!left) return {err:'左栏没渲染（宽度不足 lg？）'};
    const lb=left.getBoundingClientRect();
    const h1=left.querySelector('h1');
    const attr=[...left.querySelectorAll('p')].find(p=>/素材|Pexels/.test(p.textContent||''));
    const bt=(el)=>{const r=el.getBoundingClientRect();return {t:Math.round(r.top),b:Math.round(r.bottom)};};
    return {left:{t:Math.round(lb.top),b:Math.round(lb.bottom)}, h1:bt(h1),
      attr:attr?bt(attr):null,
      attrVisible: attr? attr.getBoundingClientRect().bottom<=lb.bottom+0.5 : null,
      h1Visible: h1.getBoundingClientRect().top>=lb.top-0.5};})()`)
  const tag = info?.err ? `  ✗ ${info.err}`
    : `  标题可见 ${info.h1Visible ? '✓' : '✗'} · 声明可见 ${info.attrVisible ? '✓' : '✗'}`
      + `（左栏 ${info.left.t}~${info.left.b}，标题 ${info.h1.t}~${info.h1.b}，`
      + `声明 ${info.attr?.t}~${info.attr?.b}）`
  console.log(`${w}×${h}${tag}`)
  if (h === 1000 || h === 600) {
    const s = await send('Page.captureScreenshot', { format: 'png' })
    writeFileSync(`${ROOT}/logs/_login-${w}x${h}.png`, Buffer.from(s.data, 'base64'))
  }
}
ws.close()
