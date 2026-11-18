<template>
  <section class="page" data-module="metric_alias">
    <header class="page-head">
      <div>
        <h2>口径注册管理</h2>
        <p class="page-desc">维护口径版本的输入、执行约束与结果记录，为口径注册提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记口径版本</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出口径注册清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <div class="analysis-form">
      <span class="muted-text" data-testid="consumer-summary">{{ consumerSummary }}</span>
      <button class="btn" type="button" data-testid="load-consumer" @click="loadConsumer">读取V031口径</button>
    </div>
    <table class="data-table" v-if="consumer.items.length" data-testid="consumer-table">
      <thead>
        <tr>
          <th>版本编号</th><th>指标编号</th><th>留档历史公式</th><th>留档精度位数</th>
          <th>旧口径结果</th><th>新口径结果</th><th>V031历史结果</th><th>与留档一致</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in consumer.items" :key="String(item['版本编号'])">
          <td>{{ item['版本编号'] }}</td><td>{{ item['指标编号'] }}</td>
          <td>{{ item['留档历史公式'] ?? '—' }}</td><td>{{ item['留档精度位数'] ?? '—' }}</td>
          <td>{{ item['旧口径结果'] }}</td><td>{{ item['新口径结果'] }}</td>
          <td>{{ item['V031历史结果'] ?? '—' }}</td><td>{{ item['与留档一致'] ? '一致' : '不一致' }}</td>
        </tr>
      </tbody>
    </table>

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
          <th>V031复算</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
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
          <td :colspan="columns.length + 2" class="empty-state">暂无口径注册数据，可先登记口径版本</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showDetail" class="modal-backdrop" data-testid="alias-detail">
      <div class="modal">
        <h3>口径详情 · {{ detail?.['版本编号'] }}</h3>
        <p class="page-desc">{{ detail?.['V031复算']?.['说明'] }}</p>
        <dl class="dag-detail" v-if="detail">
          <div><dt>旧口径</dt><dd>{{ detail['旧口径'] }}</dd></div>
          <div><dt>新口径</dt><dd>{{ detail['新口径'] }}</dd></div>
          <div><dt>旧精度原文 / 新精度原文</dt>
            <dd>{{ detail['旧精度位数'] ?? '未留档' }} / {{ detail['新精度位数'] ?? '未留档' }}</dd></div>
          <div><dt>V031 留档批次</dt><dd>{{ detail['V031复算']?.['留档批次'] ?? '未核对' }}</dd></div>
          <div><dt>留档历史公式</dt><dd>{{ detail['V031复算']?.['留档历史公式'] ?? '—' }}</dd></div>
          <div><dt>留档历史精度</dt><dd>{{ detail['V031复算']?.['留档精度位数'] ?? '—' }}</dd></div>
          <div><dt>留档当前精度</dt><dd>{{ detail['V031复算']?.['留档当前精度位数'] ?? '—' }}</dd></div>
          <div><dt>旧口径结果</dt><dd data-testid="alias-old-result">{{ detail['V031复算']?.['旧口径结果']?.['结果'] ?? '—' }}</dd></div>
          <div><dt>新口径结果</dt><dd data-testid="alias-new-result">{{ detail['V031复算']?.['新口径结果']?.['结果'] ?? '—' }}</dd></div>
          <div><dt>V031 留档历史结果</dt><dd>{{ detail['V031复算']?.['V031历史结果'] ?? '—' }}</dd></div>
          <div><dt>与留档一致</dt><dd>{{ detail['V031复算']?.['与留档一致'] ? '一致' : '不一致' }}</dd></div>
        </dl>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="close-detail" @click="showDetail = false">关闭</button>
        </div>
      </div>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记口径版本</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <label class="create-field">
          <span>旧精度位数（历史留档）</span>
          <input v-model="createValues['旧精度位数']" data-testid="create-旧精度位数" />
        </label>
        <label class="create-field">
          <span>新精度位数（历史留档）</span>
          <input v-model="createValues['新精度位数']" data-testid="create-新精度位数" />
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
      <span>共 {{ total }} 条口径注册记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Payload = Record<string, any>

const ENDPOINT = '/api/metric_alias'
const columns = ["版本编号", "指标编号", "变更说明", "旧口径", "新口径", "生效时间", "审批状态", "变更人"]
const actions = ["提交审批", "确认生效", "执行回滚"]
const statuses = ["草稿", "待审批", "已生效", "已回滚"]
const metricNames = ["口径版本总数", "待审批版本", "已生效版本"]

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
const consumer = ref<Payload>({ items: [], message: '尚未读取 V031 留档' })
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])
const consumerSummary = computed(() => consumer.value.message || '尚未读取 V031 留档')

function confirmationOf(row: Row): Payload | null {
  return (row as Payload)['V031核对'] ?? null
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function loadConsumer() {
  try {
    const response = await request(`${ENDPOINT}/consumer`)
    if (!response.ok) throw new Error('V031 口径读取失败')
    consumer.value = await response.json()
    if (consumer.value.ok === false) errorMessage.value = consumer.value.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'V031 口径读取失败'
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/detail`)
    if (!response.ok) throw new Error('口径详情读取失败')
    detail.value = await response.json()
    showDetail.value = true
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '口径详情读取失败'
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
    link.download = 'metric_alias.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  createValues.value['旧精度位数'] = ''
  createValues.value['新精度位数'] = ''
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
      throw new Error('口径注册动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '口径注册操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('口径版本列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '口径注册列表读取失败'
  }
}

onMounted(async () => {
  await reload()
})
</script>

<style scoped>
.muted-text { color: var(--muted); font-size: 12px; }
</style>
