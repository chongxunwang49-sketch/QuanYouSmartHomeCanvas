/**
 * 量登录页左栏：品牌行 / 标题 / 副标题 / 三项主张 / 底部声明的左边缘与纵向间距。
 *
 * 需求方提的三条（去除淡色衬底、把副标题那类文字改纯白、标题往上贴住品牌行）
 * 都要求"别把不该动的字挪走"，所以先量一遍现状，改完再量一遍对照。
 *
 *     node scripts/_probe_login_left.mjs [前后缀]
 */
import { writeFileSync } from 'node:fs'

const ROOT = 'C:/Users/DELL/Desktop/全友·智绘家QuanYou Smart HomeCanvas'
const TAG = process.argv[2] || 'now'
const PORT = 9222

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

await send('Page.enable')
await send('Runtime.enable')
await send('Emulation.setDeviceMetricsOverride',
  { width: 1680, height: 1000, deviceScaleFactor: 1, mobile: false })
// ⚠️ 先清令牌：登录状态下访问 /login 会被路由重定向到工作台，
//    于是 .login-left 根本不在 DOM 里（第一次就踩了这个，截了张工作台的图）。
await send('Page.navigate', { url: 'http://127.0.0.1/login' })
await new Promise((r) => setTimeout(r, 800))
await ev(`(()=>{localStorage.clear();sessionStorage.clear();return 1})()`)
await send('Page.navigate', { url: 'http://127.0.0.1/login' })
await new Promise((r) => setTimeout(r, 2500))
for (let i = 0; i < 10; i++) { await send('Page.captureScreenshot', { format: 'jpeg', quality: 30 }) }

const info = await ev(`(()=>{
  const left=document.querySelector('.login-left');
  if(!left) return {err:'没有 .login-left（视口太窄？）'};
  const box=(el)=>{const r=el.getBoundingClientRect();
    return {x:Math.round(r.left), y:Math.round(r.top),
            r:Math.round(r.right), b:Math.round(r.bottom)};};
  const byText=(sel,txt)=>[...left.querySelectorAll(sel)]
    .find(e=>(e.textContent||'').includes(txt));
  const brand=byText('div','全友 · 智绘家');
  const h1=left.querySelector('h1');
  const sub=[...left.querySelectorAll('p')]
    .find(p=>(p.textContent||'').includes('上传一张户型图'));
  const beliefs=left.querySelector('ul');
  const attr=[...left.querySelectorAll('p')]
    .find(p=>(p.textContent||'').includes('素材')||(p.textContent||'').includes('Pexels'));
  const bgOf=(el)=>el?getComputedStyle(el).backgroundColor:'-';
  const colorOf=(el)=>el?getComputedStyle(el).color:'-';
  const panel=h1?h1.parentElement:null;
  return {
    section:box(left),
    brandBox:brand?box(brand):null, brandColor:colorOf(brand),
    h1Box:h1?box(h1):null, h1Color:colorOf(h1),
    panelBox:panel?box(panel):null, panelBg:bgOf(panel),
    subBox:sub?box(sub):null, subColor:colorOf(sub),
    beliefBox:beliefs?box(beliefs):null,
    beliefDescColor:beliefs?colorOf(beliefs.querySelectorAll('div')[1]):'-',
    attrBox:attr?box(attr):null, attrColor:colorOf(attr), attrBg:bgOf(attr),
    brandPanelBg: brand?bgOf(brand.parentElement) : '-',
    brandPanelBox: brand?box(brand.parentElement) : null,
    brandToH1: (brand&&h1)? Math.round(h1.getBoundingClientRect().top
                - brand.parentElement.getBoundingClientRect().bottom) : null,
  };})()`)
console.log(JSON.stringify(info, null, 2))
if (!info?.err) {
  console.log(`\n左边缘：品牌面板 ${info.brandPanelBox?.x} / 标题面板 ${info.panelBox?.x}`
    + ` / 声明 ${info.attrBox?.x}`)
  console.log(`品牌文字 x=${info.brandBox?.x}，标题文字 x=${info.h1Box?.x}`
    + `（差 ${Math.abs((info.brandBox?.x ?? 0) - (info.h1Box?.x ?? 0))} px）`)
  console.log(`品牌行底边 y=${info.brandPanelBox?.b} → 标题面板顶边 y=${info.panelBox?.y}`
    + `（间距 ${info.brandToH1} px）`)
}
const s = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync(`${ROOT}/logs/_login-left-${TAG}.png`, Buffer.from(s.data, 'base64'))
console.log(`截图 → logs/_login-left-${TAG}.png`)
ws.close()
