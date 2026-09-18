<template>
  <div class="card">
    <div class="row" style="justify-content:space-between;align-items:flex-start">
      <div>
        <h3 style="margin-bottom:4px">训练知识</h3>
        <div class="muted" style="font-size:12px">
          每周自动爬取权威站点的运动训练与健康文章，按域名权威度打分入库，教练回答时会引用这里的内容
        </div>
      </div>
      <div class="row" style="gap:8px">
        <button @click="load">刷新</button>
        <button class="primary" :disabled="crawling || !keyword.trim()" @click="crawlKeyword">
          按关键词搜全网
        </button>
        <button :disabled="crawling" @click="crawl()">
          {{ crawling ? '爬取中…' : '爬权威源一轮' }}
        </button>
      </div>
    </div>
    <div class="row" style="margin-top:12px">
      <input
        v-model="keyword"
        placeholder="输入关键词搜全网爬取，如：拉伸姿势教学"
        style="flex:1;min-width:200px"
        @keyup.enter="crawlKeyword"
      />
      <label class="row" style="gap:6px;font-size:13px;white-space:nowrap" title="只收政府/国际权威机构的内容，方案更权威但条数会明显变少">
        <input v-model="onlyAuth" type="checkbox" /> 仅权威源
      </label>
    </div>
    <div class="muted" style="font-size:12px;margin-top:6px">
      关键词模式：多路搜索（含 <code>site:gov.cn</code> / NHS / WHO 权威域）+ 按域名权威度排序
      → 抓正文 → 图片 OCR 补文字 → 抽取入库。权威源排前面、优先抓。
      <template v-if="onlyAuth">当前<strong>仅收权威源</strong>，内容会少但更可信。</template>
    </div>
  </div>

  <!-- 概览 -->
  <div class="grid grid-4">
    <div class="stat">
      <div class="label">收录文章</div>
      <div class="value">{{ stats.total || 0 }}<span class="unit">篇</span></div>
    </div>
    <div class="stat">
      <div class="label">来源站点</div>
      <div class="value">{{ (stats.sources || []).length }}<span class="unit">个</span></div>
    </div>
    <div class="stat">
      <div class="label">上轮新增</div>
      <div class="value">{{ stats.last_run ? stats.last_run.inserted : '—' }}<span class="unit">篇</span></div>
    </div>
    <div class="stat">
      <div class="label">上轮状态</div>
      <div class="value" style="font-size:16px">
        <span class="tag" :class="runTagClass">{{ runStatusText }}</span>
      </div>
    </div>
  </div>

  <div class="grid grid-2">
    <!-- 主题分布 -->
    <div class="card">
      <h3>主题分布</h3>
      <div v-if="categories.length" style="display:flex;flex-wrap:wrap;gap:8px">
        <button v-for="c in categories" :key="c.key"
                :class="{ primary: filter.category === c.key }"
                @click="pickCategory(c.key)">
          {{ c.label }} · {{ c.count }}
        </button>
        <button v-if="filter.category" @click="pickCategory('')">清除筛选</button>
      </div>
      <div v-else class="empty">知识库还是空的，点右上角「立即爬取一轮」</div>
    </div>

    <!-- 权威来源 -->
    <div class="card">
      <h3>权威来源与权重</h3>
      <div class="muted" style="font-size:12px;margin-bottom:8px">
        取「域名后缀」与「站点白名单」中较高的那个。政府 .gov 1.0，学术 .edu/.ac 0.95，学会 .org 0.85
      </div>
      <table>
        <thead><tr><th>站点</th><th>域名</th><th>权重</th><th>已收</th></tr></thead>
        <tbody>
          <tr v-for="s in sourceRows" :key="s.id">
            <td>{{ s.name }}</td>
            <td class="muted" style="font-size:12px">{{ s.domain }}</td>
            <td><span class="tag" :class="s.authority >= 0.95 ? 'ok' : ''">{{ s.authority.toFixed(2) }}</span></td>
            <td>{{ s.article_count }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 文章列表 -->
  <div class="card">
    <div class="row" style="justify-content:space-between;margin-bottom:10px">
      <h3 style="margin:0">文章列表 <span class="muted" style="font-size:12px">共 {{ total }}</span></h3>
      <div class="row" style="gap:8px">
        <input v-model="filter.q" placeholder="搜标题 / 摘要 / 要点" style="width:220px"
               @keyup.enter="reload" />
        <select v-model="filter.sort" @change="reload">
          <option value="score">按综合分</option>
          <option value="published">按发布时间</option>
          <option value="recent">按收录时间</option>
        </select>
      </div>
    </div>

    <table v-if="items.length">
      <thead>
        <tr>
          <th style="width:44%">标题</th><th>来源机构</th><th>主题</th>
          <th>证据</th><th>发布</th><th>分值</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="a in items" :key="a.id" class="clickable" @click="open(a)">
          <td>
            <div style="font-weight:500">{{ a.title }}</div>
            <div class="muted" style="font-size:12px;margin-top:2px">{{ a.summary }}</div>
          </td>
          <td class="muted" style="font-size:12px">{{ a.organization || a.domain }}</td>
          <td><span class="tag">{{ catLabel(a.category) }}</span></td>
          <td><span class="tag" :class="evClass(a.evidence_level)">{{ evLabel(a.evidence_level) }}</span></td>
          <td class="muted" style="font-size:12px">{{ a.published_at || '—' }}</td>
          <td><strong>{{ a.score.toFixed(2) }}</strong></td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">没有匹配的文章</div>

    <div v-if="total > filter.limit" class="row" style="justify-content:center;margin-top:12px">
      <button :disabled="filter.offset === 0" @click="page(-1)">上一页</button>
      <span class="muted" style="font-size:12px">
        {{ filter.offset + 1 }}–{{ Math.min(filter.offset + filter.limit, total) }} / {{ total }}
      </span>
      <button :disabled="filter.offset + filter.limit >= total" @click="page(1)">下一页</button>
    </div>
  </div>

  <!-- 详情抽屉 -->
  <div v-if="current" ref="detailEl" class="card">
    <div class="row" style="justify-content:space-between">
      <h3 style="margin:0">{{ current.title }}</h3>
      <button @click="current = null">关闭</button>
    </div>
    <div class="row muted" style="font-size:12px;gap:12px;margin:8px 0 12px">
      <span>{{ current.organization || current.domain }}</span>
      <span v-if="current.author">作者：{{ current.author }}</span>
      <span v-if="current.published_at">{{ current.published_at }}</span>
      <span class="tag">{{ catLabel(current.category) }}</span>
      <span class="tag" :class="evClass(current.evidence_level)">{{ evLabel(current.evidence_level) }}</span>
      <a :href="current.url" target="_blank" rel="noreferrer">原文 ↗</a>
    </div>

    <div class="grid grid-3" style="margin-bottom:12px">
      <div class="stat"><div class="label">域名权威</div><div class="value">{{ current.domain_score }}</div></div>
      <div class="stat"><div class="label">时效</div><div class="value">{{ current.recency_score }}</div></div>
      <div class="stat"><div class="label">主题相关</div><div class="value">{{ current.relevance_score }}</div></div>
    </div>

    <div v-if="current.summary" style="margin-bottom:12px">
      <strong>摘要</strong>
      <p style="font-size:13px;line-height:1.7;margin:6px 0">{{ current.summary }}</p>
    </div>

    <div v-if="(current.key_points || []).length" style="margin-bottom:12px">
      <strong>要点</strong>
      <ul style="font-size:13px;line-height:1.8;margin:6px 0;padding-left:20px">
        <li v-for="(p, i) in current.key_points" :key="i">{{ p }}</li>
      </ul>
    </div>

    <details v-if="current.content_md">
      <summary style="cursor:pointer;font-size:13px">展开正文（{{ current.word_count }} 字，仅本地查看，不喂给模型）</summary>
      <pre class="md">{{ current.content_md }}</pre>
    </details>
  </div>

  <!-- 爬取历史 -->
  <div class="card">
    <h3>爬取历史</h3>
    <table v-if="runs.length">
      <thead>
        <tr><th>时间</th><th>触发</th><th>状态</th><th>发现</th><th>抓成功</th>
            <th>新增</th><th>更新</th><th>跳过</th><th>失败</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in runs" :key="r.id">
          <td class="muted" style="font-size:12px">{{ fmtTime(r.started_at) }}</td>
          <td><span class="tag">{{ r.trigger }}</span></td>
          <td><span class="tag" :class="runClass(r.status)">{{ r.status }}</span></td>
          <td>{{ r.discovered }}</td><td>{{ r.fetched }}</td>
          <td><strong>{{ r.inserted }}</strong></td><td>{{ r.updated }}</td>
          <td>{{ r.skipped }}</td><td>{{ r.failed }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">还没有爬取记录</div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onBeforeUnmount, reactive, ref } from 'vue'
import api from '../api'

const stats = ref({})
const categories = ref([])
const items = ref([])
const total = ref(0)
const runs = ref([])
const current = ref(null)
const detailEl = ref(null)
const crawling = ref(false)
const keyword = ref('')
const onlyAuth = ref(false)

const filter = reactive({ q: '', category: '', sort: 'score', limit: 20, offset: 0 })

let timer = null

const CAT = {
  training: '训练方法', nutrition: '营养补给', health: '健康指标',
  recovery: '恢复睡眠', injury: '伤病防护', weight_loss: '减脂控重',
}
const EV = {
  guideline: '指南', review: '综述', rct: '随机对照', popular: '科普',
  unknown: '未判定',
}

const catLabel = (k) => CAT[k] || k
const evLabel = (k) => EV[k] || k
const evClass = (k) => (k === 'guideline' ? 'ok' : k === 'popular' || k === 'unknown' ? '' : 'warn')
const runClass = (s) => (s === 'done' ? 'ok' : s === 'failed' ? 'err' : 'warn')

const runStatusText = computed(() => {
  const r = stats.value.last_run
  if (!r) return '还没跑过'
  return { done: '正常', failed: '失败', warn: '有告警', running: '进行中' }[r.status] || r.status
})
const runTagClass = computed(() => {
  const r = stats.value.last_run
  if (!r) return ''
  return r.status === 'done' ? 'ok' : r.status === 'failed' ? 'err' : 'warn'
})

// 只展示真正会参与爬取的站点，权重排序，取前 12 个避免太长
const sourceRows = computed(() =>
  (stats.value.sources || []).filter((s) => s.enabled).slice(0, 12))

const fmtTime = (s) => (s ? s.slice(0, 16).replace('T', ' ') : '—')

async function reload() {
  filter.offset = 0
  await loadArticles()
}

function page(dir) {
  filter.offset = Math.max(0, filter.offset + dir * filter.limit)
  loadArticles()
}

function pickCategory(k) {
  filter.category = filter.category === k ? '' : k
  reload()
}

async function loadArticles() {
  const { data } = await api.kbArticles({
    q: filter.q || undefined,
    category: filter.category || undefined,
    sort: filter.sort,
    limit: filter.limit,
    offset: filter.offset,
  })
  items.value = data.items
  total.value = data.total
}

async function load() {
  const [s, c, r] = await Promise.all([api.kbStats(), api.kbCategories(), api.kbRuns(10)])
  stats.value = s.data
  categories.value = c.data.items
  runs.value = r.data.items
  await loadArticles()
}

async function crawl() {
  crawling.value = true
  try {
    await api.kbCrawl({ trigger: 'manual', max_per_source: 10 })
  } finally {
    // 后台跑着，轮询状态而不是阻塞界面
    poll()
  }
}

async function crawlKeyword() {
  const k = keyword.value.trim()
  if (!k) return
  crawling.value = true
  try {
    await api.kbCrawl({
      trigger: 'manual',
      keyword: k,
      max_per_source: 15,
      min_authority: onlyAuth.value ? 0.85 : 0,
    })
  } finally {
    poll()
  }
}

function poll() {
  clearInterval(timer)
  timer = setInterval(async () => {
    const { data } = await api.kbCrawlStatus()
    crawling.value = data.running
    if (!data.running) {
      clearInterval(timer)
      await load()
    }
  }, 5000)
}

async function open(a) {
  const { data } = await api.kbArticle(a.id)
  current.value = data
  // 详情卡片在列表下方：滚动到详情区，而不是回到页面顶部
  await nextTick()
  detailEl.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

onMounted(async () => {
  await load()
  const { data } = await api.kbCrawlStatus()
  crawling.value = data.running
  if (data.running) poll()
})

onBeforeUnmount(() => clearInterval(timer))
</script>

<style scoped>
.clickable { cursor: pointer; }
.clickable:hover { background: #f8f9fa; }
pre.md {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  line-height: 1.7;
  max-height: 420px;
  overflow: auto;
  background: #f8f9fa;
  padding: 12px;
  border-radius: 8px;
  margin-top: 8px;
}
</style>
