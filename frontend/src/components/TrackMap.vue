<template>
  <div>
    <div v-show="mapOk" ref="mapEl" class="map-box" :style="{ height: height + 'px' }"></div>

    <div v-if="!mapOk" class="fallback" :style="{ height: height + 'px' }">
      <svg v-if="svgTracks.length" :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="xMidYMid meet">
        <polyline
          v-for="(t, i) in svgTracks"
          :key="i"
          :points="t.d"
          :stroke="t.color"
          fill="none"
          stroke-width="2.2"
          stroke-linejoin="round"
          stroke-linecap="round"
        />
        <circle v-for="(c, i) in svgEnds" :key="'s' + i" :cx="c.x" :cy="c.y" r="4.5" :fill="c.color" />
        <circle v-for="(c, i) in svgEnds" :key="'e' + i" :cx="c.to.x" :cy="c.to.y" r="3" fill="#fff" :stroke="c.color" stroke-width="2" />
      </svg>
      <div v-else class="empty">这次运动没有记录到 GPS 轨迹</div>
      <div v-if="svgTracks.length" class="tip">地图服务未加载，当前显示轨迹轮廓（已做 GCJ-02 坐标转换）</div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { toGcjPoints } from '../utils/geo'

const props = defineProps({
  tracks: { type: Array, default: () => [] },
  height: { type: Number, default: 340 },
})

const W = 640
const H = 360
const COLORS = ['#0d9488', '#2563eb', '#f59e0b', '#dc2626', '#8b5cf6', '#0891b2']

const mapEl = ref(null)
const mapOk = ref(false)
let map = null
let layer = null

const gcjTracks = computed(() =>
  props.tracks
    .map((t, i) => ({
      ...t,
      pts: toGcjPoints(t.points),
      color: t.color || COLORS[i % COLORS.length],
    }))
    .filter((t) => t.pts.length >= 2)
)

const svgTracks = computed(() => {
  const all = gcjTracks.value.flatMap((t) => t.pts)
  if (all.length < 2) return []

  const lats = all.map((p) => p[0])
  const lons = all.map((p) => p[1])
  const minLa = Math.min(...lats)
  const maxLa = Math.max(...lats)
  const minLo = Math.min(...lons)
  const maxLo = Math.max(...lons)

  const pad = 22
  const sx = maxLo - minLo || 0.001
  const sy = maxLa - minLa || 0.001
  const scale = Math.min((W - 2 * pad) / sx, (H - 2 * pad) / sy)
  const ox = (W - (maxLo - minLo) * scale) / 2
  const oy = (H - (maxLa - minLa) * scale) / 2

  const project = ([la, lo]) => [
    ox + (lo - minLo) * scale,
    H - (oy + (la - minLa) * scale),
  ]

  return gcjTracks.value.map((t) => {
    const proj = t.pts.map(project)
    return {
      d: proj.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' '),
      color: t.color,
      from: proj[0],
      to: proj[proj.length - 1],
    }
  })
})

const svgEnds = computed(() =>
  svgTracks.value.map((t) => ({
    color: t.color,
    x: t.from[0],
    y: t.from[1],
    to: { x: t.to[0], y: t.to[1] },
  }))
)

function waitTMap(cb, tries = 25) {
  if (typeof window.TMap !== 'undefined') return cb()
  if (tries <= 0) return cb()
  setTimeout(() => waitTMap(cb, tries - 1), 120)
}

function renderMap() {
  const tracks = gcjTracks.value
  if (!tracks.length || typeof window.TMap === 'undefined') {
    mapOk.value = false
    return
  }

  mapOk.value = true
  const TMap = window.TMap

  nextTick(() => {
    const all = tracks.flatMap((t) => t.pts)
    const center = all[Math.floor(all.length / 2)]

    if (!map) {
      map = new TMap.Map(mapEl.value, {
        zoom: 14,
        center: new TMap.LatLng(center[0], center[1]),
      })
    }

    if (layer) {
      layer.setMap(null)
      layer = null
    }

    layer = new TMap.MultiPolyline({
      map,
      styles: Object.fromEntries(
        tracks.map((t, i) => [
          `s${i}`,
          new TMap.PolylineStyle({
            color: t.color,
            width: 5,
            borderWidth: 1,
            borderColor: '#ffffff',
            lineCap: 'round',
          }),
        ])
      ),
      geometries: tracks.map((t, i) => ({
        id: `t${i}`,
        styleId: `s${i}`,
        paths: t.pts.map(([la, lo]) => new TMap.LatLng(la, lo)),
      })),
    })

    try {
      const bounds = new TMap.LatLngBounds()
      all.forEach(([la, lo]) => bounds.extend(new TMap.LatLng(la, lo)))
      map.fitBounds(bounds, { padding: 50 })
    } catch {
      map.setCenter(new TMap.LatLng(center[0], center[1]))
    }
  })
}

onMounted(() => waitTMap(renderMap))
watch(() => props.tracks, () => waitTMap(renderMap))

onBeforeUnmount(() => {
  if (layer) layer.setMap(null)
  map = null
})
</script>

<style scoped>
.map-box,
.fallback {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  background: #fbfcfd;
}

.fallback {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 8px;
}

.fallback svg { width: 100%; height: 100%; }

.tip {
  font-size: 12px;
  color: var(--muted);
  padding-bottom: 6px;
}
</style>
