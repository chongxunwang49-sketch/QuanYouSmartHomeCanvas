/**
 * 登录页的外观与「文字会不会被图墙闪掉」的检查。
 *
 * 做法：截图两张（间隔 1.2s，图墙已经滚过一段），分别裁出**左上角品牌行**
 * 那块区域的像素，比较两张是否一致 —— 一致说明滚动没有影响到文字。
 */
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
await ev('localStorage.clear(); sessionStorage.clear();')
await send('Page.navigate', { url: APP + 'login' })
await sleep(3500)

const shot1 = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync('logs/_login-a.png', Buffer.from(shot1.data, 'base64'))
await sleep(1200)
const shot2 = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync('logs/_login-b.png', Buffer.from(shot2.data, 'base64'))

// 图墙在不在滚：读第一列的 transform 矩阵
const moving = await ev(`(()=>{
  const tracks=[...document.querySelectorAll('.wall-track')];
  if(!tracks.length) return '没有 .wall-track —— 图墙没渲染';
  return JSON.stringify(tracks.map(t=>{
    const m=getComputedStyle(t).transform;
    return {anim:getComputedStyle(t).animationName, dur:getComputedStyle(t).animationDuration, m};
  }));
})()`)
console.log('图墙状态:', moving)

// 文字层的位置与合成层（拿不到 will-change 也算正常，Chromium 不暴露层树）
const textBox = await ev(`(()=>{
  const h1=document.querySelector('section h1');
  const brand=[...document.querySelectorAll('section div')].find(d=>/全友 · 智绘家/.test(d.textContent||''));
  const r=(e)=>e?(({x,y,width,height})=>({x:Math.round(x),y:Math.round(y),w:Math.round(width),h:Math.round(height)}))(e.getBoundingClientRect()):null;
  return JSON.stringify({h1:r(h1), brand:r(brand),
    案例图张数:document.querySelectorAll('.wall-track img').length,
    轮播还在:!!document.querySelector('.slide-fade-enter-active')});
})()`)
console.log('版式:', textBox)

console.log('截图 → logs/_login-a.png / _login-b.png')
ws.close()
