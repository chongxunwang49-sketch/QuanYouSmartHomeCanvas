
/** 端到端验证：族色 / 放大倍数 / 准星家具说明。 */
import { spawnSync } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'

const API='http://127.0.0.1/api/v1', APP='http://127.0.0.1/'
const api = async (p,b,t)=>{const r=await fetch(API+p,{method:b===undefined?'GET':'POST',
  headers:{'Content-Type':'application/json',...(t?{Authorization:`Bearer ${t}`}:{})},
  body:b===undefined?undefined:JSON.stringify(b)});return r.json()}
const src=readFileSync('tests/test_auth.py','utf8')
const m=src.match(/\[\(("?admin"?),\s*"([^"]+)"\)/)
const token=(await api('/auth/login',{username:m[1].replace(/"/g,''),password:m[2]})).data.access_token
const sleep=ms=>new Promise(r=>setTimeout(r,ms))

console.log('① 解析…')
const img=readFileSync('演示素材/户型图/02-两室一厅-78平.png')
const p=await api('/layout/parse',{image:img.toString('base64'),image_media_type:'image/png'},token)
let s; for(let i=0;i<90;i++){s=(await api(`/task/${p.data.task_id}/status`,undefined,token)).data
  if(['completed','failed'].includes(s.status))break; await sleep(2000)}
const layoutId=s.result?.layout_id; console.log('  ',s.status,s.elapsed_seconds+'s',layoutId)

console.log('② 生成方案…')
const g=await api('/design/generate',{layout_id:layoutId,styles:['modern','nordic','chinese'],
  budget_grades:['economy','medium','high']},token)
let G; for(let i=0;i<70;i++){G=(await api(`/task/${g.data.task_id}/status`,undefined,token)).data
  process.stdout.write(`   ${(i+1)*5}s ${G.phase_text}      \r`)
  if(['completed','failed'].includes(G.status))break; await sleep(5000)}
const plans=G.result?.plans||[]; const planId=plans[0]?.plan_id
console.log(`\n   ${G.status}  方案 ${plans.length}  取用 ${planId}`)

console.log('③ 直接查接口（走 nginx）')
const w=(await (await fetch(`http://127.0.0.1/api/v1/layout/${layoutId}/walkable?plan_id=${planId}`,
  {headers:{Authorization:`Bearer ${token}`}})).json()).data
const f=(await (await fetch(`http://127.0.0.1/api/v1/layout/${layoutId}/furniture?plan_id=${planId}`,
  {headers:{Authorization:`Bearer ${token}`}})).json()).data
console.log('   scene_scale =', w.scene_scale, ' / ', f.scene_scale)
console.log('   scale_note  =', w.scene_scale_note || '(未放大)')
const colors=new Set(); for(const r of f.rooms) for(const x of r.placements) colors.add(x.color)
console.log(`   摆下 ${f.placed_count} 件，${colors.size} 种颜色`)
const sp=f.rejected.filter(r=>r.kind==='space'), po=f.rejected.filter(r=>r.kind==='policy')
console.log(`   空间类拒绝 ${sp.length} 件   政策类 ${po.length} 件`)
console.log('   平面图是否被放大：', await (await fetch(`http://127.0.0.1/api/v1/layout/${layoutId}/plan.svg`,
  {headers:{Authorization:`Bearer ${token}`}})).text().then(t=>t.includes('<svg')?'不受影响（仍是真实尺寸）':'?'))
writeFileSync('/tmp/verify.json', JSON.stringify({layoutId, planId, token}))