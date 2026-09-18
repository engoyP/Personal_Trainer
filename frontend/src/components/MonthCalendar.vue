<template>
  <div class="card">
    <div class="cal-head">
      <h3 style="margin:0">运动日历</h3>
      <div class="cal-nav">
        <button @click="shift(-1)">‹</button>
        <span style="min-width:90px;text-align:center">{{ year }} 年 {{ month }} 月</span>
        <button @click="shift(1)">›</button>
      </div>
    </div>

    <div class="cal-grid cal-week">
      <span v-for="w in ['一','二','三','四','五','六','日']" :key="w">{{ w }}</span>
    </div>
    <div class="cal-grid">
      <div
        v-for="(d, i) in cells"
        :key="i"
        class="cal-cell"
        :class="{ dim: d === null, today: isToday(d) }"
      >
        <template v-if="d !== null">
          <div class="cal-day">{{ d }}</div>
          <div class="cal-tags">
            <span
              v-for="(a, j) in acts(d)"
              :key="j"
              class="cal-tag"
              @click="open(a.id)"
            >{{ sportName(a.sport_type) }} {{ a.distance_km }}km</span>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'

const router = useRouter()
const now = new Date()
const year = ref(now.getFullYear())
const month = ref(now.getMonth() + 1)
const days = ref({})

const SPORT = { running: '跑步', cycling: '骑行', walking: '步行', swimming: '游泳', hiking: '徒步' }
const sportName = (s) => SPORT[s] || s || '运动'
const open = (id) => router.push(`/activity/${id}`)

const cells = computed(() => {
  const first = new Date(year.value, month.value - 1, 1)
  const firstDow = (first.getDay() + 6) % 7            // 周一=0
  const n = new Date(year.value, month.value, 0).getDate()
  const arr = []
  for (let i = 0; i < firstDow; i++) arr.push(null)
  for (let d = 1; d <= n; d++) arr.push(d)
  while (arr.length % 7) arr.push(null)
  return arr
})

const dkey = (d) => `${year.value}-${String(month.value).padStart(2, '0')}-${String(d).padStart(2, '0')}`
const acts = (d) => days.value[dkey(d)] || []
const isToday = (d) =>
  d !== null && dkey(d) === `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`

async function load() {
  const { data } = await api.activityCalendar(year.value, month.value)
  days.value = data.days || {}
}

function shift(delta) {
  let m = month.value + delta
  let y = year.value
  if (m < 1) { m = 12; y-- }
  if (m > 12) { m = 1; y++ }
  month.value = m
  year.value = y
  load()
}

onMounted(load)
</script>

<style scoped>
.cal-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.cal-nav {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 13px;
}

.cal-nav button {
  width: 28px;
  height: 28px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: #fff;
  cursor: pointer;
  font-size: 16px;
  line-height: 1;
}

.cal-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 4px;
}

.cal-week {
  margin-bottom: 4px;
}

.cal-week span {
  text-align: center;
  font-size: 12px;
  color: var(--muted);
  padding: 4px 0;
}

.cal-cell {
  min-height: 64px;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 4px 5px;
  background: #fff;
  font-size: 12px;
  overflow: hidden;
}

.cal-cell.dim {
  background: transparent;
  border-color: transparent;
}

.cal-cell.today {
  border-color: #0d9488;
  background: #f0fdfa;
}

.cal-day {
  font-weight: 600;
  margin-bottom: 3px;
}

.cal-tags {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cal-tag {
  display: block;
  font-size: 10px;
  line-height: 1.4;
  padding: 1px 5px;
  border-radius: 4px;
  background: #0d9488;
  color: #fff;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cal-tag:hover {
  background: #0f766e;
}
</style>
