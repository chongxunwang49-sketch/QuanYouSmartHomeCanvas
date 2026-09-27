/**
 * 先进漫游（点一下画布），再按 R 回俯瞰位，然后截图。
 * 上一版没点画布 —— 界面上还盖着"点击进入漫游"，键事件被忽略了。
 */
import { writeFileSync } from 'node:fs'

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
const ev = async (e) => {
  const r = await send('Runtime.evaluate', { expression: e, awaitPromise: true, returnByValue: true })
  return r.result?.value
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// 画布位置
const box = await ev(`(()=>{const c=document.querySelector('canvas'); const r=c.getBoundingClientRect();
  return JSON.stringify({x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2)});})()`)
const { x, y } = JSON.parse(box)
console.log('画布中心:', x, y)

// 点一下进入漫游
for (const type of ['mousePressed', 'mouseReleased']) {
  await send('Input.dispatchMouseEvent', { type, x, y, button: 'left', clickCount: 1 })
}
await sleep(800)
console.log('进入漫游后文案:', (await ev(`document.body.innerText.replace(/\\s+/g,' ').slice(180,340)`)))

// 按 R 回俯瞰位
for (const t of ['rawKeyDown', 'keyUp']) {
  await send('Input.dispatchKeyEvent', { type: t, windowsVirtualKeyCode: 82, code: 'KeyR', key: 'r' })
}
await sleep(1500)
const s = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync('logs/_furniture-top2.png', Buffer.from(s.data, 'base64'))
console.log('截图 → logs/_furniture-top2.png')
ws.close()
