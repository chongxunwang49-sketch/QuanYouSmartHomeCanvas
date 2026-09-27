/**
 * 量一下解析页左右两栏的底边对不对齐。
 *
 * 需求方提过一条界面要求：右栏不能留一块异常的空白，两栏要形成规整的长框格局。
 * 后来右栏又加了一整个「户型详情（文字资料）」模块，右栏比原来高得多 ——
 * 这条探针就是回归检查：**左右两栏的最后一张卡，底边差多少**。
 *
 *     node scripts/probes/_probe_parse_layout.mjs
 */
import { writeFileSync } from 'node:fs'

const ROOT = 'C:/Users/DELL/Desktop/全友·智绘家QuanYou Smart HomeCanvas'
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
await send('Page.navigate', { url: 'http://127.0.0.1/parse' })
await new Promise((r) => setTimeout(r, 4000))
for (let i = 0; i < 12; i++) { await send('Page.captureScreenshot', { format: 'jpeg', quality: 30 }) }

const info = await ev(`(()=>{
  // 两栏是主内容区里那个 xl:grid 的两列，各取最后一层子卡片
  const grid=[...document.querySelectorAll('main div')]
    .find(d=>getComputedStyle(d).display==='grid'
             && d.children.length===2
             && d.querySelector('button'));
  if(!grid) return {err:'没找到两栏容器'};
  const [L,R]=grid.children;
  const last=(col)=>{const ks=[...col.children]; return ks[ks.length-1];};
  const box=(el)=>{const r=el.getBoundingClientRect();
    return {top:Math.round(r.top),bottom:Math.round(r.bottom),h:Math.round(r.height)};};
  return {
    colLeft:box(L), colRight:box(R),
    lastLeft:box(last(L)), lastRight:box(last(R)),
    lastLeftText:(last(L).innerText||'').replace(/\\s+/g,' ').slice(0,40),
    lastRightText:(last(R).innerText||'').replace(/\\s+/g,' ').slice(0,40),
    docH:Math.round(document.documentElement.scrollHeight),
    winH:window.innerHeight,
  };})()`)
console.log(JSON.stringify(info, null, 2))
if (info && !info.err) {
  console.log(`\n左栏底边 ${info.colLeft.bottom} / 右栏底边 ${info.colRight.bottom} `
    + `→ 差 ${Math.abs(info.colLeft.bottom - info.colRight.bottom)} px`)
  console.log(`两栏最后一卡的底边：${info.lastLeft.bottom} vs ${info.lastRight.bottom} `
    + `→ 差 ${Math.abs(info.lastLeft.bottom - info.lastRight.bottom)} px`)

  // ── 底下的内容够不够得着（需求方提过"滚不到底、按不到按钮"）──
  const scroll = await ev(`(()=>{
    const cands=[];
    for(let el=document.querySelector('main')||document.body; el; el=el.parentElement){
      const cs=getComputedStyle(el);
      if(/(auto|scroll)/.test(cs.overflowY) && el.scrollHeight>el.clientHeight+1)
        cands.push(el);
    }
    if(!cands.length) return {scrollable:false};
    // 最外层那个才是真正滚动的容器
    const el=cands[cands.length-1];
    const before=el.scrollTop; el.scrollTop=el.scrollHeight;
    const card=[...document.querySelectorAll('div')]
      .filter(d=>/识别到的房间/.test(d.innerText||'') && d.children.length<8).pop();
    const r=card?card.getBoundingClientRect():null;
    const res={scrollable:true, tag:el.tagName+'.'+el.className.split(' ')[0],
      scrollHeight:el.scrollHeight, clientHeight:el.clientHeight,
      scrollTopBefore:before, scrollTopMax:el.scrollTop,
      lastCardBottomAfterScroll:r?Math.round(r.bottom):null, winH:window.innerHeight,
      reachable:r? r.bottom<=window.innerHeight+1 : null};
    el.scrollTop=before;
    return res;})()`)
  console.log('\n滚动容器：' + JSON.stringify(scroll))
  if (scroll?.scrollable) {
    console.log(`  可滚 ${scroll.scrollHeight - scroll.clientHeight} px；`
      + `滚到底后最后一张卡底边 y=${scroll.lastCardBottomAfterScroll}（视口 ${scroll.winH}）`
      + ` → ${scroll.reachable ? '够得着 ✓' : '够不着 ✗'}`)
  } else {
    console.log('  页面没有可滚动容器；若内容超出视口即为够不着 ✗')
  }
  const s = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(`${ROOT}/logs/_parse-layout.png`, Buffer.from(s.data, 'base64'))
}
ws.close()
