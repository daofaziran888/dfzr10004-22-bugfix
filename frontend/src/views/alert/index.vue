<template>
  <section class="page" data-module="alert">
    <header class="page-head">
      <div>
        <h2>采集告警中心管理</h2>
        <p class="page-desc">维护采集告警的监控指标、触发条件与通知策略，为采集异常告警提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记告警规则</button>
        <button class="btn" type="button" @click="exportRows">导出采集告警中心清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
          <th>详情</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">
            <span :class="cellClass(column, row)">{{ displayValue(column, row) }}</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
          <td>
            <button class="link" type="button" @click="openDetail(row)">查看详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无采集告警中心数据，可先登记告警规则</td>
        </tr>
      </tbody>
    </table>

    <section v-if="detail" class="detail-panel" data-detail="alert">
      <header class="detail-head">
        <h3>告警规则详情 · {{ detail.规则编号 }}</h3>
        <button class="btn ghost" type="button" @click="closeDetail">关闭</button>
      </header>
      <div class="detail-grid">
        <article><span>当前状态</span><strong>{{ detail.status }}</strong></article>
        <article><span>是否启用</span><strong>{{ detail.是否启用 }}</strong></article>
        <article><span>异常标记</span><strong>{{ detail.异常标记 ? '是' : '否' }}</strong></article>
        <article><span>处理窗口</span><strong>{{ detail.处理窗口 }}</strong></article>
        <article><span>升级次数</span><strong>{{ detail.升级次数 }}</strong></article>
        <article><span>最近触发</span><strong>{{ detail.triggered_at || '—' }}</strong></article>
        <article><span>最近恢复</span><strong>{{ detail.resumed_at || '—' }}</strong></article>
        <article><span>告警级别</span><strong>{{ detail.告警级别 || '—' }}</strong></article>
      </div>

      <h4>状态流转历史</h4>
      <table class="data-table compact">
        <thead><tr><th>动作</th><th>原状态</th><th>新状态</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="item in history" :key="`${item.at}-${item.action}`">
            <td>{{ item.action }}</td>
            <td>{{ item.from }}</td>
            <td>{{ item.to }}</td>
            <td>{{ item.at }}</td>
          </tr>
          <tr v-if="!history.length"><td colspan="4" class="empty-state">尚无状态流转记录</td></tr>
        </tbody>
      </table>

      <h4>处置记录</h4>
      <table class="data-table compact">
        <thead><tr><th>处置编号</th><th>动作</th><th>结果</th><th>处理窗口</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="item in disposals" :key="item.处置编号">
            <td>{{ item.处置编号 }}</td>
            <td>{{ item.action }}</td>
            <td>{{ item.result }}</td>
            <td>{{ item.from }} → {{ item.to }}</td>
            <td>{{ item.at }}</td>
          </tr>
          <tr v-if="!disposals.length"><td colspan="5" class="empty-state">尚无处置记录</td></tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集告警中心记录</span>
      <span v-if="message" class="ok-text">{{ message }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>
type DetailRecord = Record<string, any>

const ENDPOINT = '/api/alert'
const columns = ['规则编号', '监控指标', '告警级别', '状态', '异常标记', '处理窗口', '是否启用']
const actionOrder = ['提交校验', '重开规则', '启用规则', '停用规则', '暂停升级', '恢复升级', '废弃规则']
const filterFields = ['规则编号', '监控指标']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const message = ref('')
const filters = ref<Record<string, string>>({})
const detail = ref<DetailRecord | null>(null)
const history = ref<DetailRecord[]>([])
const disposals = ref<DetailRecord[]>([])

const stats = computed(() => {
  const all = rows.value
  return [
    { label: '告警规则总数', value: total.value },
    { label: '待校验告警规则', value: all.filter((row) => row.status === '待校验').length },
    { label: '已启用告警规则', value: all.filter((row) => row.是否启用 === '是').length },
    { label: '异常标记', value: all.filter((row) => Boolean(row.abnormal)).length },
  ]
})

function displayValue(column: string, row: Row): string | number {
  const value = row[column]
  if (column === '状态') return String(row.status ?? '—')
  if (column === '异常标记') return row.abnormal ? '是' : '否'
  return value ?? '—'
}

function cellClass(column: string, row: Row): string {
  if (column === '异常标记') return row.abnormal ? 'badge danger' : 'badge'
  if (column === '是否启用') return row.是否启用 === '是' ? 'badge ok' : 'badge muted'
  return ''
}

function availableActions(row: Row): string[] {
  // 停用与启用各只保留一个方向，后端会拦掉非法流转并说明原因。
  if (row.status === '已废弃') return []
  const status = String(row.status ?? '')
  if (status === '草稿') return ['提交校验', '废弃规则']
  if (status === '待校验') return ['启用规则', '废弃规则']
  if (status === '已启用') return ['停用规则', '暂停升级', '恢复升级', '废弃规则']
  if (status === '已停用') return ['重开规则', '废弃规则']
  return actionOrder
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '告警规则登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  message.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json().catch(() => ({ ok: false, message: '动作返回内容无法解析' }))
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '采集告警中心动作未生效，请稍后重试')
    }
    message.value = payload.message ?? ''
    await reload()
    if (detail.value) await openDetail({ id: row.id })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集告警中心操作失败'
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const [entryResponse, historyResponse, disposalResponse] = await Promise.all([
      request(`${ENDPOINT}/${row.id}`),
      request(`${ENDPOINT}/${row.id}/history`),
      request(`${ENDPOINT}/${row.id}/disposals`),
    ])
    if (!entryResponse.ok || !historyResponse.ok || !disposalResponse.ok) {
      throw new Error('告警规则详情读取失败')
    }
    detail.value = await entryResponse.json()
    history.value = (await historyResponse.json()).items ?? []
    disposals.value = (await disposalResponse.json()).items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '告警规则详情读取失败'
  }
}

function closeDetail() {
  detail.value = null
  history.value = []
  disposals.value = []
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('告警规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集告警中心列表读取失败'
  }
}

onMounted(reload)
</script>
