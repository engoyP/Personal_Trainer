<template>
  <div v-if="loading" class="empty">加载中…</div>
  <div v-else-if="!act.id" class="empty">记录不存在</div>
  <template v-else>
    <div class="card" style="padding:10px 20px">
      <div class="row" style="justify-content:space-between">
        <div>
          <b style="font-size:16px">{{ sportName(act.sport_type) }}</b>
          <span class="muted" style="margin-left:10px">{{ fmtTime(act.start_time) }}</span>
          <span v-if="act.source" class="tag" style="margin-left:8px">{{ sourceName(act.source) }}</span>
        </div>
        <div class="muted" style="font-size:12px">负荷 {{ act.load ?? '—' }}<template v-if="act.load_source">（{{ act.load_source === 'trimp' ? '心率实测' : '配速估算' }}）</template></div>
      </div>
    </div>

    <div class="grid grid-4">
      <div class="stat"><div class="label">距离</div><div class="value">{{ act.distance_km }}<span class="unit">km</span></div></div>
      <div class="stat"><div class="label">时长</div><div class="value">{{ act.duration_min }}<span class="unit">分</span></div></div>
      <div class="stat"><div class="label">平均配速</div><div class="value">{{ pace(act.avg_pace_s_per_km) }}</div></div>
      <div class="stat"><div class="label">平均心率</div><div class="value">{{ act.avg_hr || '—' }}<span class="unit">bpm</span></div></div>
      <div class="stat"><div class="label">最大心率</div><div class="value">{{ act.max_hr || '—' }}</div></div>
      <div class="stat"><div class="label">平均步频</div><div class="value">{{ act.avg_cadence_spm || '—' }}<span class="unit">spm</span></div></div>
      <div class="stat"><div class="label">平均步幅</div><div class="value">{{ act.avg_stride_m || '—' }}<span class="unit">m</span></div></div>
      <div class="stat"><div class="label">累计爬升</div><div class="value">{{ act.elev_gain_m ?? '—' }}<span class="unit">m</span></div></div>
      <div class="stat"><div class="label">卡路里</div><div class="value">{{ act.calories ?? '—' }}<span class="unit">kcal</span></div></div>
    </div>

    <div v-if="act.has_sample_hr === false" class="card" style="border-left:3px solid #f59e0b">
      <h3>{{ act.avg_hr ? '已补填心率' : '补填心率（可选）' }}</h3>
      <div class="muted" style="font-size:12px;margin-bottom:10px">
        <template v-if="act.avg_hr">
          当前负荷 <b>{{ act.load }}</b> 是按你填的心率 {{ act.avg_hr }} bpm 算的（TRIMP）。
          数值可以改，或点清空回退到配速估算。
        </template>
        <template v-else>
          这条记录的文件里没有心率字段（华为导出的 GPX / TCX 只有轨迹），
          当前负荷 <b>{{ act.load }}</b> 是按配速估的。
          到华为运动健康 App 里看一眼这次的平均心率填进来，负荷就会换成真实的 TRIMP。
        </template>
        心率区间仍会留空——只有一个平均值，不编造逐点分布。
      </div>
      <div class="row">
        <div class="field" style="max-width:150px">
          <label>平均心率</label>
          <input v-model.number="hr.avg_hr" type="number" min="30" max="250" placeholder="如 145" />
        </div>
        <div class="field" style="max-width:150px">
          <label>最大心率</label>
          <input v-model.number="hr.max_hr" type="number" min="30" max="250" placeholder="如 168" />
        </div>
        <button class="primary" :disabled="!hr.avg_hr || hrSaving" @click="saveHeartRate">
          {{ hrSaving ? '保存中…' : '应用' }}
        </button>
        <button v-if="act.avg_hr" @click="clearHeartRate">清空</button>
      </div>
      <div v-if="hrMsg" :class="['tag', hrOk ? 'ok' : 'err']" style="margin-top:10px">{{ hrMsg }}</div>
    </div>

    <div class="card">
      <h3>去过的城市</h3>
      <div class="muted" style="font-size:12px;margin-bottom:10px">
        这次运动在哪个城市跑的？标记后会在「去过的地方」地图里点亮。
      </div>
      <div class="row">
        <input
          v-model="cityQ"
          @input="onCitySearch"
          placeholder="输入城市名搜索，如：杭州"
          style="flex:1;min-width:200px"
        />
      </div>
      <div v-if="citySuggests.length" class="city-suggest">
        <button v-for="c in citySuggests" :key="c.name" class="suggest-item" @click="addCity(c)">
          + {{ c.name }} <span class="muted">{{ c.province }}</span>
        </button>
      </div>
      <div v-if="cityMsg" :class="['tag', cityOk ? 'ok' : 'err']" style="margin-top:8px">{{ cityMsg }}</div>
      <div v-if="actCities.length" class="city-chips">
        <span v-for="c in actCities" :key="c.id" class="chip">📍 {{ c.city_name }}</span>
      </div>
    </div>

    <div class="card">
      <h3>跑步轨迹</h3>
      <TrackMap v-if="trackPoints.length" :tracks="[{ points: trackPoints }]" :height="340" />
      <div v-else class="empty">这次运动没有记录到 GPS 轨迹</div>
    </div>

    <div class="card">
      <h3>心率与配速</h3>
      <div ref="hrEl" class="chart"></div>
      <div class="muted" style="font-size:12px">配速轴已反转，曲线越靠上代表跑得越快</div>
    </div>

    <div class="card">
      <h3>步频与步幅</h3>
      <div ref="cadEl" class="chart"></div>
    </div>

    <div class="card">
      <h3>运动后主观反馈</h3>
      <div class="grid grid-3">
        <div class="field">
          <label>RPE 自觉用力（1-10）</label>
          <input v-model="fb.rpe" type="number" min="1" max="10" />
        </div>
        <div class="field">
          <label>疲劳感（1-5）</label>
          <input v-model="fb.fatigue" type="number" min="1" max="5" />
        </div>
        <div class="field">
          <label>肌肉酸痛（1-5）</label>
          <input v-model="fb.soreness" type="number" min="1" max="5" />
        </div>
        <div class="field">
          <label>状态</label>
          <input v-model="fb.mood" placeholder="轻松 / 一般 / 很累" />
        </div>
        <div class="field">
          <label>疼痛部位</label>
          <input v-model="fb.pain" placeholder="如：右膝轻微不适" />
        </div>
        <div class="field">
          <label>备注</label>
          <input v-model="fb.note" placeholder="想说的都写这里" />
        </div>
      </div>
      <button class="primary" @click="saveFeedback">保存反馈</button>
      <span v-if="saved" class="tag ok" style="margin-left:10px">已保存</span>
    </div>
  </template>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import * as echarts from 'echarts'
import api from '../api'
import TrackMap from '../components/TrackMap.vue'

const route = useRoute()
const act = ref({})
const loading = ref(true)
const saved = ref(false)
const hrEl = ref(null)
const cadEl = ref(null)
let charts = []

const fb = ref({ rpe: null, fatigue: null, soreness: null, mood: '', pain: '', note: '' })
const trackPoints = ref([])

const hr = ref({ avg_hr: null, max_hr: null })
const hrSaving = ref(false)
const hrMsg = ref('')
const hrOk = ref(false)

// 去过的城市
const cityQ = ref('')
const citySuggests = ref([])
const cityMsg = ref('')
const cityOk = ref(false)
const actCities = ref([])

let cityTimer = null
function onCitySearch() {
  clearTimeout(cityTimer)
  cityTimer = setTimeout(async () => {
    const k = cityQ.value.trim()
    if (!k) {
      citySuggests.value = []
      return
    }
    const { data } = await api.citySearch(k)
    const done = new Set(actCities.value.map((c) => c.city_name))
    citySuggests.value = data.filter((c) => !done.has(c.name)).slice(0, 8)
  }, 200)
}

async function addCity(c) {
  cityMsg.value = ''
  try {
    // 带上 activity_id：后端会自动把这次运动的日期作为首次达成日期
    await api.addVisitedCity({ city_name: c.name, activity_id: Number(route.params.id) })
    cityQ.value = ''
    citySuggests.value = []
    cityOk.value = true
    cityMsg.value = `已点亮 ${c.name}，可以在「去过的地方」地图里看到`
    await loadCities()
  } catch (e) {
    cityOk.value = false
    cityMsg.value = '添加失败：' + (e.response?.data?.detail || e.message)
  }
}

async function loadCities() {
  const { data } = await api.visitedCities()
  actCities.value = data.filter((c) => c.activity_id === Number(route.params.id))
}

const pace = (s) => {
  if (!s) return '—'
  return `${Math.floor(s / 60)}'${String(Math.round(s % 60)).padStart(2, '0')}"`
}

const SPORT = { running: '跑步', cycling: '骑行', walking: '步行', swimming: '游泳', hiking: '徒步' }
const sportName = (s) => SPORT[s] || s || '运动'
const SOURCE = { gpx: 'GPX 轨迹', tcx: 'TCX 轨迹', huawei: '华为数据', csv: 'CSV 明细', manual: '手动/识图录入' }
const sourceName = (s) => SOURCE[s] || s || ''
const fmtTime = (s) => (s ? s.slice(0, 16).replace('T', ' ') : '')

function render(series) {
  charts.forEach((c) => c.dispose())
  charts = []
  const xs = series.map((s) => Math.round(s.t / 60))

  const c1 = echarts.init(hrEl.value)
  c1.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['心率', '配速'], bottom: 0 },
    grid: { left: 55, right: 55, top: 20, bottom: 40 },
    xAxis: { type: 'category', data: xs, name: '分钟' },
    yAxis: [
      { type: 'value', name: 'bpm' },
      { type: 'value', name: '秒/km', inverse: true },
    ],
    series: [
      { name: '心率', type: 'line', data: series.map((s) => s.hr), itemStyle: { color: '#ef4444' }, smooth: true, showSymbol: false },
      { name: '配速', type: 'line', yAxisIndex: 1, data: series.map((s) => s.pace), itemStyle: { color: '#0d9488' }, smooth: true, showSymbol: false },
    ],
  })
  charts.push(c1)

  const c2 = echarts.init(cadEl.value)
  c2.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['步频', '步幅'], bottom: 0 },
    grid: { left: 55, right: 55, top: 20, bottom: 40 },
    xAxis: { type: 'category', data: xs, name: '分钟' },
    yAxis: [
      { type: 'value', name: 'spm' },
      { type: 'value', name: 'm' },
    ],
    series: [
      { name: '步频', type: 'line', data: series.map((s) => s.cad), itemStyle: { color: '#2563eb' }, smooth: true, showSymbol: false },
      { name: '步幅', type: 'line', yAxisIndex: 1, data: series.map((s) => s.stride), itemStyle: { color: '#f59e0b' }, smooth: true, showSymbol: false },
    ],
  })
  charts.push(c2)
}

const onResize = () => charts.forEach((c) => c.resize())

async function saveFeedback() {
  const payload = Object.fromEntries(
    Object.entries(fb.value).filter(([, v]) => v !== '' && v !== null)
  )
  await api.setFeedback(act.value.id, payload)
  saved.value = true
}

async function saveHeartRate() {
  hrSaving.value = true
  hrMsg.value = ''
  try {
    const { data } = await api.setHeartRate(act.value.id, {
      avg_hr: hr.value.avg_hr || null,
      max_hr: hr.value.max_hr || null,
    })
    act.value = { ...act.value, ...data }
    hrOk.value = true
    hrMsg.value = `已按心率 ${data.avg_hr} bpm 重算，负荷 ${data.load}（来源 ${data.load_source}）`
  } catch (e) {
    hrOk.value = false
    hrMsg.value = e.response?.data?.detail || e.message
  } finally {
    hrSaving.value = false
  }
}

async function clearHeartRate() {
  hr.value = { avg_hr: null, max_hr: null }
  hrSaving.value = true
  try {
    const { data } = await api.setHeartRate(act.value.id, { avg_hr: null, max_hr: null })
    act.value = { ...act.value, ...data }
    hrOk.value = true
    hrMsg.value = '已清空，负荷回到配速估算'
  } catch (e) {
    hrOk.value = false
    hrMsg.value = e.response?.data?.detail || e.message
  } finally {
    hrSaving.value = false
  }
}

onMounted(async () => {
  try {
    const { data } = await api.activity(route.params.id)
    act.value = data
    if (data.feedback) fb.value = { ...fb.value, ...data.feedback }
    if (data.avg_hr) hr.value = { avg_hr: data.avg_hr, max_hr: data.max_hr ?? null }
    try {
      await loadCities()
    } catch {
      actCities.value = []
    }
    try {
      const t = await api.track(route.params.id)
      trackPoints.value = (t.data.points || []).map((p) => [p.lat, p.lon])
    } catch {
      trackPoints.value = []
    }
    render(data.series || [])
    window.addEventListener('resize', onResize)
  } catch (e) {
    // 记录不存在或接口异常：不再卡「加载中」，直接落到「记录不存在」
    act.value = {}
    console.error('加载运动详情失败', e)
  } finally {
    loading.value = false
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  charts.forEach((c) => c.dispose())
})
</script>
