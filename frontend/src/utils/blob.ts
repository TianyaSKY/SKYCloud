/**
 * Blob 与 Object URL 相关工具：合并 useFilePreview 中共三处重复的 readAsText 逻辑，
 * 并提供安全的 revokeObjectURL 包装。
 */
import {logger} from './logger'

/**
 * 以文本方式读取 Blob 内容，返回 Promise。
 *
 * 相对原 useFilePreview 中三处重复的内联 FileReader 写法：
 *  - 补齐 `onerror` 分支，读取失败时 reject（原实现仅设 onload，错误被静默吞掉）；
 *  - 通过 encoding 可选参数显式指定编码（缺省交由浏览器自动判定，与原 readAsText(blob) 一致）。
 */
export function readBlobAsText(blob: Blob, encoding?: string): Promise<string> {
    return new Promise<string>((resolve, reject) => {
        const reader = new FileReader()
        reader.onload = (event) => {
            const result = event.target?.result
            resolve(typeof result === 'string' ? result : '')
        }
        reader.onerror = () => {
            const error = reader.error
            logger.warn('readBlobAsText 读取失败', {error})
            reject(error ?? new Error('readBlobAsText 读取失败'))
        }
        reader.onabort = () => {
            reject(new Error('readBlobAsText 读取被中断'))
        }
        if (encoding) {
            reader.readAsText(blob, encoding)
        } else {
            reader.readAsText(blob)
        }
    })
}

/**
 * 释放 Object URL，无效入参与异常均安全吞掉（仅记 warn 日志，不抛出）。
 *
 * 用于替换 useFilePreview 等处 `window.URL.revokeObjectURL(url)` 调用，
 * 避免 url 为空或已释放时抛出污染上层逻辑。
 */
export function safeRevokeObjectURL(url: string | null | undefined): void {
    if (!url) return
    try {
        URL.revokeObjectURL(url)
    } catch (error) {
        logger.warn('safeRevokeObjectURL 释放失败', {url, error})
    }
}
