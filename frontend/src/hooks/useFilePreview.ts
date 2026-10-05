import { onScopeDispose, ref } from 'vue'
import type { FileItem } from '../api/file'
import { downloadFileBlob } from '../api/file'
import { readBlobAsText, safeRevokeObjectURL } from '../utils/blob'
import { logger } from '../utils/logger'

/** 文件预览：按扩展名分流；切换/关闭时释放 Object URL 防泄漏 */
export function useFilePreview() {
  const previewVisible = ref(false)
  const previewTitle = ref('')
  const previewUrl = ref('')
  const previewType = ref('')
  const textContent = ref('')
  const loading = ref(false)
  let generation = 0
  let pending: AbortController | undefined

  const handlePreviewClose = () => {
    generation += 1
    pending?.abort()
    pending = undefined
    safeRevokeObjectURL(previewUrl.value)
    previewVisible.value = false
    previewUrl.value = ''
    textContent.value = ''
    previewType.value = ''
    loading.value = false
  }
  onScopeDispose(handlePreviewClose)

  const handleFileClick = async (record: FileItem) => {
    handlePreviewClose()
    const current = generation
    const readText = async (blob: Blob) => {
      const text = await readBlobAsText(blob)
      if (current === generation) textContent.value = text
    }
    const controller = new AbortController()
    pending = controller
    previewTitle.value = record.name
    const ext = record.name.split('.').pop()?.toLowerCase() ?? ''

    try {
      loading.value = true
      // 切换预览文件前先释放旧 Object URL，避免内存泄漏
      safeRevokeObjectURL(previewUrl.value)
      previewUrl.value = ''
      textContent.value = ''

      const blob = await downloadFileBlob(record.id, controller.signal)
      if (current !== generation) return
      const url = window.URL.createObjectURL(blob)
      previewUrl.value = url

      if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp', 'ico'].includes(ext)) {
        previewType.value = 'image'
      } else if (['md', 'markdown'].includes(ext)) {
        previewType.value = 'markdown'
        await readText(blob)
      } else if (
        [
          'js',
          'ts',
          'jsx',
          'tsx',
          'vue',
          'py',
          'java',
          'c',
          'cpp',
          'h',
          'hpp',
          'go',
          'rs',
          'rb',
          'php',
          'sh',
          'bash',
          'sql',
          'r',
          'swift',
          'kt',
          'scala',
          'lua',
          'pl',
          'dart',
          'groovy',
          'html',
          'htm',
          'css',
          'scss',
          'less',
          'sass',
          'xml',
          'yaml',
          'yml',
          'toml',
          'ini',
          'cfg',
          'conf',
          'json',
          'jsonl',
          'graphql',
          'proto',
          'dockerfile',
          'makefile',
          'cmake',
        ].includes(ext)
      ) {
        previewType.value = 'code'
        await readText(blob)
      } else if (
        ['txt', 'csv', 'tsv', 'log', 'env', 'gitignore', 'editorconfig', 'properties', 'lock', 'pid', 'out'].includes(
          ext,
        )
      ) {
        previewType.value = 'text'
        await readText(blob)
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
      if (current !== generation) return
      previewVisible.value = true
    } catch (error) {
      if (current !== generation) return
      // 拦截器已弹唯一 Message.error，这里只做状态清理 + 日志
      logger.warn('handleFileClick 预览失败 id={} error={}', record.id, error)
      safeRevokeObjectURL(previewUrl.value)
      previewUrl.value = ''
    } finally {
      if (current === generation) {
        pending = undefined
        loading.value = false
      }
    }
  }

  return {
    previewVisible,
    previewTitle,
    previewUrl,
    previewType,
    textContent,
    loading,
    handleFileClick,
    handlePreviewClose,
  }
}
