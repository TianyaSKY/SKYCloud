/**
 * 前端统一日志入口。生产环境静默，开发环境输出到 console。
 *
 * 项目规范（AGENTS.md）要求 Python 端统一使用 Loguru；
 * 前端暂无统一日志服务，统一收敛到此处便于后续接入埋点/上报。
 */
const isDev = import.meta.env.DEV

function logError(message: string, ...args: unknown[]): void {
    if (isDev) {
        // eslint-disable-next-line no-console
        console.error(`[ERROR] ${message}`, ...args)
    }
}

function logWarn(message: string, ...args: unknown[]): void {
    if (isDev) {
        // eslint-disable-next-line no-console
        console.warn(`[WARN] ${message}`, ...args)
    }
}

function logInfo(message: string, ...args: unknown[]): void {
    if (isDev) {
        // eslint-disable-next-line no-console
        console.info(`[INFO] ${message}`, ...args)
    }
}

export const logger = {
    error: logError,
    warn: logWarn,
    info: logInfo,
}
