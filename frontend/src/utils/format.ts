/**
 * 通用格式化工具：日期与数字的展示态转换，避免各视图重复实现。
 */
import { logger } from './logger'

// 模块级共享 Intl 实例，避免每次调用重新构造。
// zh-CN 默认 12 小时制，强制 hour12: false 得到 24 小时制。
const DATE_TIME_FORMATTER = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

/**
 * 统一格式化为 `YYYY-MM-DD HH:mm`（到分钟，24 小时制）。
 *
 * 边界：空值/非法值返回 `'-'`；「永久有效」等业务文案由调用方自行处理（如 MySharesView）。
 * 用 formatToParts 重排分隔符，避免依赖 locale 默认的 `/` 分隔。
 */
export function formatDate(value: string | number | Date | null | undefined): string {
  if (value === null || value === undefined || value === '') return '-'
  const date = value instanceof Date ? value : new Date(value)
  const time = date.getTime()
  if (!Number.isFinite(time)) return '-'
  const parts = DATE_TIME_FORMATTER.formatToParts(date)
  const picked: Record<string, string> = {}
  for (const part of parts) {
    if (part.type !== 'literal') {
      picked[part.type] = part.value
    }
  }
  const year = picked['year'] ?? ''
  const month = picked['month'] ?? ''
  const day = picked['day'] ?? ''
  const hour = picked['hour'] ?? ''
  const minute = picked['minute'] ?? ''
  const result = `${year}-${month}-${day} ${hour}:${minute}`
  if (!year || !month || !day || !hour || !minute) {
    // 分量缺失通常意味着宿主 Intl 数据异常，回退兜底而非输出残缺字符串。
    logger.warn('formatDate 解析日期分量缺失，回退到 ISO 字符串', { value, result })
    return date.toISOString().replace('T', ' ').slice(0, 16)
  }
  return result
}

/**
 * 千分位 + 固定小数位格式化；null/undefined/NaN/Infinity 统一返回 `'0'`。
 * 默认 digits=2；纯整数 Token 计数可传 `digits=0`。
 */
export function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return '0'
  if (!Number.isFinite(value)) return '0'
  return value.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}
