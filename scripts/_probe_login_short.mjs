/**
 * 登录页在矮窗口下：左栏（品牌/标题/主张/声明）有没有被裁掉、够不够得到。
 *
 * 左栏的根是 `overflow-hidden`（图墙必须裁），所以"内容比框高"时，
 * 多出来的部分**永远看不到也滚不到** —— 这正是"滑不到最下面"的症状之一。
 * 关键：要先清掉 token，否则 /login 会直接跳到工作台，量到的不是登录页。
 */
const PORT = 9222
const APP = 'http://127.0.0.1/'
const HEIGHT = Number(process.argv[2] || 500)

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
  const r = await send('Runtime.evaluate', {
    expression: e, awaitPromise: true, returnByValue: true })
  if (r.exceptionDetails) throw new Error('JS: ' + (r.exceptionDetails.exception?.description || '').slice(0, 140))
  return r.result.value
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

await send('Page.enable')
await send('Runtime.enable')
await send('Emulation.setDeviceMetricsOverride',
  { width: 1280, height: HEIGHT, deviceScaleFactor: 1, mobile: false })
await send('Page.navigate', { url: APP + 'login' })
await sleep(1500)
await ev('localStorage.clear(); sessionStorage.clear();')
await send('Page.navigate', { url: APP + 'login' })
await sleep(3200)

console.log(`视口 1280×${HEIGHT}`)
console.log(await ev(`(()=>{
  const secs=[...document.querySelectorAll('main > section')];
  const info=(el)=>{ if(!el) return null; const b=el.getBoundingClientRect();
    return {cls:(el.className||'').toString().slice(0,24), clientH:el.clientHeight,
            scrollH:el.scrollHeight, 藏住:el.scrollHeight-el.clientHeight,
            overflowY:getComputedStyle(el).overflowY,
            top:Math.round(b.top), bottom:Math.round(b.bottom)};};
  const left=secs[0];
  const kids=[...left.querySelectorAll('*')].filter(e=>e.getBoundingClientRect().height>4);
  const last=kids.reduce((a,c)=>a.getBoundingClientRect().bottom>c.getBoundingClientRect().bottom?a:c);
  const lb=last.getBoundingClientRect(), lbb=left.getBoundingClientRect();
  return JSON.stringify({
    视口:innerHeight,
    左栏:info(left), 右栏:info(secs[1]),
    左栏最后元素:{
      文案:(last.textContent||'').trim().replace(/\\s+/g,' ').slice(0,22),
      bottom:Math.round(lb.bottom),
      超出左栏底:Math.round(lb.bottom-lbb.bottom),
      在视口内: lb.bottom<=innerHeight+1,
    },
    左栏根被裁: left.scrollHeight>left.clientHeight+2,
  }, null, 1);})()`))

// 左栏里所有文字块的位置（看哪个被推到看不见的地方）
console.log('\n左栏文字块：')
console.log(await ev(`(()=>{
  const left=document.querySelector('main > section');
  const lb=left.getBoundingClientRect();
  const out=[];
  for(const e of left.querySelectorAll('h1, p, li, span')){
    const t=(e.textContent||'').trim();
    if(t.length<6 || e.children.length) continue;
    const b=e.getBoundingClientRect();
    if(b.height<6) continue;
    out.push({文案:t.slice(0,26), top:Math.round(b.top), bottom:Math.round(b.bottom),
      在左栏内: b.bottom<=lb.bottom+1 && b.top>=lb.top-1});
    if(out.length>=8) break;
  }
  return JSON.stringify(out, null, 1);})()`))
ws.close()
