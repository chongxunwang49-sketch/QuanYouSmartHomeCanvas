/**
 * 找「内容被裁掉、又没有滚动条」的容器 —— 这才是"滑轮滑不到最下面"的真凶。
 *
 * 判据：元素 `overflow-y: hidden|clip`，而 `scrollHeight > clientHeight + 8`。
 * 这类容器把超出的内容**永远藏起来**：里面最后一个按钮既滚不到、也点不到。
 *
 * 顺带统计：每个页面有多少个这样的容器、被藏了多少像素、最后一个被藏住的
 * 可点元素是什么（给定位用）。
 *
 * 用法: node scripts/probes/_probe_clip.mjs [视口高] [视口宽]
 */
import { writeFileSync } from 'node:fs'

const HEIGHT = Number(process.argv[2] || 560)
const WIDTH = Number(process.argv[3] || 1280)
const PORT = 9222
const APP = 'http://127.0.0.1/'
const ROUTES = [
  ['/login', '登录页'],
  ['/dashboard', '工作台'], ['/parse', '解析·上传'], ['/parse/overview', '解析·总览'],
  ['/generate', '方案·参数'], ['/generate/plans', '方案·三选一'],
  ['/review', '避坑审查'], ['/materials', '材料价格'], ['/analytics', '数据分析'],
  ['/knowledge', '知识库'], ['/users', '用户管理'], ['/profile', '个人中心'],
]

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
const bad = []
for (const [route, name] of ROUTES) {
  await send('Page.navigate', { url: APP.replace(/\/$/, '') + route })
  await sleep(2600)
  const r = await ev(`(()=>{
    const out=[];
    const cand=[...document.querySelectorAll('main, main *')];
    for(const e of cand){
      const s=getComputedStyle(e);
      if(!/hidden|clip/.test(s.overflowY)) continue;
      const over = e.scrollHeight - e.clientHeight;
      if(over <= 8) continue;
      // 里面有没有可点元素
      const btns=[...e.querySelectorAll('button, a[href], [role=button]')].length;
      if(!btns) continue;
      out.push({cls:(e.className||'').toString().slice(0,34), 藏住:over, 按钮数:btns});
    }
    return JSON.stringify(out.slice(0,4));})()`)
  let list2
  try { list2 = JSON.parse(r) } catch { list2 = [] }
  const flag = list2.length ? '❌' : '✅'
  if (list2.length) bad.push([name, route, list2])
  console.log(`${flag} ${name.padEnd(10)} ${route.padEnd(20)} 被裁容器 ${list2.length} 个 ${JSON.stringify(list2).slice(0, 150)}`)
}
console.log(`\n有内容被裁掉的页面：${bad.length} 个`)
for (const [n, r2, l] of bad) console.log(`   - ${n} (${r2}) → ${JSON.stringify(l)}`)
ws.close()
