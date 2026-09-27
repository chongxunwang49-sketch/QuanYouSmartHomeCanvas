/**
 * 浏览器里走一遍演示路径，并对关键几步截图：
 *
 *   上传 02-两室一厅-78平.png → 解析 → 点「载入演示样例」
 *   → 输入框里应当是 02 那一份 → 点「保存并重新诊断」→ 分数与置信度上来了
 *
 * 这条路径的意义：需求方问的是"三个平面图各自的屋子详细资料在哪、
 * 是不是一一对应"。文件在磁盘上对上了只是一半，**界面上点得到、挑得对**才是另一半。
 *
 *     node scripts/_probe_demo_flow.mjs [01|02|03]
 *
 * 需要：Edge 开着 --remote-debugging-port=9222，四个容器 healthy。
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'

const NUM = process.argv[2] || '02'
const PLAN = {
  '01': '01-一室一厅-45平',
  '02': '02-两室一厅-78平',
  '03': '03-三室两厅-98平',
}[NUM]
if (!PLAN) throw new Error(`不认识的编号 ${NUM}`)

const ROOT = 'C:/Users/DELL/Desktop/全友·智绘家QuanYou Smart HomeCanvas'
const PNG = `${ROOT}/演示素材/户型图/${PLAN}.png`
const PORT = 9222
const APP = 'http://127.0.0.1/'

mkdirSync(`${ROOT}/logs`, { recursive: true })

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
// rAF 在被遮挡的窗口里不跑，必须周期性地强制出一帧
const frame = () => send('Page.captureScreenshot', { format: 'jpeg', quality: 30 })
const shot = async (name) => {
  const s = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(`${ROOT}/logs/_demo-flow-${name}.png`, Buffer.from(s.data, 'base64'))
  console.log(`    截图 → logs/_demo-flow-${name}.png`)
}
const clickByText = (text) => ev(`(()=>{
  const b=[...document.querySelectorAll('button')].find(x=>(x.textContent||'').includes('${text}'));
  if(!b) return '找不到按钮：${text}';
  b.click(); return 'clicked';})()`)

await send('Page.enable')
await send('Runtime.enable')
await send('Emulation.setDeviceMetricsOverride',
  { width: 1680, height: 1000, deviceScaleFactor: 1, mobile: false })

// ── 登录并把 token 放进 localStorage ──
await send('Page.navigate', { url: APP + 'login' })
await sleep(1500)
await ev(`(async()=>{
  const r=await fetch('/api/v1/auth/login',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:'vip',password:'vip123'})});
  const j=await r.json();
  localStorage.setItem('qy.access_token', j.data.access_token);
  sessionStorage.clear();
  return true;})()`)
console.log('已登录')

// ── 交一次解析，把 task_id 塞进 sessionStorage，再进解析页 ──
const b64 = readFileSync(PNG).toString('base64')
const created = await ev(`(async()=>{
  const r=await fetch('/api/v1/layout/parse',{method:'POST',
    headers:{'Content-Type':'application/json',
             'Authorization':'Bearer '+localStorage.getItem('qy.access_token')},
    body:JSON.stringify({image:'data:image/png;base64,${b64}',
                         image_media_type:'image/png', detail_level:'full'})});
  const j=await r.json();
  if(j.code!==0) return 'FAIL '+JSON.stringify(j);
  sessionStorage.setItem('qy.poll.parse', j.data.task_id);
  return j.data.task_id;})()`)
if (typeof created === 'string' && created.startsWith('FAIL')) {
  throw new Error(`提交解析失败：${created}`)
}
console.log(`已提交解析 task_id=${created}（图：${PLAN}）`)

await send('Page.navigate', { url: APP + 'parse' })
// ⚠️ 判据不能是页面上有没有「解析完成」这几个字 —— 右栏那张卡片的说明文案里
//    就写着"解析完成后，右侧会解锁…"，一开始就在，会立刻误判成完成。
//    真正的前置条件是**「载入演示样例」这个按钮出现**（它只在有结果时渲染）。
let done = false
for (let i = 0; i < 90; i++) {
  await sleep(2000)
  await frame()
  const st = await ev(`(()=>{
    const btns=[...document.querySelectorAll('button')].map(b=>b.textContent||'');
    if(btns.some(t=>t.includes('载入演示样例'))) return 'results';
    const m=document.body.innerText.match(/(\\d+)\\s*%/);
    return m? '进度 '+m[1]+'%' : '等待中';})()`)
  if (i % 5 === 0) console.log(`    [${(i * 2)}s] ${st}`)
  if (st === 'results') { done = true; break }
}
if (!done) throw new Error('解析没有在 180 秒内完成')
await sleep(1500)
await frame()
console.log('\n解析完成。右栏的「载入演示样例」会挑哪一份：')
await shot('1-parsed')

console.log('  ' + await ev(`(()=>{const b=[...document.querySelectorAll('button')]
  .find(x=>(x.textContent||'').includes('载入演示样例')); return b?'按钮在':'没找到按钮';})()`))

await clickByText('载入演示样例')
await sleep(2500)
await frame()
console.log('\n点完「载入演示样例」：')
console.log('  输入框标题：' + await ev(
  `(()=>{const i=[...document.querySelectorAll('input')].find(x=>/演示样例|45平|78平|98平/.test(x.value));return i?i.value:'（没找到）';})()`))
console.log('  提示行：' + await ev(`(()=>{
  const t=document.body.innerText; const i=t.indexOf('最接近');
  return i<0? t.slice(0,0) : t.slice(i-20,i+40).replace(/\\s+/g,' ');})()`))
await shot('2-sample')

await clickByText('保存并重新诊断')
for (let i = 0; i < 45; i++) { await sleep(2000); await frame() }
await sleep(1000)
console.log('\n点完「保存并重新诊断」：')
console.log('  ' + await ev(`(()=>{const t=document.body.innerText;
  const m=t.match(/综合\\s*([\\d.]+)\\s*分[\\s\\S]{0,60}?(\\d\\.\\d+)/);
  return m? '综合 '+m[1]+' 分' : '（没读到分数）';})()`))
console.log('  页面上的诊断/详情区：')
console.log(await ev(`(()=>{const t=document.body.innerText.replace(/\\s+/g,' ');
  const i=t.indexOf('综合'); return i<0? '（没找到）' : t.slice(Math.max(0,i-120), i+240);})()`))
await shot('3-rediagnosed')

ws.close()
