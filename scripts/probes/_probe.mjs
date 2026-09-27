/**
 * ⚠️ **这个脚本跑不起来**（2026-09-28 整理时发现，如实标注而不是留着让人踩）：
 *    它 `import { connect, sleep } from './_cdp.mjs'`，而那个文件
 *    **在仓库里从来没有过**（`git ls-files | grep _cdp` 一次都没有）。
 *    多半是当时只在本地存在、随某次事故一起没了 —— 全仓只有这一个文件 import 它。
 *
 * 要救活的话：照着 `_probe_login_left.mjs` 里那段 CDP 连接代码（原生 WebSocket +
 * 手写 send/ev）补一个 `_cdp.mjs` 即可；不需要就直接删。
 */

import { connect, sleep } from './_cdp.mjs'
import { spawnSync } from 'node:child_process'
const ps = `
Add-Type -Namespace W -Name U -MemberDefinition '
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);'
$p = Get-Process msedge | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if ($p) { [W.U]::ShowWindow($p.MainWindowHandle, 9); [W.U]::SetForegroundWindow($p.MainWindowHandle); "ok" } else { "no window" }`
console.log('提窗口:', spawnSync('powershell.exe',['-NoProfile','-Command',ps],{encoding:'utf8'}).stdout.trim())
const cdp = await connect()
await cdp.send('Runtime.enable'); await cdp.send('Page.enable'); await cdp.send('Page.bringToFront')
await sleep(2500)
console.log(await cdp.evaluate(`JSON.stringify({hidden:document.hidden, url:location.pathname})`))
const MEASURE = `new Promise((res)=>{ let n=0; const t0=performance.now(); const sp=[];
  function loop(t){ sp.push(t); n++;
    if(performance.now()-t0<6000) requestAnimationFrame(loop);
    else { const d=sp.slice(1).map((v,i)=>v-sp[i]).sort((a,b)=>a-b); const k=d.length||1;
      res({fps:+(k/((performance.now()-t0)/1000)).toFixed(1), p50:+d[Math.floor(k*.5)]?.toFixed(2),
           p95:+d[Math.floor(k*.95)]?.toFixed(2), worst:+d[k-1]?.toFixed(2),
           jank20:d.filter(x=>x>20).length,
           badge:([...document.querySelectorAll('span')].find(s=>/fps/.test(s.textContent))||{}).textContent}); }
  }
  requestAnimationFrame(loop); setTimeout(()=>res({timeout:true}), 14000); })`
console.log('带家具静止:', JSON.stringify(await cdp.evaluate(MEASURE)))
cdp.close()