/**
 * 起一次真实的方案生成（三套并行），等它跑完，打印 task_id。
 * 给探针用：后端重启/任务过期之后，需要一个"活"的 generate 任务才能打开
 * 带家具的 3D 漫游。
 *
 * 用法: node scripts/_new_gen_task.mjs [layoutId]
 */
const BASE = 'http://127.0.0.1/api/v1'
const LAYOUT = process.argv[2] || 'layout_20260927_4fb0cc'

const login = async () => {
  const r = await fetch(BASE + '/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'vip', password: 'vip123' }),
  })
  const j = await r.json()
  if (j.code !== 0) throw new Error('登录失败: ' + j.msg)
  return j.data.access_token
}

const tok = await login()
const H = { 'Content-Type': 'application/json', Authorization: 'Bearer ' + tok }

const created = await (await fetch(BASE + '/design/generate', {
  method: 'POST', headers: H,
  body: JSON.stringify({
    layout_id: LAYOUT,
    styles: ['modern', 'nordic', 'chinese'],
    budget_grades: ['economy', 'medium', 'high'],
    quanyou_priority: true,
    requirements: { family_size: 3, has_elderly: false, has_children: true, pets: false, smart_home: false, eco_level: 'high' },
  }),
})).json()
if (created.code !== 0) throw new Error('创建任务失败: ' + JSON.stringify(created).slice(0, 200))
const taskId = created.data.task_id
console.log('task_id:', taskId, '| 预计', created.data.estimated_seconds, '秒')

const t0 = Date.now()
let last = ''
for (;;) {
  await new Promise((r) => setTimeout(r, 2000))
  const st = await (await fetch(`${BASE}/task/${taskId}/status`, { headers: H })).json()
  const d = st.data || {}
  if (d.phase_text && d.phase_text !== last) {
    last = d.phase_text
    console.log(`  +${((Date.now() - t0) / 1000).toFixed(0)}s ${d.phase_text}`)
  }
  if (d.status === 'completed' || d.status === 'failed') {
    console.log(`\n状态 ${d.status}，用时 ${((Date.now() - t0) / 1000).toFixed(1)}s，`
      + `方案数 ${(d.result?.plans || []).length}`)
    console.log('TASK=' + taskId)
    break
  }
  if (Date.now() - t0 > 240_000) { console.log('超时'); process.exit(1) }
}
