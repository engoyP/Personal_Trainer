<template>
  <div class="card">
    <h3>点亮去过的地方</h3>
    <div class="muted" style="font-size:12px;margin-bottom:12px">
      已点亮 {{ visited.length }} 座城市。在运动详情里点「添加去过的城市」也会同步到这里。
    </div>

    <div class="row" style="margin-bottom:6px">
      <input
        v-model="q"
        @input="onSearch"
        placeholder="输入城市名搜索并点亮，如：杭州"
        style="flex:1;min-width:200px"
      />
    </div>
    <div v-if="suggests.length" class="suggest">
      <button v-for="c in suggests" :key="c.name" class="suggest-item" @click="add(c)">
        + {{ c.name }} <span class="muted">{{ c.province }}</span>
      </button>
    </div>
    <div v-if="msg" :class="['tag', msgOk ? 'ok' : 'err']" style="margin-top:8px">{{ msg }}</div>

    <div ref="mapEl" class="map-box"></div>
    <div v-if="!mapOk" class="muted" style="font-size:12px;margin-top:6px">
      地图服务未加载，下方以列表展示（不影响记录与统计）
    </div>

    <div class="city-list">
      <span v-for="c in visited" :key="c.id" class="chip">
        📍 {{ c.city_name }}
        <span class="muted">{{ c.first_date || '' }}</span>
        <button class="chip-x" @click="remove(c)" title="取消点亮">×</button>
      </span>
      <span v-if="!visited.length" class="empty">还没有点亮任何城市</span>
    </div>
  </div>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import api from '../api'

const visited = ref([])
const suggests = ref([])
const q = ref('')
const msg = ref('')
const msgOk = ref(true)

const mapEl = ref(null)
const mapOk = ref(false)
let map = null
let layer = null

// 点亮图标：金色发光圆点（内联 SVG，不引用外部图片）
const LIT_ICON =
  'data:image/svg+xml,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" viewBox="0 0 28 28">' +
      '<circle cx="14" cy="14" r="10" fill="#f59e0b" fill-opacity="0.32"/>' +
      '<circle cx="14" cy="14" r="6" fill="#f59e0b" stroke="#ffffff" stroke-width="2"/>' +
      '</svg>'
  )

async function load() {
  const { data } = await api.visitedCities()
  visited.value = data
  renderMap()
}

let searchTimer = null
function onSearch() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(async () => {
    const k = q.value.trim()
    if (!k) {
      suggests.value = []
      return
    }
    const { data } = await api.citySearch(k)
    // 已点亮的城市不再出现在候选里
    const done = new Set(visited.value.map((c) => c.city_name))
    suggests.value = data.filter((c) => !done.has(c.name)).slice(0, 8)
  }, 200)
}

async function add(c) {
  msg.value = ''
  try {
    await api.addVisitedCity({ city_name: c.name })
    q.value = ''
    suggests.value = []
    msgOk.value = true
    msg.value = `已点亮 ${c.name}`
    await load()
  } catch (e) {
    msgOk.value = false
    msg.value = '添加失败：' + (e.response?.data?.detail || e.message)
  }
}

async function remove(c) {
  await api.deleteVisitedCity(c.id)
  await load()
}

function waitTMap(cb, tries = 25) {
  if (typeof window.TMap !== 'undefined') return cb()
  if (tries <= 0) return cb()
  setTimeout(() => waitTMap(cb, tries - 1), 120)
}

function renderMap() {
  if (typeof window.TMap === 'undefined') {
    mapOk.value = false
    return
  }
  mapOk.value = true
  const TMap = window.TMap

  nextTick(() => {
    if (!map) {
      // 默认矢量底图（不传 mapStyleId，避免自定义样式未开通导致底图空白）
      map = new TMap.Map(mapEl.value, {
        zoom: 4,
        center: new TMap.LatLng(34.5, 105.0),
      })
    }
    if (layer) {
      layer.setMap(null)
      layer = null
    }
    if (!visited.value.length) return

    layer = new TMap.MultiMarker({
      map,
      styles: {
        lit: new TMap.MarkerStyle({
          width: 28,
          height: 28,
          anchor: { x: 14, y: 14 },
          src: LIT_ICON,
        }),
      },
      geometries: visited.value.map((c) => ({
        id: String(c.id),
        styleId: 'lit',
        position: new TMap.LatLng(c.lat, c.lng),
      })),
    })

    // 有城市时把视野收到所有点亮城市的范围内
    try {
      if (visited.value.length === 1) {
        map.setCenter(new TMap.LatLng(visited.value[0].lat, visited.value[0].lng))
        map.setZoom(8)
      } else {
        const b = new TMap.LatLngBounds()
        visited.value.forEach((c) => b.extend(new TMap.LatLng(c.lat, c.lng)))
        map.fitBounds(b, { padding: 60 })
      }
    } catch {
      /* 视野调整失败不影响打点 */
    }
  })
}

onMounted(async () => {
  await load()
  waitTMap(renderMap)
})
watch(visited, () => waitTMap(renderMap))

onBeforeUnmount(() => {
  if (layer) layer.setMap(null)
  map = null
})
</script>

<style scoped>
.map-box {
  width: 100%;
  height: 440px;
  margin-top: 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  background: #fbfcfd;
}

.suggest {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.suggest-item {
  padding: 4px 10px;
  font-size: 12px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: #fff;
  cursor: pointer;
}

.suggest-item:hover {
  border-color: #f59e0b;
  color: #b45309;
}

.city-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 14px;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  font-size: 13px;
  border-radius: 999px;
  background: #fff7ed;
  color: #b45309;
  border: 1px solid #fed7aa;
}

.chip .muted {
  font-size: 11px;
}

.chip-x {
  border: none;
  background: transparent;
  cursor: pointer;
  font-size: 15px;
  line-height: 1;
  color: #c2410c;
  padding: 0 2px;
}
</style>
