/**
 * 个人/管理员用量页共享的日志分页 + 日趋势流程。
 *
 * 调用方只负责选择 API（个人或管理员接口）与各自的汇总模板；分页大小、
 * 筛选后回到第一页、日期范围、趋势与日志的失败降级都在此处统一处理。
 * 错误提示由 `api/request.ts` 的响应拦截器唯一负责，这里只记录日志：
 * - 日趋势失败保留上一次成功的趋势，避免图表突然清空；
 * - 日志失败清空列表并把 total 归零，避免旧页码与新筛选条件错配。
 *
 * 不在 hook 内自动 watch 筛选条件或 onMounted，初始化仍由页面显式启动，
 * 避免同一条件下出现两次请求。
 */
import { computed, ref, type ComputedRef, type Ref } from 'vue'
import type { DailyStat, TokenUsageLog, UsageLogQueryParams } from '@/api/token_usage'
import { usePagination, type PaginationState } from '@/hooks/usePagination'
import { maxDailyTokens } from '@/hooks/useUsageCharts'
import { logger } from '@/utils/logger'

export interface UsageReportSource<
  T extends TokenUsageLog,
  Q extends UsageLogQueryParams,
> {
  logs: (query: Q) => Promise<{ items: T[]; total: number }>
  daily: (days: number) => Promise<DailyStat[]>
}

export interface UsageReportReturn<T extends TokenUsageLog> {
  logs: Ref<T[]>
  dailyStats: Ref<DailyStat[]>
  dailyDays: Ref<number>
  loadingLogs: Ref<boolean>
  loadingDaily: Ref<boolean>
  dailyMax: ComputedRef<number>
  logsPagination: PaginationState
  fetchLogs: () => Promise<void>
  fetchDailyStats: () => Promise<void>
  handleFilterChange: () => void
  handleDateChange: (values: string[] | undefined) => void
  handleLogPageChange: (page: number) => void
  handleLogPageSizeChange: (size: number) => void
}

const LOG_PAGE_SIZE = 15
const LOG_PAGE_SIZE_OPTIONS = [15, 30, 50]
const DEFAULT_DAILY_DAYS = 30

export function useUsageReport<
  T extends TokenUsageLog,
  Q extends UsageLogQueryParams,
>(source: UsageReportSource<T, Q>, filters: Q): UsageReportReturn<T> {
  // Vue's UnwrapRef would rewrite the generic element type; the runtime value is a plain Ref.
  const logs = ref<T[]>([]) as Ref<T[]>
  const loadingLogs = ref(false)
  const dailyStats = ref<DailyStat[]>([])
  const loadingDaily = ref(false)
  const dailyDays = ref(DEFAULT_DAILY_DAYS)

  const {
    pagination: logsPagination,
    handlePageChange,
    handlePageSizeChange,
    reset: resetLogsPagination,
    setTotal: setLogsTotal,
  } = usePagination({
    defaultPageSize: LOG_PAGE_SIZE,
    pageSizeOptions: LOG_PAGE_SIZE_OPTIONS,
    onChange: () => fetchLogs(),
  })

  async function fetchLogs(): Promise<void> {
    loadingLogs.value = true
    try {
      // Q only widens UsageLogQueryParams, so the paginated query is exactly a Q.
      const query = {
        ...filters,
        page: logsPagination.current,
        page_size: logsPagination.pageSize,
      } as Q
      const res = await source.logs(query)
      logs.value = res.items
      setLogsTotal(res.total)
    } catch (error) {
      // 拦截器已弹唯一 Message.error，仅清空本地状态并记录日志
      logger.warn('加载使用明细失败', { filters, error })
      logs.value = []
      setLogsTotal(0)
    } finally {
      loadingLogs.value = false
    }
  }

  async function fetchDailyStats(): Promise<void> {
    loadingDaily.value = true
    try {
      dailyStats.value = await source.daily(dailyDays.value)
    } catch (error) {
      // 拦截器已弹唯一 Message.error，保留旧趋势避免清空图表
      logger.warn('加载每日趋势失败', { days: dailyDays.value, error })
    } finally {
      loadingDaily.value = false
    }
  }

  // 缓存每日 Token 最大值，避免 barHeight 在每行重复计算导致 O(n²)
  const dailyMax = computed(() =>
    maxDailyTokens(dailyStats.value.map((day) => ({ tokens: day.total_tokens }))),
  )

  const handleFilterChange = (): void => {
    resetLogsPagination()
    void fetchLogs()
  }

  const handleDateChange = (values: string[] | undefined): void => {
    if (values && values.length === 2) {
      Object.assign(filters, { start_date: values[0], end_date: values[1] })
    } else {
      Object.assign(filters, { start_date: undefined, end_date: undefined })
    }
    resetLogsPagination()
    void fetchLogs()
  }

  return {
    logs,
    dailyStats,
    dailyDays,
    loadingLogs,
    loadingDaily,
    dailyMax,
    logsPagination,
    fetchLogs,
    fetchDailyStats,
    handleFilterChange,
    handleDateChange,
    handleLogPageChange: handlePageChange,
    handleLogPageSizeChange: handlePageSizeChange,
  }
}
