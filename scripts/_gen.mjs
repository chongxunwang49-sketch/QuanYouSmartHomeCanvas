
import { readFileSync, writeFileSync } from 'node:fs'
const V=JSON.parse(readFileSync('/tmp/verify.json','utf8'))
const api=async(p,b,t)=>{const r=await fetch('http://127.0.0.1/api/v1'+p,{method:b===undefined?'GET':'POST',
  headers:{'Content-Type':'application/json',...(t?{Authorization:`Bearer ${t}`}:{})},
  body:b===undefined?undefined:JSON.stringify(b)});return r.json()}
const sleep=ms=>new Promise(r=>setTimeout(r,ms))
const g=await api('/design/generate',{layout_id:V.layoutId,styles:['modern','nordic','chinese'],
  budget_grades:['economy','medium','high']},V.token)
let s; for(let i=0;i<70;i++){s=(await api(`/task/${g.data.task_id}/status`,undefined,V.token)).data
  if(['completed','failed'].includes(s.status))break; await sleep(5000)}
console.log(s.status, '方案', (s.result?.plans||[]).length)
V.taskId=g.data.task_id
writeFileSync('/tmp/verify.json', JSON.stringify(V))