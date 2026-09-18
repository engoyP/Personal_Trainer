<template>
  <div class="grid grid-4">
    <div class="stat">
      <div class="label">近 30 天运动</div>
      <div class="value">{{ summary.count }}<span class="unit">次</span></div>
    </div>
    <div class="stat">
      <div class="label">总里程</div>
      <div class="value">{{ summary.total_distance_km || 0 }}<span class="unit">km</span></div>
    </div>
    <div class="stat">
      <div class="label">总时长</div>
      <div class="value">{{ summary.total_duration_h || 0 }}<span class="unit">小时</span></div>
    </div>
    <div class="stat">
      <div class="label">平均配速</div>
      <div class="value">{{ pace(summary.avg_pace_s_per_km) }}</div>
    </div>
    <div class="stat">
      <div class="label">平均心率</div>
      <div class="value">{{ summary.avg_hr || '—' }}<span class="unit">bpm</span></div>
    </div>
    <div class="stat">
      <div class="label">平均步频</div>
      <div class="value">{{ summary.avg_cadence_spm || '—' }}<span class="unit">spm</span></div>
    </div>
    <div class="stat">
      <div class="label">平均步幅</div>
      <div class="value">{{ summary.avg_stride_m || '—' }}<span class="unit">m</span></div>
    </div>
    <div class="stat">
      <div class="label">训练负荷</div>
      <div class="value">{{ summary.total_load || 0 }}</div>
    </div>
  </div>

  <MonthCalendar />

  <div class="grid grid-2">
    <div class="card">
      <h3>近 8 周跑量与负荷</h3>
      <div ref="weeklyEl" class="chart"></div>
    </div>
    <div class="card">
      <h3>心率区间分布（近 28 天）</h3>
      <div ref="zoneEl" class="chart"></div>
    </div>
  </div>

  <div class="card">
    <h3>训练负荷曲线 · 疲劳与状态</h3>
    <div ref="loadEl" class="chart"></div>
    <div class="row muted" style="font-size:12px">
      <span>ATL 急性负荷（7 天）· CTL 慢性负荷（42 天）· TSB = CTL − ATL，负值代表疲劳</span>
    </div>
  </div>

  <div class="grid grid-2">
    <div class="card">
      <h3>体重与睡眠</h3>
      <div ref="bodyEl" class="chart"></div>
    </div>
    <div class="card">
      <h3>配速与心率</h3>
      <div ref="paceEl" class="chart"></div>
    </div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between;margin-bottom:12px">
      <h3 style="margin:0">运动记录</h3>
      <div class="row">
        <input v-model="monthFilter" type="month" style="width:150px" @change="onFilter" />
        <button v-if="monthFilter" @click="clearFilter">全部</button>
        <span class="muted" style="font-size:12px">共 {{ totalCount }} 条</span>
      </div>
    </div>
    <div class="table-scroll">
      <table v-if="activities.length">
        <thead>
          <tr>
            <th>日期</th><th>类型</th><th>距离</th><th>时长</th>
            <th>配速</th><th>心率</th><th>步频</th><th>步幅</th><th>负荷</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in activities" :key="a.id" class="clickable" @click="open(a.id)">
            <td>{{ a.start_time ? a.start_time.slice(0, 16).replace('T', ' ') : '—' }}</td>
            <td><span class="tag">{{ sportName(a.sport_type) }}</span></td>
            <td>{{ a.distance_km }} km</td>
            <td>{{ a.duration_min }} 分</td>
            <td>{{ pace(a.avg_pace_s_per_km) }}</td>
            <td>{{ a.avg_hr || '—' }}</td>
            <td>{{ a.avg_cadence_spm || '—' }}</td>
            <td>{{ a.avg_stride_m || '—' }}</td>
            <td>{{ a.load }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">这个月还没有运动数据，去「个人档案」页面导入 GPX / TCX 文件</div>
    </div>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import api from '../api'
import MonthCalendar from '../components/MonthCalendar.vue'

const router = useRouter()
const summary = ref({})
const activities = ref([])
const metrics = ref({})
const loadItems = ref([])
const dailyItems = ref([])
const trendItems = ref([])
const profile = ref({})

const weeklyEl = ref(null)
const zoneEl = ref(null)
const loadEl = ref(null)
const bodyEl = ref(null)
const paceEl = ref(null)
let charts = []

const pace = (s) => {
  if (!s) return '—'
  const m = Math.floor(s / 60)
  const sec = Math.round(s % 60)
  return `${m}'${String(sec).padStart(2, '0')}"`
}

const SPORT = { running: '跑步', cycling: '骑行', walking: '步行', swimming: '游泳', hiking: '徒步' }
const sportName = (s) => SPORT[s] || s || '运动'

const monthFilter = ref('')
const totalCount = ref(0)

async function fetchActivities() {
  const params = { limit: 30 }
  if (monthFilter.value) {
    const [y, m] = monthFilter.value.split('-').map(Number)
    params.year = y
    params.month = m
  }
  const { data } = await api.activities(params)
  activities.value = data.items
  totalCount.value = data.total
}

function onFilter() {
  fetchActivities()
}

function clearFilter() {
  monthFilter.value = ''
  fetchActivities()
}

const open = (id) => router.push(`/activity/${id}`)

function render() {
  charts.forEach((c) => c.dispose())
  charts = []

  const m = metrics.value
  const weeks = (m.weekly_distance_km || []).map((_, i, arr) => `W-${arr.length - 1 - i}`)

  const c1 = echarts.init(weeklyEl.value)
  c1.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['周跑量', '周负荷'], bottom: 0 },
    grid: { left: 50, right: 50, top: 20, bottom: 40 },
    xAxis: { type: 'category', data: weeks },
    yAxis: [
      { type: 'value', name: 'km' },
      { type: 'value', name: '负荷' },
    ],
    series: [
      { name: '周跑量', type: 'bar', data: m.weekly_distance_km || [], itemStyle: { color: '#0d9488' } },
      { name: '周负荷', type: 'line', yAxisIndex: 1, data: m.weekly_load || [], itemStyle: { color: '#2563eb' }, smooth: true },
    ],
  })
  charts.push(c1)

  const zones = m.hr_zone_seconds || {}
  const zoneNames = { z1: 'Z1 热身', z2: 'Z2 有氧', z3: 'Z3 节奏', z4: 'Z4 阈值', z5: 'Z5 极限' }
  const zoneColors = { z1: '#93c5fd', z2: '#6ee7b7', z3: '#fcd34d', z4: '#fb923c', z5: '#f87171' }
  const pieData = Object.entries(zones)
    .filter(([, v]) => v > 0)
    .map(([k, v]) => ({ name: zoneNames[k] || k, value: Math.round(v / 60), itemStyle: { color: zoneColors[k] } }))

  const c2 = echarts.init(zoneEl.value)
  c2.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} 分钟 ({d}%)' },
    legend: { bottom: 0 },
    // 华为 GPX/TCX 不含心率，导入后区间全是 0。空饼图会让人以为是加载失败，
    // 所以明确写一句原因。
    graphic: pieData.length
      ? []
      : [{
          type: 'text', left: 'center', top: 'middle',
          style: { text: '近 28 天没有心率数据\n（华为 GPX/TCX 导出不含心率）',
                   textAlign: 'center', fill: '#94a3b8', fontSize: 13, lineHeight: 20 },
        }],
    series: [{ type: 'pie', radius: ['42%', '68%'], data: pieData, label: { formatter: '{b}\n{d}%' } }],
  })
  charts.push(c2)

  const c3 = echarts.init(loadEl.value)
  c3.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['ATL 急性', 'CTL 慢性', 'TSB 状态'], bottom: 0 },
    grid: { left: 50, right: 50, top: 20, bottom: 40 },
    xAxis: { type: 'category', data: loadItems.value.map((d) => d.date.slice(5)) },
    yAxis: { type: 'value' },
    series: [
      { name: 'ATL 急性', type: 'line', data: loadItems.value.map((d) => d.atl), itemStyle: { color: '#ef4444' }, smooth: true, showSymbol: false },
      { name: 'CTL 慢性', type: 'line', data: loadItems.value.map((d) => d.ctl), itemStyle: { color: '#0d9488' }, smooth: true, showSymbol: false },
      { name: 'TSB 状态', type: 'line', data: loadItems.value.map((d) => d.tsb), itemStyle: { color: '#8b5cf6' }, smooth: true, showSymbol: false },
    ],
  })
  charts.push(c3)

  // 体重 + 睡眠：体重折线（含目标线），睡眠柱状
  const dItems = dailyItems.value
  const dDates = dItems.map((d) => d.date.slice(5))
  const target = profile.value.target_weight_kg || null

  const c4 = echarts.init(bodyEl.value)
  c4.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['体重', '睡眠', '目标体重'], bottom: 0 },
    grid: { left: 50, right: 50, top: 20, bottom: 40 },
    xAxis: { type: 'category', data: dDates },
    yAxis: [
      { type: 'value', name: 'kg', scale: true },
      { type: 'value', name: 'h', max: 12 },
    ],
    series: [
      {
        name: '体重', type: 'line', yAxisIndex: 0, connectNulls: true,
        data: dItems.map((d) => d.weight_kg), smooth: true,
        itemStyle: { color: '#0d9488' },
        markLine: target
          ? { silent: true, symbol: 'none', data: [{ yAxis: target, label: { formatter: `目标 ${target}kg` } }],
              lineStyle: { color: '#f59e0b', type: 'dashed' } }
          : undefined,
      },
      { name: '睡眠', type: 'bar', yAxisIndex: 1, data: dItems.map((d) => d.sleep_h), itemStyle: { color: '#a5b4fc' } },
    ],
  })
  charts.push(c4)

  // 配速 + 心率：配速轴反向（越低越快，图上越高）
  const tItems = trendItems.value.filter((d) => d.avg_pace_s_per_km || d.avg_hr)
  const c5 = echarts.init(paceEl.value)
  c5.setOption({
    tooltip: {
      trigger: 'axis',
      formatter: (ps) => {
        const rows = ps.map((p) => {
          const v = p.seriesName === '配速' ? pace(p.value) : `${p.value} bpm`
          return `${p.marker}${p.seriesName}：${v}`
        })
        return `${ps[0].axisValue}<br/>${rows.join('<br/>')}`
      },
    },
    legend: { data: ['配速', '心率'], bottom: 0 },
    grid: { left: 60, right: 50, top: 20, bottom: 40 },
    xAxis: { type: 'category', data: tItems.map((d) => d.date.slice(5)) },
    yAxis: [
      { type: 'value', name: '配速', inverse: true, axisLabel: { formatter: (v) => pace(v) }, scale: true },
      { type: 'value', name: 'bpm', scale: true },
    ],
    series: [
      { name: '配速', type: 'line', yAxisIndex: 0, data: tItems.map((d) => d.avg_pace_s_per_km), connectNulls: true, smooth: true, itemStyle: { color: '#2563eb' } },
      { name: '心率', type: 'line', yAxisIndex: 1, data: tItems.map((d) => d.avg_hr), connectNulls: true, smooth: true, itemStyle: { color: '#ef4444' }, showSymbol: false },
    ],
  })
  charts.push(c5)
}

const onResize = () => charts.forEach((c) => c.resize())

onMounted(async () => {
  const [s, m, l, d, t, p] = await Promise.all([
    api.summary(30),
    api.metrics(),
    api.loadCurve(90),
    api.daily(60),
    api.trend(90),
    api.getProfile(),
  ])
  summary.value = s.data
  metrics.value = m.data
  loadItems.value = l.data.items
  dailyItems.value = d.data.items
  trendItems.value = t.data.items
  profile.value = p.data
  fetchActivities()
  render()
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  charts.forEach((c) => c.dispose())
})
</script>
