<template>
  <div class="card">
    <h3>导入运动文件</h3>
    <div class="row">
      <input type="file" accept=".gpx,.tcx,.csv,.json" style="max-width:320px" @change="onPick" />
      <button :disabled="!file || busy" @click="doProbe">先看结构</button>
      <button class="primary" :disabled="!file || busy" @click="doUpload">导入</button>
    </div>
    <div class="muted" style="font-size:12px;margin-top:8px">
      支持 GPX / TCX / <b>CSV</b> / 华为导出 JSON。
      <br />华为运动健康 App 三个入口用途不同：
      <br />· 单次运动 → 分享/更多 → <b>导出数据</b>：逐点 CSV，<b>含心率</b>（推荐）
      <br />· 单次运动 → 轨迹图标 → 导出路线文件：GPX / TCX，只有轨迹，<b>不含心率和步频</b>
      <br />· 我的 → 运动数据 → 导出：每天一行的汇总表，<b>无法导入</b>
      <br />只有轨迹的文件导入后心率区间会留空、训练负荷按配速估算。
    </div>

    <div v-if="probeResult" class="mt">
      <div class="tag">识别结果</div>
      <div v-if="probeResult.error" class="tag err" style="margin-top:6px">{{ probeResult.error }}</div>
      <template v-else>
        <div class="muted" style="font-size:12px;margin-top:6px">
          类型 {{ probeResult.sport_type }}，采样点 {{ probeResult.candidates?.[0]?.count ?? probeResult.candidates?.length }}
        </div>
        <div v-for="c in probeResult.candidates || []" :key="c.path" class="muted" style="font-size:12px">
          {{ c.path }} · {{ c.count }} 条 · 字段 {{ (c.keys || []).join(', ') }}
        </div>
      </template>
      <div v-for="w in probeResult.warnings || []" :key="w" class="tag warn" style="margin-top:6px">{{ w }}</div>
    </div>

    <div v-if="uploadMsg" :class="['tag', uploadOk ? 'ok' : 'err']" style="margin-top:10px">{{ uploadMsg }}</div>
  </div>

  <div class="card">
    <h3>手动录入运动</h3>
    <div class="muted" style="font-size:12px;margin-bottom:10px">
      只有华为 summary 截图、没有 GPS/CSV 文件时，可以在这里直接录入汇总数据。
      <br />不伪造逐点心率：负荷有平均心率时按 TRIMP 算，没有时按配速估算，心率区间留空。
    </div>
    <div class="muted" style="font-size:12px;margin-bottom:10px">
      📷 上传或拖拽华为运动海报/截图，自动识别汇总数据
    </div>
    <div
      class="recog-drop"
      :class="{ dragging: dragOver }"
      @dragover.prevent="dragOver = true"
      @dragleave.prevent="dragOver = false"
      @drop.prevent="onDrop"
      @click="fileInput?.click()"
      :style="{ opacity: recogBusy ? 0.6 : 1 }"
    >
      <input ref="fileInput" type="file" accept="image/*" @change="onRecognizeFile" :disabled="recogBusy" hidden />
      <template v-if="recogBusy">
        <div style="font-size:28px">🔍</div>
        <div style="margin-top:6px">识别中…</div>
      </template>
      <template v-else-if="recogPreview">
        <img :src="recogPreview" alt="预览" style="max-height:120px;max-width:100%;border-radius:6px" />
        <div style="font-size:12px;color:#94a3b8;margin-top:6px">点击或拖拽可重新选择图片</div>
      </template>
      <template v-else>
        <div style="font-size:28px">📷</div>
        <div style="margin-top:6px">拖拽图片到这里，或点击选择文件</div>
      </template>
    </div>
    <div v-if="recogMsg" :class="['tag', recogOk ? 'ok' : 'err']" style="margin-top:6px">{{ recogMsg }}</div>
    <div v-if="recogNotes.length" class="muted" style="font-size:12px;margin-top:6px">提示：{{ recogNotes.join('；') }}</div>
    <div class="grid grid-4">
      <div class="field"><label>日期</label><input v-model="manual.date" type="date" /></div>
      <div class="field"><label>时间</label><input v-model="manual.time" type="time" /></div>
      <div class="field"><label>运动类型</label>
        <select v-model="manual.sport_type">
          <option value="running">跑步</option>
          <option value="cycling">骑行</option>
          <option value="walking">步行</option>
          <option value="swimming">游泳</option>
        </select>
      </div>
      <div class="field"><label>距离 km</label><input v-model.number="manual.distance_km" type="number" step="0.01" /></div>
      <div class="field"><label>时长 分钟</label><input v-model.number="manual.duration_min" type="number" step="0.1" /></div>
      <div class="field"><label>平均心率</label><input v-model.number="manual.avg_hr" type="number" placeholder="选填" /></div>
      <div class="field"><label>最大心率</label><input v-model.number="manual.max_hr" type="number" placeholder="选填" /></div>
      <div class="field"><label>平均步频</label><input v-model.number="manual.avg_cadence_spm" type="number" placeholder="选填" /></div>
      <div class="field"><label>平均步幅 m</label><input v-model.number="manual.avg_stride_m" type="number" step="0.01" placeholder="选填" /></div>
      <div class="field"><label>累计爬升 m</label><input v-model.number="manual.elev_gain_m" type="number" placeholder="选填" /></div>
      <div class="field"><label>卡路里 kcal</label><input v-model.number="manual.calories" type="number" placeholder="选填，默认按体重估算" /></div>
    </div>
    <button class="primary" :disabled="busy" @click="doManual">录入运动</button>
    <div v-if="manualMsg" :class="['tag', manualOk ? 'ok' : 'err']" style="margin-top:10px">{{ manualMsg }}</div>
  </div>

  <div class="card">
    <h3>个人档案</h3>
    <div class="grid grid-3">
      <div class="field"><label>性别</label>
        <select v-model="form.gender"><option value="male">男</option><option value="female">女</option></select>
      </div>
      <div class="field"><label>出生年份</label><input v-model.number="form.birth_year" type="number" /></div>
      <div class="field"><label>身高 cm</label><input v-model.number="form.height_cm" type="number" /></div>
      <div class="field"><label>最大心率</label><input v-model.number="form.max_hr" type="number" /></div>
      <div class="field"><label>静息心率</label><input v-model.number="form.resting_hr" type="number" /></div>
      <div class="field"><label>目标体重 kg</label><input v-model.number="form.target_weight_kg" type="number" /></div>
      <div class="field"><label>每周可训练天数</label><input v-model.number="form.weekly_available_days" type="number" /></div>
      <div class="field"><label>目标</label>
        <select v-model="form.goal">
          <option value="fat_loss">减脂</option>
          <option value="endurance">提升耐力</option>
          <option value="speed">提升速度</option>
          <option value="health">保持健康</option>
        </select>
      </div>
      <div class="field"><label>经验</label>
        <select v-model="form.experience">
          <option value="beginner">新手</option>
          <option value="intermediate">进阶</option>
          <option value="advanced">资深</option>
        </select>
      </div>
    </div>
    <div class="field">
      <label>伤病史（会写进计划约束）</label>
      <input v-model="form.injury_notes" placeholder="如：右膝偶尔不适，避免连续两天高强度" />
    </div>
    <button class="primary" @click="saveProfile">保存档案</button>
    <span v-if="pSaved" class="tag ok" style="margin-left:10px">已保存</span>
  </div>

  <div class="card">
    <h3>记录今天的身体数据</h3>
    <div class="grid grid-4">
      <div class="field"><label>日期</label><input v-model="daily.date" type="date" /></div>
      <div class="field"><label>体重 kg</label><input v-model.number="daily.weight_kg" type="number" step="0.1" /></div>
      <div class="field"><label>睡眠 小时</label><input v-model.number="daily.sleep_h" type="number" step="0.1" /></div>
      <div class="field"><label>静息心率</label><input v-model.number="daily.resting_hr" type="number" /></div>
    </div>
    <button class="primary" @click="saveDaily">保存</button>
    <span v-if="dSaved" class="tag ok" style="margin-left:10px">已保存</span>
    <div class="mt">
      <button :disabled="busy" @click="doRecalc">按当前档案重算历史数据</button>
      <span class="muted" style="font-size:12px;margin-left:8px">改了体重或最大心率后需要点一次</span>
    </div>
    <div v-if="recalcMsg" class="tag ok" style="margin-top:10px">{{ recalcMsg }}</div>
  </div>

  <div class="card">
    <h3>最近身体数据</h3>
    <table v-if="dailyItems.length">
      <thead><tr><th>日期</th><th>体重</th><th>睡眠</th><th>静息心率</th></tr></thead>
      <tbody>
        <tr v-for="d in dailyItems" :key="d.date">
          <td>{{ d.date }}</td><td>{{ d.weight_kg }} kg</td><td>{{ d.sleep_h }} h</td><td>{{ d.resting_hr }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">还没有记录</div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../api'

const form = ref({})
const daily = ref({ date: new Date().toISOString().slice(0, 10) })
const dailyItems = ref([])
const file = ref(null)
const probeResult = ref(null)
const uploadMsg = ref('')
const uploadOk = ref(false)
const manual = ref({
  date: new Date().toISOString().slice(0, 10),
  time: '06:00',
  sport_type: 'running',
  distance_km: '',
  duration_min: '',
  avg_hr: '',
  max_hr: '',
  avg_cadence_spm: '',
  avg_stride_m: '',
  elev_gain_m: '',
  calories: ''
})
const manualMsg = ref('')
const manualOk = ref(false)
const pSaved = ref(false)
const dSaved = ref(false)
const recalcMsg = ref('')
const busy = ref(false)
const recogBusy = ref(false)
const recogOk = ref(false)
const recogMsg = ref('')
const recogNotes = ref([])
const dragOver = ref(false)
const recogPreview = ref('')
const fileInput = ref(null)

const onPick = (e) => {
  file.value = e.target.files[0]
  probeResult.value = null
  uploadMsg.value = ''
}

async function doProbe() {
  busy.value = true
  try {
    const { data } = await api.probe(file.value)
    probeResult.value = data
  } catch (e) {
    uploadMsg.value = '无法识别：' + (e.response?.data?.detail || e.message)
    uploadOk.value = false
  } finally {
    busy.value = false
  }
}

// 通用：把图片文件送去识别并预填表单（点击上传 / 拖拽共用）
async function recognizeFile(f) {
  if (!f) return
  recogBusy.value = true
  recogMsg.value = ''
  recogOk.value = false
  recogNotes.value = []
  // 本地预览
  recogPreview.value = await new Promise((res) => {
    const r = new FileReader()
    r.onload = () => res(r.result)
    r.onerror = () => res('')
    r.readAsDataURL(f)
  })
  try {
    const b64 = await new Promise((res, rej) => {
      const r = new FileReader()
      r.onload = () => res(r.result)
      r.onerror = rej
      r.readAsDataURL(f)
    })
    const { data } = await api.recognize(b64)
    if (!data.ok) {
      recogOk.value = false
      recogMsg.value = data.error || '识别失败'
      return
    }
    const fl = data.fields || {}
    // 日期时间：从 start_time 拆成 date + time，避免 toISOString 时区偏移
    const m = (fl.start_time || '').match(/(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/)
    if (m) { manual.value.date = m[1]; manual.value.time = m[2] }
    if (fl.sport_type) manual.value.sport_type = fl.sport_type
    if (fl.distance_km != null) manual.value.distance_km = fl.distance_km
    if (fl.duration_min != null) manual.value.duration_min = fl.duration_min
    if (fl.avg_hr != null) manual.value.avg_hr = fl.avg_hr
    if (fl.max_hr != null) manual.value.max_hr = fl.max_hr
    if (fl.avg_cadence_spm != null) manual.value.avg_cadence_spm = fl.avg_cadence_spm
    if (fl.avg_stride_m != null) manual.value.avg_stride_m = fl.avg_stride_m
    if (fl.elev_gain_m != null) manual.value.elev_gain_m = fl.elev_gain_m
    if (fl.calories != null) manual.value.calories = fl.calories
    recogOk.value = true
    recogMsg.value = `已识别 ${data.block_count} 个文本块，请核对后点「录入运动」`
    recogNotes.value = data.notes || []
  } catch (err) {
    recogOk.value = false
    recogMsg.value = '识别失败：' + (err.response?.data?.error || err.response?.data?.detail || err.message)
  } finally {
    recogBusy.value = false
  }
}

function onRecognizeFile(e) {
  recognizeFile(e.target.files[0])
  e.target.value = ''   // 允许重复选同一张图
}

function onDrop(e) {
  dragOver.value = false
  const f = e.dataTransfer?.files?.[0]
  if (f) recognizeFile(f)
}

async function doManual() {
  busy.value = true
  manualMsg.value = ''
  try {
    const payload = {
      sport_type: manual.value.sport_type,
      start_time: `${manual.value.date}T${manual.value.time}`,
      duration_min: parseFloat(manual.value.duration_min),
      distance_km: parseFloat(manual.value.distance_km),
      avg_hr: manual.value.avg_hr !== '' ? parseFloat(manual.value.avg_hr) : null,
      max_hr: manual.value.max_hr !== '' ? parseFloat(manual.value.max_hr) : null,
      avg_cadence_spm: manual.value.avg_cadence_spm !== '' ? parseFloat(manual.value.avg_cadence_spm) : null,
      avg_stride_m: manual.value.avg_stride_m !== '' ? parseFloat(manual.value.avg_stride_m) : null,
      elev_gain_m: manual.value.elev_gain_m !== '' ? parseFloat(manual.value.elev_gain_m) : null,
      calories: manual.value.calories !== '' ? parseFloat(manual.value.calories) : null,
    }
    const { data } = await api.manualActivity(payload)
    manualOk.value = data.ok
    if (!data.ok) {
      manualMsg.value = data.reason
      return
    }
    const est = data.summary.load_source === 'met_estimate'
    manualMsg.value =
      `录入成功：${(data.summary.distance_m / 1000).toFixed(2)} km，心率 ${data.summary.avg_hr ?? '无'}，负荷 ${data.summary.load}` +
      (est ? '（无平均心率，负荷由配速估算）' : '') +
      (data.warning ? ` ⚠ ${data.warning}` : '')
  } catch (e) {
    manualOk.value = false
    manualMsg.value = e.response?.data?.detail || e.message
  } finally {
    busy.value = false
  }
}

async function doUpload() {
  busy.value = true
  try {
    const { data } = await api.upload(file.value)
    uploadOk.value = data.ok
    if (!data.ok) {
      uploadMsg.value = data.reason
      return
    }
    // 华为导出的 GPX/TCX 只有轨迹，不含心率和步频。这时候负荷是配速估算的，
    // 必须当场讲清楚，否则用户会以为是实测值。
    const est = data.summary.load_source === 'met_estimate'
    uploadMsg.value =
      `导入成功：${(data.summary.distance_m / 1000).toFixed(2)} km，心率 ${data.summary.avg_hr ?? '无'}，步频 ${data.summary.avg_cadence_spm ?? '无'}` +
      (est ? '（文件无心率，训练负荷由配速估算，心率区间留空）' : '') +
      (data.warning ? ` ⚠ ${data.warning}` : '')
  } catch (e) {
    uploadOk.value = false
    uploadMsg.value = e.response?.data?.detail || e.message
  } finally {
    busy.value = false
  }
}

async function saveProfile() {
  await api.updateProfile(form.value)
  pSaved.value = true
}

async function saveDaily() {
  await api.upsertDaily(daily.value)
  dSaved.value = true
  loadDaily()
}

async function doRecalc() {
  busy.value = true
  const { data } = await api.recalc()
  recalcMsg.value = `已重算 ${data.recalculated} 条运动的负荷与卡路里`
  busy.value = false
}

async function loadDaily() {
  const { data } = await api.daily(14)
  dailyItems.value = data.items.slice().reverse()
}

onMounted(async () => {
  const { data } = await api.getProfile()
  form.value = data
  loadDaily()
})
</script>
