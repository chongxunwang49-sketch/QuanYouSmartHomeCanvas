/**
 * 两条硬要求的实测：
 *   ① 登录页图墙滚动时，左上角品牌行**不许闪**（连拍多张，逐张比那一块的像素）
 *   ② 侧栏子菜单全展开时，底部账号按钮**必须还能点到**（命中测试 + 可滚动）
 *
 * 用法：先起 9222 的 Edge，再 node scripts/_verify_ui.mjs
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

// ── ① 登录页：品牌行区域连拍 ──
await send('Page.navigate', { url: APP + 'login' })
await sleep(1200)
await ev('localStorage.clear(); sessionStorage.clear();')
await send('Page.navigate', { url: APP + 'login' })
await sleep(3500)

const box = await ev(`(()=>{const b=[...document.querySelectorAll('section div')]
  .find(d=>/全友 · 智绘家/.test(d.textContent||''));
  const r=b.getBoundingClientRect();
  return JSON.stringify({x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height)});})()`)
const b = JSON.parse(box)
const clip = { x: 0, y: 0, width: 420, height: b.y + b.height + 10, scale: 1 }
console.log('品牌行区域:', b, '裁切:', clip)

for (let i = 0; i < 5; i++) {
  const s = await send('Page.captureScreenshot', { format: 'png', clip })
  writeFileSync(`logs/_brand-${i}.png`, Buffer.from(s.data, 'base64'))
  await sleep(900)
}
console.log('连拍 5 张 → logs/_brand-0..4.png')

// ── ② 侧栏：展开全部子菜单，看账号按钮还能不能点 ──
const login = await ev(`(async()=>{
  const r=await fetch('/api/v1/auth/login',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:'admin',password:'admin123'})});
  const j=await r.json();
  localStorage.setItem('qy.access_token', j.data.access_token);
  return j.code===0;})()`)
console.log('登录 admin:', login)
await send('Page.navigate', { url: APP })
await sleep(3500)

// 逐个点开带子菜单的一级项的箭头
const expanded = await ev(`(async()=>{
  const btns=[...document.querySelectorAll('aside button[aria-expanded]')];
  const before=btns.filter(b=>b.getAttribute('aria-expanded')==='true').length;
  for(const b of btns){ if(b.getAttribute('aria-expanded')!=='true'){ b.click(); await new Promise(r=>setTimeout(r,120)); } }
  await new Promise(r=>setTimeout(r,400));
  const after=[...document.querySelectorAll('aside button[aria-expanded]')]
    .filter(b=>b.getAttribute('aria-expanded')==='true').length;
  return JSON.stringify({before, after,
    一级项:document.querySelectorAll('aside button[aria-expanded]').length,
    二级项:document.querySelectorAll('aside .nav-subitem, aside .nav-subitem-active').length});
})()`)
console.log('展开全部子菜单:', expanded)
await sleep(500)

const probe = await ev(`(()=>{
  const aside=document.querySelector('aside');
  const scroller=[...aside.querySelectorAll('div')].find(d=>d.scrollHeight>d.clientHeight+2 && /overflow-y/.test(getComputedStyle(d).overflowY));
  const acct=[...aside.querySelectorAll('button')].find(b=>/查看系统信息/.test(b.getAttribute('title')||''));
  const r=acct.getBoundingClientRect();
  const hit=document.elementFromPoint(r.x+r.width/2, r.y+r.height/2);
  return JSON.stringify({
    侧栏高:aside.clientHeight,
    可滚区:scroller?{client:scroller.clientHeight, scroll:scroller.scrollHeight,
                     overflowY:getComputedStyle(scroller).overflowY}:null,
    账号按钮:{y:Math.round(r.y),h:Math.round(r.height),
             在视口内:r.bottom<=innerHeight && r.top>=0},
    命中测试通过: acct.contains(hit) || hit===acct,
    命中到的是: (hit&&hit.className||'').toString().slice(0,60),
  });})()`)
console.log('侧栏实测:', probe)

const s2 = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync('logs/_sidebar-expanded.png', Buffer.from(s2.data, 'base64'))
console.log('截图 → logs/_sidebar-expanded.png')

// 收起全部子菜单，确认"装得下时不出滚动条"
const collapsed = await ev(`(async()=>{
  const btns=[...document.querySelectorAll('aside button[aria-expanded]')];
  for(const b of btns){ if(b.getAttribute('aria-expanded')==='true'){ b.click(); await new Promise(r=>setTimeout(r,120)); } }
  await new Promise(r=>setTimeout(r,400));
  const aside=document.querySelector('aside');
  const scroller=[...aside.querySelectorAll('div')].find(d=>/overflow-y/.test(getComputedStyle(d).overflowY) && getComputedStyle(d).overflowY!=='visible');
  return JSON.stringify({装得下:scroller?scroller.scrollHeight<=scroller.clientHeight+2:null,
    scroll:scroller&&scroller.scrollHeight, client:scroller&&scroller.clientHeight});})()`)
console.log('收起后:', collapsed)
ws.close()
