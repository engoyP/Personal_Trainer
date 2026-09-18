<template>
  <div class="trace-panel" :class="{ open: open }">
    <button class="trace-toggle" @click="open = !open" :title="open ? '收起' : '展开执行链路'">
      <span v-if="open">◀</span><span v-else>▶</span>
      <span v-if="!open" class="toggle-label">链路</span>
    </button>

    <div v-if="open" class="trace-body">
      <div class="trace-head">
        <span class="trace-title">执行链路</span>
        <div class="trace-actions">
          <button
            class="follow-btn"
            :class="{ active: following }"
            @click="toggleFollow"
            :title="following ? '正在跟随最新日志，点击停止' : '点击跟随最新日志'"
          >{{ following ? '⦿ 追踪中' : '○ 追踪' }}</button>
          <button @click="paused = !paused">{{ paused ? '▶ 继续' : '⏸ 暂停' }}</button>
          <button @click="clear">清空</button>
        </div>
      </div>
      <div ref="listEl" class="trace-list" @scroll="onScroll">
        <div v-if="!events.length" class="trace-empty">等待事件…（生成方案 / 对话 / 导入时会有流水）</div>
        <div v-for="e in events" :key="e.id" class="trace-line" :class="e.level">
          <span class="t-time">{{ e.ts }}</span>
          <span class="t-label">{{ e.label }}</span>
          <span v-if="e.detail" class="t-detail">{{ e.detail }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import api from '../api'

const open = ref(false)
const paused = ref(false)
const following = ref(true)
const events = ref([])
const listEl = ref(null)
let sinceId = 0
let timer = null

const atBottom = () => {
  const el = listEl.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight < 30
}

const scroll = () => nextTick(() => {
  const el = listEl.value
  if (el && following.value) el.scrollTop = el.scrollHeight
})

// 用户手动滚动：滑到底部附近就自动恢复跟随，往上翻则停住
function onScroll() {
  following.value = atBottom()
}

function toggleFollow() {
  following.value = !following.value
  if (following.value) scroll()
}

async function poll() {
  if (paused.value) return
  try {
    const { data } = await api.trace(sinceId)
    sinceId = data.since_id
    if (data.events?.length) {
      events.value = events.value.concat(data.events)
      // 最多保留 500 条，避免 DOM 爆炸
      if (events.value.length > 500) events.value = events.value.slice(-500)
      scroll()
    }
  } catch {
    /* 后端没起时静默 */
  }
}

function clear() {
  events.value = []
}

onMounted(() => {
  poll()
  timer = setInterval(poll, 1000)
})

onBeforeUnmount(() => {
  clearInterval(timer)
})
</script>

<style scoped>
.trace-panel {
  position: sticky;
  top: 0;
  height: 100vh;
  width: 44px;
  flex-shrink: 0;
  background: #0d1117;
  border-left: 1px solid #21262d;
  display: flex;
  transition: width 0.2s;
  overflow: hidden;
}

.trace-panel.open {
  width: 25%;
  min-width: 300px;
}

.trace-toggle {
  flex-shrink: 0;
  width: 44px;
  height: 100%;
  background: #161b22;
  border: none;
  color: #8b949e;
  cursor: pointer;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding-top: 16px;
  gap: 10px;
}

.trace-toggle:hover {
  color: #c9d1d9;
}

.toggle-label {
  writing-mode: vertical-rl;
  font-size: 12px;
  letter-spacing: 2px;
}

.trace-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.trace-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border-bottom: 1px solid #21262d;
  flex-shrink: 0;
}

.trace-title {
  color: #c9d1d9;
  font-size: 13px;
  font-weight: 600;
}

.trace-actions button {
  background: #21262d;
  border: 1px solid #30363d;
  color: #c9d1d9;
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 5px;
  cursor: pointer;
  margin-left: 6px;
}

.trace-actions button:hover {
  background: #30363d;
}

.trace-actions .follow-btn.active {
  background: #1f6feb;
  border-color: #388bfd;
  color: #fff;
}

.trace-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px 10px;
  font-family: 'Cascadia Code', Consolas, 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.7;
}

.trace-empty {
  color: #484f58;
  font-size: 12px;
  padding: 12px 4px;
}

.trace-line {
  padding: 1px 0;
  white-space: normal;
  word-break: break-all;
}

.t-time {
  color: #484f58;
  margin-right: 6px;
}

.t-label {
  font-weight: 600;
}

.t-detail {
  color: #8b949e;
  margin-left: 6px;
}

.trace-line.route .t-label { color: #58a6ff; }
.trace-line.llm .t-label { color: #3fb950; }
.trace-line.node .t-label { color: #d29922; }
.trace-line.tool .t-label { color: #bc8cff; }
.trace-line.semantic .t-label { color: #39c5cf; }
.trace-line.info .t-label { color: #8b949e; }
.trace-line.error .t-label { color: #f85149; }
</style>
