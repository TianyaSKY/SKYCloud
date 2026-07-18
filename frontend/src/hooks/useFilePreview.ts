import {ref} from 'vue'
import {downloadFile} from '../api/file'
import type {FileItem} from '../api/file'
import {readBlobAsText, safeRevokeObjectURL} from '../utils/blob'
import {logger} from '../utils/logger'

/** 文件预览：按扩展名分流；切换/关闭时释放 Object URL 防泄漏 */
export function useFilePreview() {
    const previewVisible = ref(false)
    const previewTitle = ref('')
    const previewUrl = ref('')
    const previewType = ref('')
    const textContent = ref('')
    const loading = ref(false)

    const handleFileClick = async (record: FileItem) => {
        previewTitle.value = record.name
        const ext = record.name.split('.').pop()?.toLowerCase() ?? ''

        try {
            loading.value = true
            // 切换预览文件前先释放旧 Object URL，避免内存泄漏
            safeRevokeObjectURL(previewUrl.value)
            previewUrl.value = ''
            textContent.value = ''

            const blob = await downloadFile(record.id)
            const url = window.URL.createObjectURL(blob)
            previewUrl.value = url

            if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp', 'ico'].includes(ext)) {
                previewType.value = 'image'
            } else if (['md', 'markdown'].includes(ext)) {
                previewType.value = 'markdown'
                textContent.value = await readBlobAsText(blob)
            } else if ([
                'js', 'ts', 'jsx', 'tsx', 'vue', 'py', 'java', 'c', 'cpp', 'h', 'hpp',
                'go', 'rs', 'rb', 'php', 'sh', 'bash', 'sql', 'r', 'swift', 'kt',
                'scala', 'lua', 'pl', 'dart', 'groovy',
                'html', 'htm', 'css', 'scss', 'less', 'sass',
                'xml', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf',
                'json', 'jsonl', 'graphql', 'proto',
                'dockerfile', 'makefile', 'cmake',
            ].includes(ext)) {
                previewType.value = 'code'
                textContent.value = await readBlobAsText(blob)
            } else if ([
                'txt', 'csv', 'tsv', 'log', 'env', 'gitignore', 'editorconfig',
                'properties', 'lock', 'pid', 'out',
            ].includes(ext)) {
                previewType.value = 'text'
                textContent.value = await readBlobAsText(blob)
            } else if (['mp4', 'webm', 'ogg'].includes(ext)) {
                previewType.value = 'video'
            } else if (['mp3', 'wav', 'aac', 'flac', 'm4a'].includes(ext)) {
                previewType.value = 'audio'
            } else if (ext === 'docx') {
                previewType.value = 'docx'
            } else if (ext === 'pdf') {
                previewType.value = 'pdf'
            } else if (['xlsx', 'xls'].includes(ext)) {
                previewType.value = 'excel'
            } else {
                previewType.value = 'unknown'
            }
            previewVisible.value = true
        } catch (error) {
            // 拦截器已弹唯一 Message.error，这里只做状态清理 + 日志
            logger.warn('handleFileClick 预览失败 id={} error={}', record.id, error)
            safeRevokeObjectURL(previewUrl.value)
            previewUrl.value = ''
        } finally {
            loading.value = false
        }
    }

    const handlePreviewClose = () => {
        safeRevokeObjectURL(previewUrl.value)
        previewUrl.value = ''
        textContent.value = ''
        previewType.value = ''
    }

    return {
        previewVisible,
        previewTitle,
        previewUrl,
        previewType,
        textContent,
        loading,
        handleFileClick,
        handlePreviewClose
    }
}
