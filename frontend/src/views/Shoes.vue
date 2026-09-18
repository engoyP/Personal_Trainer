<template>
  <div class="card">
    <h3>我的跑鞋</h3>
    <div class="muted" style="font-size:12px;margin-bottom:12px">
      录入每双跑鞋、传张照片、记每日跑量，看它陪你跑了多少公里。
    </div>

    <!-- 添加鞋 -->
    <div class="add-form">
      <div class="row" style="margin-bottom:6px">
        <input v-model="form.name" placeholder="鞋名 * 如 亚瑟士 Kayano" style="flex:1;min-width:180px" />
        <input v-model="form.brand" placeholder="品牌（可选）" style="width:150px" />
        <input v-model="form.purchased_date" type="date" title="购入日期" />
      </div>
      <div class="row" style="margin-bottom:6px">
        <input v-model.number="form.initial_km" type="number" min="0" step="1" placeholder="购入时已有里程 km（可选）" style="width:200px" />
        <input v-model="form.note" placeholder="备注（可选），如 已跑 300km 退役" style="flex:1;min-width:180px" />
        <button class="primary" @click="create">添加跑鞋</button>
      </div>
      <div v-if="msg" :class="['tag', msgOk ? 'ok' : 'err']" style="margin-top:6px">{{ msg }}</div>
    </div>

    <div v-if="!shoes.length" class="empty">还没有录入跑鞋，先添一双</div>

    <div class="shoe-grid">
      <div v-for="s in shoes" :key="s.id" class="shoe-card" :class="{ retired: s.retired }">
        <div class="shoe-img" @click="pickImage(s)">
          <img v-if="s.has_image" :src="shoeImg(s)" :key="s.id + imgVer" alt="" />
          <div v-else class="shoe-ph">👟<br /><span>点我传照片</span></div>
        </div>
        <div class="shoe-body">
          <div class="shoe-name">
            {{ s.name }}
            <span v-if="s.retired" class="tag" style="font-size:11px;background:#f1f5f9">已退役</span>
          </div>
          <div class="muted" style="font-size:12px">
            {{ s.brand || '—' }}{{ s.purchased_date ? ' · ' + s.purchased_date : '' }}
          </div>
          <div class="shoe-km">
            <b>{{ s.total_km }}</b><span class="unit">km</span>
          </div>
          <div class="muted" style="font-size:11px">
            累计 {{ s.mileage_count }} 次记录{{ s.initial_km ? '（含初始 ' + s.initial_km + 'km）' : '' }}
          </div>
          <div v-if="s.note" class="muted" style="font-size:11px;margin-top:2px">{{ s.note }}</div>

          <div class="row" style="margin-top:8px">
            <input v-model="s._km" type="number" min="0.1" step="0.1" placeholder="跑量 km" style="width:88px" />
            <input v-model="s._date" type="date" style="width:128px" />
            <button @click="addKm(s)">记一笔</button>
            <button class="ghost" @click="openEdit(s)">编辑</button>
          </div>

          <div v-if="s._records && s._records.length" class="mileage-list">
            <div v-for="m in s._records" :key="m.id" class="mileage-row">
              <span>{{ m.run_date }}</span>
              <span><b>{{ m.km }}</b> km</span>
              <button class="m-x" @click="delKm(s, m)" title="删除">×</button>
            </div>
          </div>
        </div>
        <button class="shoe-del" @click="remove(s)" title="删除">×</button>
      </div>
    </div>

    <!-- 编辑弹窗 -->
    <div v-if="editing" class="modal-mask" @click.self="editing = null">
      <div class="modal">
        <h3 style="margin-top:0">编辑跑鞋</h3>
        <div class="field"><label>鞋名</label><input v-model="editing.name" /></div>
        <div class="field"><label>品牌</label><input v-model="editing.brand" /></div>
        <div class="field"><label>购入日期</label><input v-model="editing.purchased_date" type="date" /></div>
        <div class="field"><label>购入时已有里程 km</label><input v-model.number="editing.initial_km" type="number" min="0" step="1" /></div>
        <div class="field"><label>备注</label><input v-model="editing.note" /></div>
        <label class="row" style="gap:6px;font-size:13px;color:var(--text)">
          <input v-model="editing.retired" type="checkbox" /> 已退役（不再穿）
        </label>
        <div class="row" style="margin-top:12px;justify-content:flex-end">
          <button @click="editing = null">取消</button>
          <button class="primary" @click="saveEdit">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../api'

const shoes = ref([])
const imgVer = ref(0)
const form = ref({ name: '', brand: '', purchased_date: '', initial_km: '', note: '' })
const msg = ref('')
const msgOk = ref(true)
const editing = ref(null)

// 本地时区的今天（避免 toISOString 的 UTC 偏移把凌晨错成昨天）
const today = () => {
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

async function load() {
  const { data } = await api.shoes()
  shoes.value = data.items.map((s) => ({ ...s, _km: '', _date: today(), _records: [] }))
  for (const s of shoes.value) {
    try {
      const r = await api.shoeMileage(s.id)
      s._records = (r.data.items || []).slice(0, 8)
    } catch {
      s._records = []
    }
  }
}

function flash(ok, text) {
  msgOk.value = ok
  msg.value = text
  setTimeout(() => (msg.value = ''), 3000)
}

async function create() {
  const name = form.value.name.trim()
  if (!name) {
    flash(false, '鞋名不能为空')
    return
  }
  try {
    await api.createShoe({
      name,
      brand: form.value.brand.trim(),
      purchased_date: form.value.purchased_date || null,
      initial_km: parseFloat(form.value.initial_km) || 0,
      note: form.value.note.trim(),
    })
    form.value = { name: '', brand: '', purchased_date: '', initial_km: '', note: '' }
    flash(true, `已添加「${name}」`)
    await load()
  } catch (e) {
    flash(false, '添加失败：' + (e.response?.data?.detail || e.message))
  }
}

function shoeImg(s) {
  return `/api/shoes/${s.id}/image?t=${imgVer.value}`
}

function pickImage(s) {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = 'image/*'
  input.onchange = async () => {
    const f = input.files[0]
    if (!f) return
    await api.uploadShoeImage(s.id, f)
    imgVer.value++
    await load()
  }
  input.click()
}

async function addKm(s) {
  const km = parseFloat(s._km)
  if (!km || km <= 0) {
    flash(false, '请输入大于 0 的跑量')
    return
  }
  const date = s._date || today()
  try {
    const { data } = await api.addMileage(s.id, { run_date: date, km })
    s._km = ''
    flash(true, `已加 ${km}km，${date} 当天累计 ${data.day_km}km`)
    await load()
  } catch (e) {
    flash(false, '记跑量失败：' + (e.response?.data?.detail || e.message))
  }
}

async function remove(s) {
  if (!confirm(`删除跑鞋「${s.name}」及其所有跑量记录？`)) return
  await api.deleteShoe(s.id)
  await load()
}

async function delKm(s, m) {
  await api.deleteMileage(m.id)
  await load()
}

function openEdit(s) {
  editing.value = {
    id: s.id,
    name: s.name,
    brand: s.brand || '',
    purchased_date: s.purchased_date || '',
    initial_km: s.initial_km || 0,
    retired: !!s.retired,
    note: s.note || '',
  }
}

async function saveEdit() {
  const e = editing.value
  if (!e || !e.name.trim()) return
  try {
    await api.updateShoe(e.id, {
      name: e.name.trim(),
      brand: e.brand.trim(),
      purchased_date: e.purchased_date || null,
      initial_km: parseFloat(e.initial_km) || 0,
      retired: e.retired ? 1 : 0,
      note: e.note.trim(),
    })
    editing.value = null
    flash(true, '已保存')
    await load()
  } catch (err) {
    flash(false, '保存失败：' + (err.response?.data?.detail || err.message))
  }
}

onMounted(load)
</script>

<style scoped>
.add-form {
  margin-bottom: 16px;
  padding: 12px;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  background: #fafbfc;
}

.shoe-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 16px;
}

.shoe-card {
  position: relative;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  background: var(--card);
}

.shoe-card.retired {
  opacity: 0.65;
}

.shoe-img {
  height: 140px;
  background: #f6f8fa;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  overflow: hidden;
}

.shoe-img img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.shoe-ph {
  text-align: center;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}

.shoe-body {
  padding: 12px;
}

.shoe-name {
  font-weight: 600;
  font-size: 14px;
}

.shoe-km {
  margin: 6px 0 2px;
  font-size: 22px;
  color: #0f766e;
}

.shoe-km .unit {
  font-size: 12px;
  margin-left: 4px;
}

.shoe-del {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 24px;
  height: 24px;
  border: none;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.45);
  color: #fff;
  cursor: pointer;
  line-height: 1;
}

.mileage-list {
  margin-top: 8px;
  border-top: 1px dashed var(--border);
  padding-top: 6px;
  max-height: 150px;
  overflow-y: auto;
}

.mileage-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  padding: 2px 0;
}

.mileage-row .m-x {
  border: none;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
}

.mileage-row .m-x:hover {
  color: #dc2626;
}

.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal {
  background: #fff;
  border-radius: 12px;
  padding: 20px;
  width: 360px;
  max-width: 92vw;
  box-shadow: 0 20px 50px rgba(0, 0, 0, 0.2);
}
</style>
