/**
 * 静态量法（不依赖合成输入）：页面底部的按钮**能不能被滚进视野**。
 *
 * 判据：滚动容器自己的底边必须在视口内。
 * 如果 `scroller.bottom > innerHeight`，那么容器最下面那一段**永远滚不出来** ——
 * 表现就是"滑轮滑到底了，按钮还差一截、按不到"。
 *
 * 另外查三件事：
 *   · 有没有祖先盖住了它（祖先 overflow:hidden 且高度小于容器）
 *   · 容器内部最后 8px 处是否真的能命中内容（而不是压在别的容器下面）
 *   · 侧栏那种「自己滚的列」是否与主内容区相互独立
 *
 * 用法: node scripts/_probe_bottom.mjs [视口高]
 */
import { writeFileSync } from 'node:fs'

const HEIGHT = Number(process.argv[2] || 560)
const PORT = 9222
const APP = 'http://127.0.0.1/'
const ROUTES = [
  ['/dashboard', '工作台'], ['/parse', '解析·上传'], ['/parse/overview', '解析·总览'],
  ['/generate', '方案·参数'], ['/generate/plans', '方案·三选一'],
  ['/generate/walkthrough', '方案·3D'], ['/parse/walkthrough', '解析·3D'],
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
  { width: 1280, height: HEIGHT, deviceScaleFactor: 1, mobile: false })

await send('Page.navigate', { url: APP + 'login' })
await sleep(1500)
await ev(`(async()=>{
  const r=await fetch('/api/v1/auth/login',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:'admin',password:'admin123'})});
  const j=await r.json();
  localStorage.setItem('qy.access_token', j.data.access_token);
  return true;})()`)

console.log(`视口 1280×${HEIGHT}\n`)
const bad = []
for (const [route, name] of ROUTES) {
  const url = APP.replace(/\/$/, '') + route +
    (route.includes('walkthrough') ? '?layout=layout_20260927_4fb0cc&task=task_20260927_e1b871b4' : '')
  await send('Page.navigate', { url })
  await sleep(2800)

  const r = await ev(`(()=>{
    const win = {h: innerHeight};
    // 所有"纵向可滚或本该可滚"的容器
    const all=[...document.querySelectorAll('main,div,section')];
    const scrollers=all.filter(e=>/auto|scroll/.test(getComputedStyle(e).overflowY));
    // 主内容区：scrollHeight 最大的那个
    const main=scrollers.filter(e=>e.scrollHeight>e.clientHeight+4)
      .sort((a,b)=>b.scrollHeight-a.scrollHeight)[0];
    if(!main) return JSON.stringify({没有可滚区:true});
    const mb=main.getBoundingClientRect();
    const need = main.scrollHeight - main.clientHeight;
    // 容器的底边是否在视口内（超出 → 底部永远滚不出来）
    const 底边 = Math.round(mb.bottom);
    // 内容里最后一个可点元素（排除侧栏）
    const aside=document.querySelector('aside');
    const clickables=[...document.querySelectorAll('button, a[href], [role=button]')]
      .filter(e=>!aside?.contains(e))
      .filter(e=>{const q=e.getBoundingClientRect(); return q.width>8&&q.height>8&&!e.disabled;});
    const last=clickables.length? clickables.reduce((a,c)=>
      a.getBoundingClientRect().bottom>c.getBoundingClientRect().bottom?a:c):null;
    // 会被祖先裁掉吗：任一祖先 overflow hidden 且高度小于 main.scrollHeight
    let clippedBy=null;
    for(let p=main.parentElement;p;p=p.parentElement){
      const s=getComputedStyle(p);
      if(/hidden|clip/.test(s.overflowY) && p.clientHeight < main.clientHeight-2){
        clippedBy=(p.className||'').toString().slice(0,30)+' clientH='+p.clientHeight; break;
      }
    }
    return JSON.stringify({
      容器高:main.clientHeight, 内容高:main.scrollHeight, 需滚动:need,
      容器底边:底边, 视口:win.h,
      底边在视口内: 底边<=win.h+1,
      被祖先裁:clippedBy,
      最后按钮: last? {文案:(last.textContent||'').trim().replace(/\\s+/g,' ').slice(0,14),
        相对容器底:(Math.round(mb.bottom - last.getBoundingClientRect().bottom))}:null,
    });})()`)
  let info
  try { info = JSON.parse(r) } catch { info = { err: String(r).slice(0, 70) } }
  const ok = info.底边在视口内 !== false && !info.没有可滚区 && !info.err
  if (!ok) bad.push([name, route, info])
  console.log(`${ok ? '✅' : '❌'} ${name.padEnd(10)} ${route.padEnd(22)} ${JSON.stringify(info).slice(0, 165)}`)
}
console.log(`\n底部够不到的页面：${bad.length} 个`)
bad.forEach(([n, r]) => console.log(`   - ${n} (${r})`))
const s = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync('logs/_bottom-check.png', Buffer.from(s.data, 'base64'))
ws.close()
