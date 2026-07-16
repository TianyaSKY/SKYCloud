/**
 * 前端统一日志入口。生产环境静默，开发环境输出到 console。
 *
 * 项目规范（AGENTS.md）要求 Python 端统一使用 Loguru；
 * 前端暂无统一日志服务，统一收敛到此处便于后续接入埋点/上报。
 */
const isDev = import.meta.env.DEV

/**
 * 将单个参数稳健地转为字符串，用于填入 message 中的 {} 占位符。
 * - Error：拼接 message 与可选 stack，便于在控制台快速定位抛错位置
 * - 普通对象：优先 JSON.stringify（循环引用等失败时回退 String(arg)）
 * - 基本类型：null/undefined 显式化，其余走 String(arg)
 */
function stringifyArg(arg: unknown): string {
    if (arg instanceof Error) {
        return arg.stack ? `${arg.message}\n${arg.stack}` : arg.message
    }
    if (arg === null) {
        return 'null'
    }
    if (arg === undefined) {
        return 'undefined'
    }
    if (typeof arg === 'string') {
        return arg
    }
    if (typeof arg === 'object') {
        try {
            return JSON.stringify(arg)
        } catch {
            return String(arg)
        }
    }
    return String(arg)
}

/**
 * 按出现顺序将 message 中的 {} 占位符替换为对应 args 的字符串化值。
 * 被占位符消费掉的 args 不再透传；多余的 args 原样追加到 console 调用末尾，
 * 便于在控制台以可展开对象形式查看（保留原生对象信息）。
 *
 * 使用函数形式 replacer：返回值不做 $&/$$ 等特殊替换模式处理，
 * 因此含 $ 字符的字符串化结果不会被误解析。
 */
function formatWithPlaceholders(
    message: string,
    args: unknown[]
): {text: string, rest: unknown[]} {
    let consumed = 0
    const text = message.replace(/\{\}/g, () => {
        if (consumed < args.length) {
            return stringifyArg(args[consumed++])
        }
        return '{}'
    })
    return {text, rest: args.slice(consumed)}
}

function logError(message: string, ...args: unknown[]): void {
    if (isDev) {
        const {text, rest} = formatWithPlaceholders(message, args)
        console.error(`[ERROR] ${text}`, ...rest)
    }
}

function logWarn(message: string, ...args: unknown[]): void {
    if (isDev) {
        const {text, rest} = formatWithPlaceholders(message, args)
        console.warn(`[WARN] ${text}`, ...rest)
    }
}

function logInfo(message: string, ...args: unknown[]): void {
    if (isDev) {
        const {text, rest} = formatWithPlaceholders(message, args)
        console.info(`[INFO] ${text}`, ...rest)
    }
}

export const logger = {
    error: logError,
    warn: logWarn,
    info: logInfo,
}
