
/** 打开应用供人工测试（自包含，不依赖其它脚本）。 */
import { spawnSync } from 'node:child_process'

const PORT = 9222, APP = process.env.QY_APP || 'http://127.0.0.1/'
const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
const page = list.find((t) => t.type === 'page')
const ws = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((r, j) => { ws.addEventListener('open', r, {once:true}); ws.addEventListener('error', j, {once:true}) })
let id = 0; const pend = new Map()
ws.addEventListener('message', (e) => { const m = JSON.parse(e.data)
  if (m.id && pend.has(m.id)) { const {res,rej}=pend.get(m.id); pend.delete(m.id); m.error?rej(new Error(JSON.stringify(m.error))):res(m.result) } })
const send = (method, params={}) => new Promise((res,rej)=>{ const n=++id; pend.set(n,{res,rej}); ws.send(JSON.stringify({id:n,method,params})) })
const ev = async (expr) => (await send('Runtime.evaluate',{expression:expr,awaitPromise:true,returnByValue:true})).result.value
const sleep = (ms) => new Promise(r=>setTimeout(r,ms))

await send('Page.enable'); await send('Runtime.enable')
await send('Page.navigate', { url: APP + 'login' }); await sleep(2500)
await ev(`localStorage.clear(); sessionStorage.clear();`)
await send('Page.navigate', { url: APP + 'login' }); await sleep(4000)
console.log('标题:', await ev('document.title'))
console.log('摘要:', await ev(`document.body.innerText.replace(/\\s+/g,' ').slice(0,200)`))
const ps = `
Add-Type -Namespace W -Name U -MemberDefinition '
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);'
$p = Get-Process msedge | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if ($p) { [W.U]::ShowWindow($p.MainWindowHandle, 3); [W.U]::ShowWindow($p.MainWindowHandle, 9); [W.U]::SetForegroundWindow($p.MainWindowHandle); "已提到前台" } else { "无窗口" }`
console.log('窗口:', spawnSync('powershell.exe',['-NoProfile','-Command',ps],{encoding:'utf8'}).stdout.trim())
console.log('可见:', await ev(`JSON.stringify({hidden:document.hidden, vis:document.visibilityState})`))
const r = await send('Page.captureScreenshot', { format: 'png' })
const { writeFileSync } = await import('node:fs')
writeFileSync('logs/_opened.png', Buffer.from(r.data, 'base64'))
console.log('截图 → logs/_opened.png')
ws.close()