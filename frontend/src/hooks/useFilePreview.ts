import {ref} from 'vue'
import {Message} from '@arco-design/web-vue'
import {downloadFile} from '../api/file'

export function useFilePreview() {
    const previewVisible = ref(false)
    const previewTitle = ref('')
    const previewUrl = ref('')
    const previewType = ref('')
    const textContent = ref('')
    const loading = ref(false)

    const handleFileClick = async (record: any) => {
        previewTitle.value = record.name
        const ext = record.name.split('.').pop().toLowerCase()

        try {
            loading.value = true
            const blob = await downloadFile(record.id)
            const url = window.URL.createObjectURL(new Blob([blob as any]))
            previewUrl.value = url

            if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp', 'ico'].includes(ext)) {
                previewType.value = 'image'
            } else if (['md', 'markdown'].includes(ext)) {
                previewType.value = 'markdown'
                const reader = new FileReader()
                reader.onload = (e) => {
                    textContent.value = e.target?.result as string
                }
                reader.readAsText(blob as any)
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
                const reader = new FileReader()
                reader.onload = (e) => {
                    textContent.value = e.target?.result as string
                }
                reader.readAsText(blob as any)
            } else if ([
                'txt', 'csv', 'tsv', 'log', 'env', 'gitignore', 'editorconfig',
                'properties', 'lock', 'pid', 'out',
            ].includes(ext)) {
                previewType.value = 'text'
                const reader = new FileReader()
                reader.onload = (e) => {
                    textContent.value = e.target?.result as string
                }
                reader.readAsText(blob as any)
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
            Message.error('预览失败')
        } finally {
            loading.value = false
        }
    }

    const handlePreviewClose = () => {
        if (previewUrl.value) {
            window.URL.revokeObjectURL(previewUrl.value)
            previewUrl.value = ''
        }
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
