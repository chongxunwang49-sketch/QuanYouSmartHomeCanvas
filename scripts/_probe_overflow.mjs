/**
 * 横向溢出检查：窄窗口下页面出现横向滚动条时，它会在底部**吃掉一条**，
 * 并且常常压住最下面那排按钮（Windows 上是经典滚动条，占 15~17px）。
 *
 * 同时检查：视口高很低时，`main` 的实际可滚高度是否与内容一致。
 *
 * 用法: node scripts/_probe_overflow.mjs [宽] [高]
 */
import { writeFileSync } from 'node:fs'

const WIDTH = Number(process.argv[2] || 1024)
const HEIGHT = Number(process.argv[3] || 560)
const PORT = 9222
const APP = 'http://127.0.0.1/'
const ROUTES = [['/dashboard', '工作台'], ['/parse', '解析·上传'], ['/generate', '方案·参数'],
                ['/generate/plans', '方案·三选一'], ['/review', '避坑审查'],
                ['/materials', '材料价格'], ['/analytics', '数据分析'],
                ['/knowledge', '知识库'], ['/users', '用户管理'], ['/profile', '个人中心']]

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

console.log(`视口 ${WIDTH}×${HEIGHT}\n`)
for (const [route, name] of ROUTES) {
  await send('Page.navigate', { url: APP.replace(/\/$/, '') + route })
  await sleep(2500)
  const r = await ev(`(()=>{
    const de=document.documentElement;
    const hOver = de.scrollWidth - de.clientWidth;
    // 谁溢出了
    let culprit=null;
    if(hOver>1){
      for(const e of document.querySelectorAll('main *')){
        const b=e.getBoundingClientRect();
        if(b.right > de.clientWidth + 1 && b.width>60){
          culprit={cls:(e.className||'').toString().slice(0,40), right:Math.round(b.right)}; break;
        }
      }
    }
    const main=document.querySelector('main');
    const mb=main?main.getBoundingClientRect():null;
    return JSON.stringify({
      横向溢出:hOver, 元凶:culprit,
      main可滚: main? main.scrollHeight-main.clientHeight : null,
      main底边: mb? Math.round(mb.bottom):null, 视口:innerHeight,
      滚动条宽: window.innerWidth - document.documentElement.clientWidth,
    });})()`)
  let info; try { info = JSON.parse(r) } catch { info = { err: String(r).slice(0,60) } }
  const bad = info.横向溢出 > 1 || (info.main底边 ?? 0) > HEIGHT + 1
  console.log(`${bad ? '❌' : '✅'} ${name.padEnd(10)} ${route.padEnd(20)} ${JSON.stringify(info).slice(0,170)}`)
}
const s = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync('logs/_overflow-check.png', Buffer.from(s.data, 'base64'))
ws.close()
