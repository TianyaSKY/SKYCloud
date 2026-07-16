/**
 * 剪贴板复制工具：优先现代 Clipboard API，失败回退到 execCommand 临时 textarea 方案。
 *
 * 抽自 McpView 中 handleCopyToken / handleCopy 的复制逻辑——原实现直接调用
 * `navigator.clipboard.writeText`，未做可用性判断与异常兜底，在非 HTTPS / 旧环境下会静默失败。
 */
import {logger} from './logger'

/**
 * 将文本写入剪贴板，返回是否成功。
 *
 * 优先 `navigator.clipboard.writeText`；不可用或抛错时回退到创建临时 textarea +
 * `document.execCommand('copy')`。全程 try/catch，绝不向外抛异常。
 */
export async function copyText(text: string): Promise<boolean> {
    try {
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
            await navigator.clipboard.writeText(text)
            return true
        }
    } catch (error) {
        logger.warn('clipboard.writeText 失败，回退到 execCommand', {error})
    }

    try {
        const textarea = document.createElement('textarea')
        textarea.value = text
        // 只读 + 视觉隐藏，避免移动端键盘弹起与页面闪烁
        textarea.setAttribute('readonly', '')
        textarea.style.position = 'fixed'
        textarea.style.top = '0'
        textarea.style.left = '0'
        textarea.style.opacity = '0'
        document.body.appendChild(textarea)
        textarea.select()
        const ok = document.execCommand('copy')
        document.body.removeChild(textarea)
        if (!ok) {
            logger.warn('execCommand copy 返回 false', {text})
        }
        return ok
    } catch (error) {
        logger.warn('execCommand copy 异常', {error})
        return false
    }
}
