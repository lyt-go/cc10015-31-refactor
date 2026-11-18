<template>
  <section class="page" data-module="metric_def">
    <header class="page-head">
      <div>
        <h2>指标定义管理</h2>
        <p class="page-desc">维护指标定义的输入、执行约束与结果记录，为指标定义提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记指标定义</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出指标定义清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="analysis-form" @submit.prevent="confirmVersions">
      <label>
        <span>公式版本策略</span>
        <select v-model="policy" data-testid="policy-select">
          <option v-for="item in policies" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <button class="btn primary" type="submit" data-testid="confirm-versions" :disabled="confirming">
        {{ confirming ? '核对中…' : 'V031核对并留档' }}
      </button>
      <span class="muted-text" data-testid="confirmation-summary">{{ confirmationSummary }}</span>
    </form>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>公式结果</th>
          <th>历史/当前</th>
          <th>核对状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td :data-testid="`result-${row.id}`">{{ confirmationOf(row)?.公式结果 ?? '—' }}</td>
          <td>{{ confirmationOf(row)?.历史结果 ?? '—' }} / {{ confirmationOf(row)?.当前结果 ?? '—' }}</td>
          <td>
            <button class="link" type="button" :data-testid="`detail-${row.id}`" @click="openDetail(row)">
              {{ confirmationOf(row)?.核对状态 ?? '未核对' }}
            </button>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无指标定义数据，可先登记指标定义</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showDetail" class="modal-backdrop" data-testid="metric-detail">
      <div class="modal">
        <h3>公式详情 · {{ detail?.指标名称 }}（{{ detail?.指标编号 }}）</h3>
        <p class="page-desc">
          V031 {{ detailSummary }}；详情页与列表页读取同一份留档核对结果。
        </p>
        <dl class="dag-detail" v-if="detail">
          <div><dt>计算公式（原文）</dt><dd>{{ detail['计算公式'] }}</dd></div>
          <div><dt>精度位数（原文）</dt><dd>{{ detail['精度位数'] }}</dd></div>
          <div><dt>定义结果</dt><dd data-testid="detail-definition-result">{{ detail['公式核对']?.['指标定义']?.['结果'] ?? '—' }}</dd></div>
          <div><dt>历史结果</dt><dd data-testid="detail-historical-result">{{ detail['公式核对']?.['历史口径']?.['结果'] ?? '—' }}</dd></div>
          <div><dt>当前结果</dt><dd data-testid="detail-current-result">{{ detail['公式核对']?.['当前口径']?.['结果'] ?? '—' }}</dd></div>
          <div><dt>选中口径</dt><dd>{{ detail['公式核对']?.['选中口径'] }} · {{ detail['公式核对']?.['选中结果']?.['结果'] ?? '—' }}</dd></div>
          <div><dt>版本冲突</dt><dd>{{ detail['公式核对']?.['冲突说明'] ?? '—' }}</dd></div>
          <div><dt>历史样例</dt><dd>{{ formatSample(detail['公式核对']?.['历史样例']) }}</dd></div>
          <div><dt>V031 留档批次</dt><dd>{{ detail['V031留档']?.['留档批次'] ?? '未核对' }}</dd></div>
          <div><dt>持久化一致</dt><dd data-testid="detail-persisted">
            {{ detail['V031留档'] ? (detail['V031留档']['持久化一致'] ? '一致' : '不一致') : '未核对' }}
          </dd></div>
          <div><dt>留档历史结果</dt><dd>{{ detail['V031留档']?.['历史结果'] ?? '—' }}</dd></div>
          <div><dt>留档当前结果</dt><dd>{{ detail['V031留档']?.['当前结果'] ?? '—' }}</dd></div>
        </dl>
        <p v-if="detail?.['公式核对']?.['版本说明']?.length" class="muted-text">
          {{ detail['公式核对']['版本说明'].join('；') }}
        </p>
        <ul class="version-list" v-if="detail?.['V031留档']?.['候选版本']?.length">
          <li v-for="(version, index) in detail['V031留档']['候选版本']" :key="index">
            {{ version['版本名称'] }}：{{ version['计算公式'] }} · 精度 {{ version['精度位数'] }}
          </li>
        </ul>
        <div v-if="batchHistory.length">
          <h4 class="section-title">历史核对批次（可回查）</h4>
          <table class="data-table">
            <thead><tr><th>批次</th><th>策略</th><th>历史结果</th><th>当前结果</th><th>选中结果</th></tr></thead>
            <tbody>
              <tr v-for="item in batchHistory" :key="`${item.batch}-${item.id}`">
                <td>{{ item.batch }}</td><td>{{ item['策略'] }}</td>
                <td>{{ item['历史结果'] ?? '—' }}</td><td>{{ item['当前结果'] ?? '—' }}</td>
                <td>{{ item['选中结果'] ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="close-detail" @click="showDetail = false">关闭</button>
        </div>
      </div>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记指标定义</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条指标定义记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Payload = Record<string, any>

const ENDPOINT = '/api/metric_def'
const columns = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "基准取值", "精度位数", "口径版本"]
const actions = ["提交校验", "标记生效", "标记废弃"]
const statuses = ["草稿", "待校验", "已生效", "已废弃"]
const metricNames = ["指标定义总数", "待校验指标", "已生效指标"]
const policies = ["历史优先", "按版本时间"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const showDetail = ref(false)
const detail = ref<Payload | null>(null)
const batchHistory = ref<Payload[]>([])
const policy = ref(policies[0])
const confirming = ref(false)
const confirmation = ref<Payload | null>(null)
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])
const confirmationSummary = computed(() => {
  if (!confirmation.value) return '尚未读取 V031 留档'
  return confirmation.value.message ?? ''
})
const detailSummary = computed(() => {
  if (!detail.value) return ''
  const archive = detail.value['V031留档']
  if (!archive) return '尚未留档，请先执行核对'
  return `批次 ${archive['留档批次']} · ${archive['持久化一致'] ? '数据库持久化结果一致' : '持久化结果不一致'}`
})

function formatSample(sample: Payload | undefined) {
  if (!sample || !Object.keys(sample).length) return '—'
  return Object.entries(sample).map(([key, value]) => `${key}=${value}`).join('，')
}

function confirmationOf(row: Row): Payload | null {
  return (row as Payload)['V031核对'] ?? null
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function loadConfirmation() {
  try {
    const response = await request(`${ENDPOINT}/confirmation`)
    if (!response.ok) throw new Error('V031 留档读取失败')
    confirmation.value = await response.json()
  } catch (error) {
    confirmation.value = null
    errorMessage.value = error instanceof Error ? error.message : 'V031 留档读取失败'
  }
}

async function confirmVersions() {
  confirming.value = true
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/confirmation?policy=${encodeURIComponent(policy.value)}`, {
      method: 'POST',
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.detail || payload.message || 'V031 核对失败')
    confirmation.value = payload
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'V031 核对失败'
  } finally {
    confirming.value = false
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/detail`)
    if (!response.ok) throw new Error('公式详情读取失败')
    detail.value = await response.json()
    const historyResponse = await request(`${ENDPOINT}/confirmation/history?metric_code=${encodeURIComponent(String(row['指标编号']))}`)
    batchHistory.value = historyResponse.ok ? ((await historyResponse.json()).items ?? []) : []
    showDetail.value = true
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '公式详情读取失败'
  }
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'metric_def.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('指标定义动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标定义操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('指标定义列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标定义列表读取失败'
  }
}

onMounted(async () => {
  await Promise.all([reload(), loadConfirmation()])
})
</script>

<style scoped>
.muted-text { color: var(--muted); font-size: 12px; }
.version-list { margin: 8px 0; padding-left: 18px; font-size: 12px; color: var(--muted); }
.section-title { font-size: 13px; margin: 14px 0 6px; }
</style>
