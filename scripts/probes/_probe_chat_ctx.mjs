/**
 * 验证「智友问答」能自动接回上下文：
 * 把解析任务的 task_id 放进 sessionStorage（模拟"刚才在解析页跑过一次"），
 * 打开 /chat，看顶部那排"这次会带上"是否翻成已带上，并看实际提问时是否带了 layout_id。
 */
import { writeFileSync } from 'node:fs'

const PARSE_TASK = process.argv[2] || ''
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
const net = []
ws.addEventListener('message', (e) => {
  const m = JSON.parse(e.data)
  if (m.method === 'Network.requestWillBeSent' && m.params.request.url.includes('/chat/')) {
    net.push(m.params.request.url + ' ' + (m.params.request.postData || '').slice(0, 160))
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
const frame = () => send('Page.captureScreenshot', { format: 'jpeg', quality: 30 })

await send('Page.enable')
await send('Runtime.enable')
await send('Network.enable')
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
  ${PARSE_TASK ? `sessionStorage.setItem('qy.poll.parse', '${PARSE_TASK}');` : ''}
  sessionStorage.setItem('qy.poll.generate', 'task_20260927_c8c15909');
  sessionStorage.setItem('qy.generate.selected.layout_20260927_4fb0cc', 'plan_modern_economy');
  return true;})()`)

await send('Page.navigate', { url: APP + 'chat' })
for (let i = 0; i < 60; i++) { await sleep(180); if (i % 5 === 0) await frame() }

console.log('顶部状态胶囊:', await ev(`(()=>{
  const t=document.body.innerText;
  const m=t.match(/已带上你的户型与方案|还没选中户型或方案/);
  return m? m[0] : '（没找到）';})()`))
console.log('"这次会带上"那一排:', await ev(`(()=>{
  const i=document.body.innerText.indexOf('这次会带上');
  return i<0 ? '（没找到）' : document.body.innerText.slice(i, i+60).replace(/\\s+/g,' ');})()`))

// 提一句，看请求里有没有带上 layout_id / plan_id
await ev(`(()=>{const ta=document.querySelector('textarea');
  ta.value='你好，简单介绍一下你能做什么';
  ta.dispatchEvent(new Event('input',{bubbles:true}));
  return true;})()`)
await sleep(300)
await ev(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>/发送/.test(x.textContent||''));
  b && b.click(); return true;})()`)
await sleep(9000)

console.log('\\n对 /chat 的请求（看 body 里有没有 layout_id / plan_id）:')
for (const r of net.slice(0, 4)) console.log('  ', r)
const s = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync('logs/_chat-ctx.png', Buffer.from(s.data, 'base64'))
console.log('截图 → logs/_chat-ctx.png')
ws.close()
