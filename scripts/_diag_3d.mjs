/** 3D 页打不开时的现场诊断：路由、句柄、页面文案、控制台报错。 */
const PORT = 9222
const APP = 'http://127.0.0.1/'
const URL3D = process.argv[2] ||
  `${APP}generate/walkthrough?layout=layout_20260927_4fb0cc&task=task_20260927_e1b871b4`

const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
const ws = new WebSocket(list.find((t) => t.type === 'page').webSocketDebuggerUrl)
await new Promise((r, j) => {
  ws.addEventListener('open', r, { once: true })
  ws.addEventListener('error', j, { once: true })
})
let id = 0
const pend = new Map()
const logs = []
ws.addEventListener('message', (e) => {
  const m = JSON.parse(e.data)
  if (m.method === 'Runtime.consoleAPICalled') {
    logs.push('[console] ' + (m.params.args || []).map((a) => a.value ?? a.description).join(' ').slice(0, 160))
  }
  if (m.method === 'Runtime.exceptionThrown') {
    logs.push('[异常] ' + (m.params.exceptionDetails?.exception?.description || '').slice(0, 200))
  }
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

// 装一个全局错误收集器，再导航 —— 这样连早期的异常也抓得到
await send('Page.addScriptToEvaluateOnNewDocument', { source: `
  window.__errs = [];
  window.addEventListener('error', (e) => window.__errs.push('error: ' + (e.message||'') + ' @' + (e.filename||'').split('/').pop() + ':' + e.lineno));
  window.addEventListener('unhandledrejection', (e) => window.__errs.push('reject: ' + String(e.reason && e.reason.message || e.reason).slice(0,200)));
` })
await send('Page.navigate', { url: URL3D })
await sleep(6000)
console.log('URL      :', await ev('location.href'))
console.log('__qy3d   :', await ev('typeof window.__qy3d'))
console.log('canvas   :', await ev(`(()=>{const c=document.querySelector('canvas'); return c? c.width+'x'+c.height : '无';})()`))
console.log('页面文案 :', await ev('document.body.innerText.replace(/\\s+/g," ").slice(0,300)'))
console.log('3D 相关文案:', await ev(`(()=>{const t=document.body.innerText;
  const m=t.match(/[^
]*(正在准备|3D 模块加载失败|WebGL|无法创建|摆不下|件家具|房间 ·)[^
]*/g);
  return JSON.stringify(m? m.slice(0,6): []);})()`))
console.log('canvas 数:', await ev(`document.querySelectorAll('canvas').length`))
console.log('页面内错误:', await ev('JSON.stringify((window.__errs||[]).slice(0,6))'))
console.log('控制台   :')
for (const l of logs.slice(0, 12)) console.log('   ', l)
ws.close()
