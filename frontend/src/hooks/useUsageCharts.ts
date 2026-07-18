/**
 * Token 用量图表派生计算（纯函数，无 ref / 无副作用）。
 * 供 TokenUsageView / AdminTokenUsageView 复用柱高与操作类型配色。
 */

export interface DailyTokensPoint {
  tokens?: number
}

/**
 * 取每日 Token 序列最大值；空数组返回 0，由 barHeight 在 max<=0 时兜底除零。
 */
export function maxDailyTokens(daily: Array<DailyTokensPoint | undefined>): number {
  if (!daily.length) return 0
  let max = 0
  for (const point of daily) {
    const tokens = point?.tokens
    if (typeof tokens === 'number' && tokens > max) {
      max = tokens
    }
  }
  return max
}

/**
 * 按比例计算柱状图高度（像素）。
 * max<=0 返回 0；否则按比例取整，并保留至少 2px，避免极小柱体不可见。
 */
export function barHeight(tokens: number, max: number, maxHeight: number): number {
  if (!Number.isFinite(tokens) || !Number.isFinite(max)) return 0
  if (max <= 0) return 0
  const height = Math.round((tokens / max) * maxHeight)
  return Math.max(height, 2)
}

// 操作类型 → Arco 颜色 token，直接作为 a-tag 的 color 使用
const ACTION_COLOR_MAP: Record<string, string> = {
  chat: 'arcoblue',
  describe_text: 'green',
  describe_vl: 'purple',
  embedding: 'orangered',
  organize: 'cyan',
}

/**
 * 返回操作类型对应的 Arco 颜色 token（非裸 hex，便于 a-tag 渲染一致）。
 * 未知动作回退 `'gray'`。
 */
export function actionColor(action: string): string {
  return ACTION_COLOR_MAP[action] || 'gray'
}
