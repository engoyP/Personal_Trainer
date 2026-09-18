<template>
  <div class="card">
    <h3>生成训练计划</h3>
    <div class="field">
      <label>这周你的情况与要求</label>
      <textarea v-model="request" rows="2" placeholder="例如：我想减脂，每周能练 4 天，右膝不太好别安排太多强度" />
    </div>
    <div class="row">
      <button class="primary" :disabled="busy" @click="generate()">
        {{ busy ? '生成中…' : '生成计划' }}
      </button>
      <span class="muted" style="font-size:12px">会先评估状态，生成后由安全规则校验，不通过会自动修正</span>
    </div>
    <div v-if="error" class="tag err mt">{{ error }}</div>
  </div>

  <template v-if="result">
    <div class="card">
      <h3>状态评估</h3>
      <p style="margin:0 0 12px">{{ result.assessment }}</p>
      <div class="row" style="margin-bottom:8px">
        <span class="tag err" v-for="r in result.risk_flags" :key="r">{{ r }}</span>
      </div>
      <div class="row">
        <span class="tag" v-for="c in result.constraints" :key="c">{{ c }}</span>
      </div>
    </div>

    <div class="card">
      <h3>安全校验</h3>
      <div class="row" style="margin-bottom:8px">
        <span :class="['tag', result.validation?.passed ? 'ok' : 'warn']">
          {{ result.validation?.passed ? '通过' : '已修正 ' + result.revision_count + ' 轮' }}
        </span>
        <span class="muted" style="font-size:12px">规则：10% 增幅上限 · 至少 1 天休息 · 高强度 ≤20% · ACWR>1.3 强制减量</span>
      </div>
      <div v-for="w in result.validation?.warnings || []" :key="w" class="tag warn" style="margin-right:6px">{{ w }}</div>
    </div>

    <div class="card">
      <h3>{{ result.draft_plan?.week_start }} 当周安排 · {{ result.draft_plan?.focus }}</h3>
      <table>
        <thead>
          <tr><th>日期</th><th>类型</th><th>时长</th><th>距离</th><th>心率区间</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="d in result.draft_plan?.days || []" :key="d.date">
            <td>{{ d.date }}</td>
            <td><span class="tag">{{ typeLabel(d.type) }}</span></td>
            <td>{{ d.duration_min }} 分</td>
            <td>{{ d.distance_km || 0 }} km</td>
            <td>{{ d.target_hr_zone || '—' }}</td>
            <td>{{ d.description }}</td>
          </tr>
        </tbody>
      </table>
      <div class="mt">计划总跑量：<strong>{{ result.draft_plan?.total_distance_km }} km</strong></div>
    </div>

    <!-- 饮食安排 -->
    <div v-if="result.draft_plan?.nutrition" class="card">
      <h3>饮食安排</h3>
      <div v-if="result.draft_plan.nutrition.target_kcal" class="grid grid-4" style="margin-bottom:12px">
        <div class="stat"><div class="label">每日热量</div><div class="value">{{ result.draft_plan.nutrition.target_kcal }}<span class="unit">kcal</span></div></div>
        <div class="stat"><div class="label">蛋白质</div><div class="value">{{ result.draft_plan.nutrition.protein_g }}<span class="unit">g</span></div></div>
        <div class="stat"><div class="label">脂肪</div><div class="value">{{ result.draft_plan.nutrition.fat_g }}<span class="unit">g</span></div></div>
        <div class="stat"><div class="label">碳水</div><div class="value">{{ result.draft_plan.nutrition.carb_g }}<span class="unit">g</span></div></div>
      </div>
      <div v-if="result.draft_plan.nutrition.deficit_kcal" class="muted" style="font-size:12px;margin-bottom:10px">
        每天缺口 {{ result.draft_plan.nutrition.deficit_kcal }} kcal · {{ result.draft_plan.nutrition.note }}</div>

      <div v-if="(result.draft_plan.nutrition.principles || []).length" style="margin-bottom:12px">
        <strong style="font-size:13px">执行原则</strong>
        <ul style="font-size:13px;line-height:1.9;margin:6px 0;padding-left:20px">
          <li v-for="(p, i) in result.draft_plan.nutrition.principles" :key="i">{{ p }}</li>
        </ul>
      </div>

      <div class="grid grid-2">
        <div v-for="(day, label) in mealBlocks(result.draft_plan.nutrition)" :key="label">
          <strong style="font-size:13px">{{ label }}</strong>
          <table style="margin-top:6px">
            <tbody>
              <tr v-for="(v, k) in day" :key="k">
                <td style="width:70px;color:var(--muted)">{{ mealName(k) }}</td>
                <td>{{ v }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="row">
        <button class="primary" :disabled="busy" @click="confirm">确认并保存</button>
        <input v-model="feedback" style="max-width:340px" placeholder="不满意？写修改意见后点重新生成" />
        <button :disabled="busy" @click="generate(feedback)">带意见重新生成</button>
      </div>
    </div>
  </template>

  <div v-if="confirmed" class="card">
    <h3>已保存</h3>
    <p style="margin:0">{{ confirmed.explanation }}</p>
  </div>

  <div class="card">
    <h3>历史计划</h3>
    <table v-if="plans.length">
      <thead><tr><th>起始周</th><th>状态</th><th>重点</th><th>生成时间</th><th style="width:96px"></th></tr></thead>
      <tbody>
        <tr v-for="p in plans" :key="p.id" class="clickable" @click="viewPlan = p">
          <td>{{ p.week_start }}</td>
          <td><span :class="['tag', p.status === 'confirmed' ? 'ok' : '']">{{ p.status }}</span></td>
          <td>{{ p.plan?.focus }}</td>
          <td>{{ p.created_at?.slice(0, 16).replace('T', ' ') }}</td>
          <td>
            <span class="muted" style="font-size:12px">查看 ›</span>
            <button class="del-btn" @click.stop="removePlan(p)" title="删除这条计划">删除</button>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">还没有生成过计划</div>
  </div>

  <!-- 历史计划详情弹窗 -->
  <div v-if="viewPlan" class="modal-mask" @click.self="viewPlan = null">
    <div class="modal plan-modal">
      <h3 style="margin-top:0">
        {{ viewPlan.week_start }} 当周安排
        <span :class="['tag', viewPlan.status === 'confirmed' ? 'ok' : '']" style="margin-left:8px">{{ viewPlan.status }}</span>
      </h3>
      <div class="muted" style="font-size:13px;margin-bottom:12px">{{ viewPlan.plan?.focus }}</div>

      <table>
        <thead><tr><th>日期</th><th>类型</th><th>时长</th><th>距离</th><th>心率区间</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="d in viewPlan.plan?.days || []" :key="d.date">
            <td>{{ d.date }}</td>
            <td><span class="tag">{{ typeLabel(d.type) }}</span></td>
            <td>{{ d.duration_min }} 分</td>
            <td>{{ d.distance_km || 0 }} km</td>
            <td>{{ d.target_hr_zone || '—' }}</td>
            <td>{{ d.description }}</td>
          </tr>
        </tbody>
      </table>
      <div class="mt">计划总跑量：<strong>{{ viewPlan.plan?.total_distance_km }} km</strong></div>

      <div v-if="viewPlan.plan?.nutrition" style="margin-top:14px">
        <strong style="font-size:13px">饮食安排</strong>
        <div v-if="viewPlan.plan.nutrition.target_kcal" class="row" style="gap:16px;font-size:13px;margin:6px 0">
          <span>每日 <b>{{ viewPlan.plan.nutrition.target_kcal }}</b> kcal</span>
          <span>蛋白 {{ viewPlan.plan.nutrition.protein_g }}g</span>
          <span>脂肪 {{ viewPlan.plan.nutrition.fat_g }}g</span>
          <span>碳水 {{ viewPlan.plan.nutrition.carb_g }}g</span>
        </div>
        <ul v-if="(viewPlan.plan.nutrition.principles || []).length"
            style="font-size:13px;line-height:1.9;margin:6px 0;padding-left:20px">
          <li v-for="(p, i) in viewPlan.plan.nutrition.principles" :key="i">{{ p }}</li>
        </ul>
        <div v-for="(day, label) in mealBlocks(viewPlan.plan.nutrition)" :key="label"
             class="muted" style="font-size:12px;margin-top:6px">
          <b style="color:var(--text)">{{ label }}：</b>
          {{ Object.entries(day).map(([k, v]) => mealName(k) + ' ' + v).join(' · ') }}
        </div>
      </div>

      <div v-if="viewPlan.explanation" class="plan-explanation">
        <div class="muted" style="font-size:12px;margin-bottom:4px">生成说明</div>
        <div style="white-space:pre-wrap;font-size:13px">{{ viewPlan.explanation }}</div>
      </div>

      <div class="row" style="margin-top:14px;justify-content:flex-end">
        <button class="primary" @click="viewPlan = null">关闭</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../api'

const request = ref('')
const feedback = ref('')
const result = ref(null)
const confirmed = ref(null)
const plans = ref([])
const busy = ref(false)
const error = ref('')
const viewPlan = ref(null)

const TYPES = {
  rest: '休息', easy: '轻松跑', tempo: '节奏跑', interval: '间歇',
  long: '长距离', strength: '力量', cross: '交叉训练',
}
const typeLabel = (t) => TYPES[t] || t

const MEALS = { breakfast: '早餐', lunch: '午餐', dinner: '晚餐', snack: '加餐' }
const mealName = (k) => MEALS[k] || k
function mealBlocks(nutrition) {
  const out = {}
  if (nutrition?.training_day_example) out['训练日'] = nutrition.training_day_example
  if (nutrition?.rest_day_example) out['休息日'] = nutrition.rest_day_example
  return out
}

async function generate(fb = '') {
  busy.value = true
  error.value = ''
  confirmed.value = null
  try {
    const { data } = await api.generatePlan({ request: request.value, feedback: fb })
    result.value = data
    feedback.value = ''
    loadPlans()
  } catch (e) {
    error.value = e.response?.data?.detail || e.message
  } finally {
    busy.value = false
  }
}

async function confirm() {
  busy.value = true
  try {
    const { data } = await api.confirmPlan(result.value.thread_id)
    confirmed.value = data
    result.value = null
    loadPlans()
  } catch (e) {
    error.value = e.response?.data?.detail || e.message
  } finally {
    busy.value = false
  }
}

async function loadPlans() {
  const { data } = await api.plans(10)
  plans.value = data.items
}

async function removePlan(p) {
  if (!confirm(`删除「${p.week_start}」这一周的计划？删了不能恢复。`)) return
  try {
    await api.deletePlan(p.id)
    if (viewPlan.value?.id === p.id) viewPlan.value = null
    await loadPlans()
  } catch (e) {
    alert('删除失败：' + (e.response?.data?.detail || e.message))
  }
}

onMounted(loadPlans)
</script>

<style scoped>
.del-btn {
  margin-left: 8px;
  padding: 2px 8px;
  font-size: 12px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: #fff;
  color: var(--muted);
  cursor: pointer;
}
.del-btn:hover {
  border-color: var(--danger);
  color: var(--danger);
}
</style>
