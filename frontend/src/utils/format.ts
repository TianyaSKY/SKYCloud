/**
 * 通用格式化工具：日期与数字的展示态转换。
 *
 * 抽自 FileTable / MySharesView / InboxView / McpView / TokenUsageView /
 * AdminTokenUsageView / WorkspaceView 中各处重复的 formatDate / formatNumber 实现，
 * 供 Wave 2 统一替换调用。
 */
import {logger} from './logger'

// 模块级共享 Intl 实例，避免每次调用都重新构造格式化器。
// 说明：zh-CN 默认 hour 周期为 12 小时制（带上午/下午），这里强制 hour12: false 以获得 24 小时制。
const DATE_TIME_FORMATTER = new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
})

/**
 * 将任意常见日期入参统一格式化为 `YYYY-MM-DD HH:mm`（到分钟）。
 *
 * 裁决说明：
 *  - 现有实现差异：FileTable/McpView/TokenUsage/AdminTokenUsage 对空值返回 `'-'`；
 *    InboxView 对空值与非法值返回 `''`；MySharesView 对空值返回业务文案 `'永久有效'`；
 *    WorkspaceView(原 formatTime) 对空值返回 `'-'` 并已使用 zh-CN 显式选项（但未含年份、到分钟）。
 *  - 此处统一空值/非法值返回 `'-'`（多数派且语义清晰）；MySharesView 的 `'永久有效'` 属业务特例，
 *    由 Wave 2 在调用方自行保留，不走本工具。
 *  - 精度统一为「到分钟」：秒级信息在表格/卡片场景冗余，WorkspaceView 原本即到分钟；
 *    其余原默认 toLocaleString() 含秒，统一去掉秒。
 *  - 出参严格为 `2026-07-16 09:30` 形式（连字符 + 空格 + 冒号），
 *    通过 formatToParts 重排 zh-CN 分隔符，避免依赖 locale 默认分隔符（`/`）。
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
        logger.warn('formatDate 解析日期分量缺失，回退到 ISO 字符串', {value, result})
        return date.toISOString().replace('T', ' ').slice(0, 16)
    }
    return result
}

/**
 * 将数字格式化为带千分位 + 固定小数位的字符串。
 *
 * 裁决说明：
 *  - 现有 TokenUsageView / AdminTokenUsageView 的 formatNumber 使用 `n.toLocaleString()`
 *    （默认带千分位、不固定小数位），且 null 返回 `'0'`。
 *  - 这里按任务统一为「千分位 + 固定小数位」，默认 digits=2：
 *      `1234567 -> "1,234,567.00"`、`1234.5 -> "1,234.50"`、`0 -> "0.00"`。
 *  - null/undefined/NaN/Infinity 统一返回 `'0'`（与现有 null 返回 `'0'` 一致，扩展到非法数值更稳健）。
 *  - Wave 2 若用于纯整数 Token 计数且希望不带小数，可传 `digits=0`。
 */
export function formatNumber(value: number | null | undefined, digits = 2): string {
    if (value === null || value === undefined) return '0'
    if (!Number.isFinite(value)) return '0'
    return value.toLocaleString('zh-CN', {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
    })
}
