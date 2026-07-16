<template>
  <MainLayout active-menu="token-usage" title="用量统计">
    <div class="usage-container">

      <!-- 累计统计卡片 -->
      <div class="usage-section">
        <div class="section-header">
          <icon-bar-chart class="section-icon" style="color: #165dff;" />
          <div>
            <h2 class="section-title">累计用量</h2>
            <p class="section-desc">账号自创建以来的 Token 消耗总览</p>
          </div>
        </div>

        <div v-if="!statsError" class="stats-grid">
          <div class="stat-card stat-total">
            <div class="stat-icon-wrapper" style="background: linear-gradient(135deg, #165dff 0%, #306fff 100%);">
              <icon-thunderbolt style="color: #fff;" />
            </div>
            <div class="stat-body">
              <span class="stat-label">总 Token</span>
              <span class="stat-value">{{ formatNumber(stats.total_tokens, 0) }}</span>
            </div>
          </div>
          <div class="stat-card">
            <div class="stat-icon-wrapper" style="background: linear-gradient(135deg, #00b42a 0%, #23c343 100%);">
              <icon-upload style="color: #fff;" />
            </div>
            <div class="stat-body">
              <span class="stat-label">输入 Token</span>
              <span class="stat-value">{{ formatNumber(stats.total_prompt_tokens, 0) }}</span>
            </div>
          </div>
          <div class="stat-card">
            <div class="stat-icon-wrapper" style="background: linear-gradient(135deg, #ff7d00 0%, #ff9a2e 100%);">
              <icon-download style="color: #fff;" />
            </div>
            <div class="stat-body">
              <span class="stat-label">输出 Token</span>
              <span class="stat-value">{{ formatNumber(stats.total_completion_tokens, 0) }}</span>
            </div>
          </div>
          <div class="stat-card">
            <div class="stat-icon-wrapper" style="background: linear-gradient(135deg, #722ed1 0%, #8e51da 100%);">
              <icon-clock-circle style="color: #fff;" />
            </div>
            <div class="stat-body">
              <span class="stat-label">最后活跃</span>
              <span class="stat-value stat-value-sm">{{ stats.last_active_at ? formatDate(stats.last_active_at) : '暂无' }}</span>
            </div>
          </div>
        </div>
        <a-empty v-else description="统计加载失败" style="padding: 32px 0;">
          <a-button size="small" @click="fetchStats">点此重试</a-button>
        </a-empty>
      </div>

      <!-- 每日趋势 -->
      <div class="usage-section">
        <div class="section-header">
          <icon-arrow-rise class="section-icon" style="color: #00b42a;" />
          <div>
            <h2 class="section-title">每日趋势</h2>
            <p class="section-desc">最近 {{ dailyDays }} 天的 Token 消耗趋势</p>
          </div>
          <div class="section-actions">
            <a-radio-group v-model="dailyDays" type="button" size="small" @change="fetchDailyStats">
              <a-radio :value="7">7天</a-radio>
              <a-radio :value="30">30天</a-radio>
              <a-radio :value="90">90天</a-radio>
            </a-radio-group>
          </div>
        </div>

        <div class="chart-card">
          <a-spin :loading="loadingDaily" style="width: 100%;">
            <div v-if="dailyStats.length > 0" class="chart-area">
              <!-- 简易柱状图 -->
              <div class="bar-chart">
                <div
                  v-for="day in dailyStats"
                  :key="day.date"
                  class="bar-item"
                >
                  <a-tooltip :content="`${day.date}\n总 Token: ${formatNumber(day.total_tokens, 0)}\n请求次数: ${day.request_count}`">
                    <div class="bar-wrapper">
                      <div
                        class="bar-fill bar-prompt"
                        :style="{ height: barHeight(day.prompt_tokens, dailyMax, 160) + 'px' }"
                      ></div>
                      <div
                        class="bar-fill bar-completion"
                        :style="{ height: barHeight(day.completion_tokens, dailyMax, 160) + 'px' }"
                      ></div>
                    </div>
                  </a-tooltip>
                  <span class="bar-label">{{ day.date.slice(5) }}</span>
                </div>
              </div>
              <div class="chart-legend">
                <span class="legend-item"><span class="legend-dot" style="background:#165dff;"></span>输入 Token</span>
                <span class="legend-item"><span class="legend-dot" style="background:#ff7d00;"></span>输出 Token</span>
              </div>
            </div>
            <div v-else class="chart-empty">
              <icon-empty style="font-size: 48px; color: var(--color-text-4);" />
              <p>暂无数据</p>
            </div>
          </a-spin>
        </div>
      </div>

      <!-- 使用明细 -->
      <div class="usage-section">
        <div class="section-header">
          <icon-list class="section-icon" style="color: #ff7d00;" />
          <div>
            <h2 class="section-title">使用明细</h2>
            <p class="section-desc">每次 AI 调用的 Token 消耗记录</p>
          </div>
          <div class="section-actions">
            <a-button size="small" @click="fetchLogs">
              <template #icon><icon-refresh /></template>
              刷新
            </a-button>
          </div>
        </div>

        <div class="logs-card">
          <!-- 筛选 -->
          <div class="logs-filter">
            <a-select
              v-model="logFilter.action"
              placeholder="全部类型"
              allow-clear
              style="width: 140px;"
              @change="handleFilterChange"
            >
              <a-option value="chat">chat</a-option>
              <a-option value="describe_text">describe_text</a-option>
              <a-option value="describe_vl">describe_vl</a-option>
              <a-option value="embedding">embedding</a-option>
              <a-option value="organize">organize</a-option>
            </a-select>
            <a-range-picker
              style="width: 260px;"
              @change="handleDateChange"
            />
          </div>

          <a-table
            :data="logs"
            :loading="loadingLogs"
            :pagination="logsPagination"
            row-key="id"
            @page-change="handleLogPageChange"
            @page-size-change="handleLogPageSizeChange"
          >
            <template #columns>
              <a-table-column title="时间" :width="170">
                <template #cell="{ record }">{{ formatDate(record.created_at) }}</template>
              </a-table-column>
              <a-table-column title="类型" :width="100">
                <template #cell="{ record }">
                  <a-tag :color="actionColor(record.action)" size="small">{{ record.action }}</a-tag>
                </template>
              </a-table-column>
              <a-table-column title="模型" data-index="model_name" :width="200" ellipsis />
              <a-table-column title="输入" :width="100">
                <template #cell="{ record }">
                  <span class="token-num">{{ formatNumber(record.prompt_tokens, 0) }}</span>
                </template>
              </a-table-column>
              <a-table-column title="输出" :width="100">
                <template #cell="{ record }">
                  <span class="token-num">{{ formatNumber(record.completion_tokens, 0) }}</span>
                </template>
              </a-table-column>
              <a-table-column title="总计" :width="100">
                <template #cell="{ record }">
                  <span class="token-num token-num-total">{{ formatNumber(record.total_tokens, 0) }}</span>
                </template>
              </a-table-column>
              <a-table-column title="内容摘要" ellipsis>
                <template #cell="{ record }">
                  <span class="query-summary">{{ record.query_summary || '-' }}</span>
                </template>
              </a-table-column>
            </template>
          </a-table>
        </div>
      </div>

    </div>
  </MainLayout>
</template>

<script lang="ts" setup>
import { computed, onMounted, reactive, ref } from 'vue'
import {
  IconArrowRise,
  IconBarChart,
  IconClockCircle,
  IconDownload,
  IconEmpty,
  IconList,
  IconRefresh,
  IconThunderbolt,
  IconUpload,
} from '@arco-design/web-vue/es/icon'
import MainLayout from '../components/MainLayout.vue'
import {
  getMyTokenStats,
  getMyUsageLogs,
  getMyDailyStats,
  type TokenStats,
  type TokenUsageLog,
  type DailyStat,
  type UsageLogQueryParams,
} from '@/api/token_usage'
import { usePagination } from '@/hooks/usePagination'
import { maxDailyTokens, barHeight, actionColor } from '@/hooks/useUsageCharts'
import { formatDate, formatNumber } from '@/utils/format'
import { logger } from '@/utils/logger'

// ---- 累计统计 ----
const stats = reactive<TokenStats>({
  user_id: 0,
  total_prompt_tokens: 0,
  total_completion_tokens: 0,
  total_tokens: 0,
  last_active_at: null,
})
const statsError = ref(false)

const fetchStats = async () => {
  try {
    const res = await getMyTokenStats()
    Object.assign(stats, res)
    statsError.value = false
  } catch (e) {
    // 拦截器已弹唯一 Message.error，此处仅置错误标记并降级为占位
    statsError.value = true
    logger.warn('加载累计统计失败', { error: e })
  }
}

// ---- 每日趋势 ----
const dailyDays = ref(30)
const dailyStats = ref<DailyStat[]>([])
const loadingDaily = ref(false)

const fetchDailyStats = async () => {
  loadingDaily.value = true
  try {
    const res = await getMyDailyStats(dailyDays.value)
    dailyStats.value = res
  } catch (e) {
    // 拦截器已弹唯一 Message.error，此处只记录日志，保留旧趋势避免清空
    logger.warn('加载每日趋势失败', { days: dailyDays.value, error: e })
  } finally {
    loadingDaily.value = false
  }
}

// 缓存每日 Token 最大值，避免 barHeight 在每行重复计算导致 O(n²)
const dailyMax = computed(() =>
  maxDailyTokens(dailyStats.value.map(d => ({ tokens: d.total_tokens }))),
)

// ---- 使用明细 ----
const logs = ref<TokenUsageLog[]>([])
const loadingLogs = ref(false)
const logFilter = reactive<{
  action: string | undefined
  start_date: string | undefined
  end_date: string | undefined
}>({
  action: undefined,
  start_date: undefined,
  end_date: undefined,
})
const {
  pagination: logsPagination,
  handlePageChange: handleLogPageChange,
  handlePageSizeChange: handleLogPageSizeChange,
  reset: resetLogsPagination,
  setTotal: setLogsTotal,
} = usePagination({
  defaultPageSize: 15,
  pageSizeOptions: [15, 30, 50],
  onChange: () => fetchLogs(),
})

const fetchLogs = async () => {
  loadingLogs.value = true
  try {
    const params: UsageLogQueryParams = {
      page: logsPagination.current,
      page_size: logsPagination.pageSize,
    }
    if (logFilter.action) params.action = logFilter.action
    if (logFilter.start_date) params.start_date = logFilter.start_date
    if (logFilter.end_date) params.end_date = logFilter.end_date

    const res = await getMyUsageLogs(params)
    logs.value = res.items
    setLogsTotal(res.total)
  } catch (e) {
    // 拦截器已弹唯一 Message.error，仅清理本地列表状态并记录日志
    logger.warn('加载使用明细失败', { params: logFilter, error: e })
    logs.value = []
    setLogsTotal(0)
  } finally {
    loadingLogs.value = false
  }
}

const handleFilterChange = () => {
  resetLogsPagination()
  fetchLogs()
}

const handleDateChange = (values: string[] | undefined) => {
  if (values && values.length === 2) {
    logFilter.start_date = values[0]
    logFilter.end_date = values[1]
  } else {
    logFilter.start_date = undefined
    logFilter.end_date = undefined
  }
  resetLogsPagination()
  fetchLogs()
}

onMounted(() => {
  fetchStats()
  fetchDailyStats()
  fetchLogs()
})
</script>

<style scoped>
.usage-container {
  max-width: 1100px;
  margin: 0 auto;
  padding: 0 8px 60px;
}

.usage-section {
  margin-bottom: 40px;
}

.section-header {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  margin-bottom: 20px;
}

.section-icon {
  font-size: 26px;
  line-height: 1;
  margin-top: 2px;
  flex-shrink: 0;
}

.section-title {
  font-size: 20px;
  font-weight: 600;
  color: var(--color-text-1);
  margin: 0;
}

.section-desc {
  font-size: 13px;
  color: var(--color-text-3);
  margin: 4px 0 0;
}

.section-actions {
  margin-left: auto;
  flex-shrink: 0;
}

/* ---- 统计卡片 ---- */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

.stat-card {
  background: var(--color-fill-1);
  border: 1px solid var(--color-border-1);
  border-radius: 14px;
  padding: 22px 20px;
  display: flex;
  align-items: center;
  gap: 16px;
  transition: transform 0.15s, box-shadow 0.15s;
}

.stat-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.06);
}

.stat-icon-wrapper {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  flex-shrink: 0;
}

.stat-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.stat-label {
  font-size: 12px;
  color: var(--color-text-3);
  font-weight: 500;
}

.stat-value {
  font-size: 22px;
  font-weight: 700;
  color: var(--color-text-1);
  font-variant-numeric: tabular-nums;
  line-height: 1.2;
}

.stat-value-sm {
  font-size: 14px;
  font-weight: 500;
}

/* ---- 每日趋势图 ---- */
.chart-card {
  background: var(--color-fill-1);
  border: 1px solid var(--color-border-1);
  border-radius: 14px;
  padding: 24px;
}

.chart-area {
  width: 100%;
}

.bar-chart {
  display: flex;
  align-items: flex-end;
  gap: 4px;
  height: 200px;
  padding: 0 4px;
  overflow-x: auto;
}

.bar-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  flex: 1;
  min-width: 18px;
  max-width: 40px;
}

.bar-wrapper {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 100%;
  cursor: pointer;
}

.bar-fill {
  width: 100%;
  border-radius: 3px 3px 0 0;
  min-height: 2px;
  transition: height 0.3s ease;
}

.bar-prompt {
  background: linear-gradient(180deg, #165dff 0%, #4080ff 100%);
  border-radius: 3px 3px 0 0;
}

.bar-completion {
  background: linear-gradient(180deg, #ff7d00 0%, #ff9a2e 100%);
  border-radius: 0;
}

.bar-label {
  font-size: 10px;
  color: var(--color-text-4);
  margin-top: 6px;
  white-space: nowrap;
}

.chart-legend {
  display: flex;
  justify-content: center;
  gap: 24px;
  margin-top: 16px;
  font-size: 12px;
  color: var(--color-text-3);
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 6px;
}

.legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 3px;
}

.chart-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 200px;
  gap: 12px;
  color: var(--color-text-4);
}

/* ---- 使用明细 ---- */
.logs-card {
  background: var(--color-fill-1);
  border: 1px solid var(--color-border-1);
  border-radius: 14px;
  padding: 20px;
}

.logs-filter {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}

.token-num {
  font-variant-numeric: tabular-nums;
  font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
  font-size: 13px;
  color: var(--color-text-2);
}

.token-num-total {
  color: var(--color-text-1);
  font-weight: 600;
}

.query-summary {
  font-size: 13px;
  color: var(--color-text-3);
}

@media (max-width: 900px) {
  .stats-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 600px) {
  .stats-grid {
    grid-template-columns: 1fr;
  }

  .logs-filter {
    flex-direction: column;
  }
}
</style>
