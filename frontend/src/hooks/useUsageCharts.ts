/**
 * Token 用量图表派生计算：抽自 TokenUsageView / AdminTokenUsageView 中重复的
 * maxDailyTokens / barHeight / actionColor 纯函数，供 Wave 2 替换。
 *
 * 这些均为纯函数（无 ref / 无副作用），故放在 hooks 目录下但不作为 composable，
 * 调用方直接按函数使用即可。
 */

export interface DailyTokensPoint {
    tokens?: number
}

/**
 * 取每日 Token 序列中的最大值，空数组返回 0。
 *
 * 注意：原实现为避免除零把下限设为 1；这里按任务要求空数组返回 0，
 * 由 barHeight 在 max<=0 时返回 0 来兜底除零风险。
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
 *
 * - max <= 0 时返回 0（无可用基准）；
 * - 否则按 tokens/max * maxHeight 取整，并保留 2px 最小可见高度，贴合原实现视觉（避免极小柱体消失）。
 */
export function barHeight(tokens: number, max: number, maxHeight: number): number {
    if (!Number.isFinite(tokens) || !Number.isFinite(max)) return 0
    if (max <= 0) return 0
    const height = Math.round((tokens / max) * maxHeight)
    return Math.max(height, 2)
}

// 操作类型 -> Arco 颜色 token 表，与 TokenUsageView / AdminTokenUsageView 原实现完全一致。
const ACTION_COLOR_MAP: Record<string, string> = {
    chat: 'arcoblue',
    describe_text: 'green',
    describe_vl: 'purple',
    embedding: 'orangered',
    organize: 'cyan',
}

/**
 * 返回操作类型对应的颜色 token（同时可用作 a-tag 的 color）。
 *
 * 裁决说明：原实现返回的是 Arco 颜色 token（arcoblue/green/...，非裸 CSS hex），
 * 任务文字描述为「返回 CSS 颜色」。为「精确匹配现有行为」且保持 a-tag 渲染一致，
 * 这里继续返回 Arco 颜色 token；若 Wave 2 在 :style 场景需要裸 hex，可在调用方再做一层映射。
 * 未知动作回退 `'gray'`，与原实现一致。
 */
export function actionColor(action: string): string {
    return ACTION_COLOR_MAP[action] || 'gray'
}
