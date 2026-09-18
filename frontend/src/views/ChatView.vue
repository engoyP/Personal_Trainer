<template>
  <div class="chat-layout">
    <!-- 左栏：会话历史 + 快捷提问 -->
    <aside class="chat-side">
      <button class="primary" style="width:100%;margin-bottom:10px" @click="newSession">＋ 新对话</button>

      <div class="side-block">
        <div class="side-title">历史会话</div>
        <div v-if="!sessions.length" class="muted" style="font-size:12px;padding:6px 2px">还没有会话</div>
        <div
          v-for="s in sessions"
          :key="s.id"
          class="session"
          :class="{ active: s.id === curId }"
          @click="openSession(s)"
        >
          <div class="session-title">{{ s.title }}</div>
          <div class="session-time">{{ fmtTime(s.updated_at) }}</div>
        </div>
      </div>

      <div class="side-block">
        <div class="side-title">快捷提问</div>
        <button v-for="q in quick" :key="q" class="quick-btn" :disabled="busy" @click="ask(q)">{{ q }}</button>
      </div>
    </aside>

    <!-- 中栏：对话 -->
    <section class="chat-main">
      <div ref="boxEl" class="chat-box">
        <div v-if="!messages.length" class="empty">教练已经读过你的数据了，直接问吧</div>
        <div v-for="(m, i) in messages" :key="i" :class="['msg', m.role]">
          <div class="bubble">{{ m.content }}</div>
        </div>
        <div v-if="busy" class="msg assistant"><div class="bubble muted">思考中…</div></div>
      </div>
      <div class="row mt">
        <input v-model="text" placeholder="问你的教练，回车发送" @keyup.enter="send" />
        <button class="primary" :disabled="busy || !text.trim()" @click="send">发送</button>
      </div>
    </section>

    <!-- 右栏：引用来源 + 数据上下文 -->
    <aside class="chat-ctx">
      <div class="side-block">
        <div class="side-title">引用来源</div>
        <div v-if="!lastSources.length" class="muted" style="font-size:12px">暂无引用</div>
        <a
          v-for="(s, j) in lastSources"
          :key="j"
          :href="s.url"
          target="_blank"
          rel="noreferrer"
          class="src-line"
          :title="s.title"
        >
          [{{ j + 1 }}] {{ s.organization || s.title }}
        </a>
      </div>

      <div class="side-block">
        <div class="side-title">教练能看到的数据</div>
        <ul class="ctx-list">
          <li>近 30 天训练负荷（ATL/CTL/TSB）</li>
          <li>平均配速、心率、步频、步幅</li>
          <li>心率区间分布</li>
          <li>体重 / 睡眠 / 静息心率趋势</li>
          <li>周跑量与训练计划</li>
        </ul>
        <div class="muted" style="font-size:11px">拿不到逐点采样数据，只看聚合指标</div>
      </div>
    </aside>
  </div>
</template>

<script setup>
import { computed, nextTick, ref } from 'vue'
import api from '../api'

const messages = ref([])
const text = ref('')
const busy = ref(false)
const threadId = ref(null)
const boxEl = ref(null)
const sessions = ref([])
const curId = ref(null)

const quick = [
  '我最近状态怎么样',
  '我今天该跑多少',
  '我的步频和步幅正常吗',
  '我这样减脂速度健康吗',
]

const lastSources = computed(() => {
  for (let i = messages.value.length - 1; i >= 0; i--) {
    const m = messages.value[i]
    if (m.role === 'assistant' && m.sources?.length) return m.sources
  }
  return []
})

const fmtTime = (ts) => {
  const d = new Date(ts)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

const scroll = () => nextTick(() => {
  if (boxEl.value) boxEl.value.scrollTop = boxEl.value.scrollHeight
})

// ---- 会话持久化（localStorage）----
function loadSessions() {
  try {
    sessions.value = JSON.parse(localStorage.getItem('coach_sessions') || '[]')
  } catch {
    sessions.value = []
  }
}
function saveSessions() {
  localStorage.setItem('coach_sessions', JSON.stringify(sessions.value))
}
function saveCurrent() {
  const s = sessions.value.find((x) => x.id === curId.value)
  if (!s) return
  s.messages = messages.value
  s.thread_id = threadId.value
  s.updated_at = Date.now()
  const firstUser = messages.value.find((m) => m.role === 'user')
  if (firstUser) s.title = firstUser.content.slice(0, 18)
  saveSessions()
}

function newSession() {
  saveCurrent()
  const id = String(Date.now())
  sessions.value.unshift({ id, title: '新对话', messages: [], thread_id: null, updated_at: Date.now() })
  curId.value = id
  messages.value = []
  threadId.value = null
  saveSessions()
}

function openSession(s) {
  if (s.id === curId.value) return
  saveCurrent()
  curId.value = s.id
  messages.value = s.messages || []
  threadId.value = s.thread_id || null
  scroll()
}

// ---- 对话 ----
async function send() {
  const q = text.value.trim()
  if (!q || busy.value) return
  text.value = ''
  if (!curId.value) newSession()
  messages.value.push({ role: 'user', content: q })
  const aiMsg = { role: 'assistant', content: '', sources: [] }
  messages.value.push(aiMsg)
  busy.value = true
  scroll()

  try {
    const resp = await fetch('/api/agent/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: q, thread_id: threadId.value }),
    })
    if (!resp.ok) {
      aiMsg.content = '出错了：' + resp.status
      return
    }
    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const raw = buf.slice(0, idx)
        buf = buf.slice(idx + 2)
        const line = raw.split('\n').find((l) => l.startsWith('data: '))
        if (!line) continue
        let ev
        try { ev = JSON.parse(line.slice(6)) } catch { continue }
        if (ev.type === 'thread_id') threadId.value = ev.thread_id
        else if (ev.type === 'delta') { aiMsg.content += ev.text; scroll() }
        else if (ev.type === 'sources') aiMsg.sources = ev.sources
        else if (ev.type === 'error') aiMsg.content += '\n[错误] ' + ev.error
      }
    }
    saveCurrent()
  } catch (e) {
    aiMsg.content += '\n出错了：' + e.message
  } finally {
    busy.value = false
    scroll()
  }
}

const ask = (q) => {
  text.value = q
  send()
}

loadSessions()
// 有历史则打开最近一个会话
if (sessions.value.length) {
  const s = sessions.value[0]
  curId.value = s.id
  messages.value = s.messages || []
  threadId.value = s.thread_id || null
}
</script>

<style scoped>
.chat-layout {
  display: grid;
  grid-template-columns: 230px 1fr 260px;
  gap: 14px;
  align-items: start;
}

.chat-side,
.chat-ctx {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.side-block {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--card);
  padding: 12px;
}

.side-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--text);
}

.session {
  padding: 7px 8px;
  border-radius: 8px;
  cursor: pointer;
  margin-bottom: 2px;
}

.session:hover {
  background: #f6f8fa;
}

.session.active {
  background: #e6f5f2;
}

.session-title {
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.session-time {
  font-size: 11px;
  color: var(--muted);
}

.quick-btn {
  display: block;
  width: 100%;
  text-align: left;
  margin-bottom: 6px;
  font-size: 12px;
  padding: 6px 10px;
}

.chat-main {
  display: flex;
  flex-direction: column;
}

.chat-box {
  height: 520px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px;
  background: #fbfcfd;
}

.msg { display: flex; margin-bottom: 12px; }
.msg.user { justify-content: flex-end; }

.bubble {
  max-width: 82%;
  padding: 9px 13px;
  border-radius: 12px;
  font-size: 13px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

.msg.user .bubble {
  background: var(--accent);
  color: #fff;
  border-bottom-right-radius: 4px;
}

.msg.assistant .bubble {
  background: #fff;
  border: 1px solid var(--border);
  border-bottom-left-radius: 4px;
}

input { flex: 1; }
button { white-space: nowrap; }

.src-line {
  display: block;
  font-size: 12px;
  color: #0f766e;
  text-decoration: none;
  padding: 3px 0;
  border-bottom: 1px dashed #e2e8f0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.src-line:hover { color: #0b857a; }

.ctx-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--text);
  line-height: 1.8;
}

@media (max-width: 960px) {
  .chat-layout { grid-template-columns: 1fr; }
  .chat-side, .chat-ctx { flex-direction: row; flex-wrap: wrap; }
}
</style>
