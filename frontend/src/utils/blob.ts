/**
 * Blob 与 Object URL 工具：统一文本读取，并提供安全的 revokeObjectURL 包装。
 */
import {logger} from './logger'

/**
 * 以文本方式读取 Blob；补齐 onerror/onabort，失败时 reject 而非静默吞掉。
 * encoding 可选，缺省由浏览器自动判定。
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
 * 安全释放 Object URL：空值与异常仅记 warn，不抛出，避免污染上层逻辑。
 */
export function safeRevokeObjectURL(url: string | null | undefined): void {
    if (!url) return
    try {
        URL.revokeObjectURL(url)
    } catch (error) {
        logger.warn('safeRevokeObjectURL 释放失败', {url, error})
    }
}
