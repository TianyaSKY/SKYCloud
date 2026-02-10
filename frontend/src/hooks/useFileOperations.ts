import {reactive, ref} from 'vue'
import {Message, Notification} from '@arco-design/web-vue'
import {
    batchDeleteFiles,
    createFolder,
    deleteFile,
    deleteFolder,
    downloadFile,
    getAllFolders,
    getRootFolderId,
    organizeFiles,
    rebuildFailedIndexes,
    retryEmbedding,
    updateFile,
    updateFolder
} from '../api/file'
import {createShare} from '../api/share'
import {uploadFileOptimized} from '../utils/upload'

export function useFileOperations(
    currentParentId: any,
    fetchFiles: () => Promise<void>,
    selectedKeys: any,
    fileList?: any
) {
    const folderForm = reactive({name: ''})
    const showCreateFolder = ref(false)

    const showRenameModal = ref(false)
    const renameForm = reactive({name: ''})
    const renamingItem = ref<any>(null)

    const showMoveModal = ref(false)
    const movingItem = ref<any>(null)
    const folderTree = ref<any[]>([])
    const targetFolderId = ref<number | null>(null)

    const showShareModal = ref(false)
    const showShareResult = ref(false)
    const selectedFile = ref<any>(null)
    const shareUrl = ref('')
    const shareForm = reactive({expires_at: ''})

    const handleUpload = (options: any) => {
        const {fileItem, onSuccess, onError, onProgress} = options
        const rawFile = fileItem?.file as File
        if (!rawFile) {
            onError?.(new Error('Invalid upload file'))
            return
        }

        const notificationId = `upload-${fileItem.uid || Date.now()}`
        Notification.info({
            id: notificationId,
            title: '上传中',
            content: `正在准备上传 ${fileItem.name}...`,
            duration: 0,
            closable: false
        })

        uploadFileOptimized(rawFile, currentParentId.value, (percent) => {
            if (typeof onProgress === 'function') {
                onProgress(percent)
            }
            Notification.info({
                id: notificationId,
                title: '上传中',
                content: `正在上传 ${fileItem.name}: ${Math.round(percent)}%`,
                duration: 0,
                closable: false
            })
        })
            .then(() => {
                Notification.success({
                    id: notificationId,
                    title: '上传完成',
                    content: `${fileItem.name} 上传成功`,
                    duration: 3000,
                    closable: true
                })
                fetchFiles()
                onSuccess?.()
            })
            .catch((error) => {
                Notification.error({
                    id: notificationId,
                    title: '上传失败',
                    content: `${fileItem.name} 上传失败: ${error.message || '未知错误'}`,
                    duration: 4000,
                    closable: true
                })
                onError?.(error)
            })
    }

    const handleCreateFolder = async () => {
        if (!folderForm.name) return
        try {
            await createFolder({
                name: folderForm.name,
                parent_id: currentParentId.value || undefined
            })
            Message.success('创建成功')
            showCreateFolder.value = false
            folderForm.name = ''
            await fetchFiles()
        } catch {
            // handled by interceptor
        }
    }

    const handleDelete = async (record: any) => {
        try {
            if (record.is_folder) {
                await deleteFolder(record.id)
            } else {
                await deleteFile(record.id)
            }
            Message.success('删除成功')
            await fetchFiles()
        } catch {
            // handled by interceptor
        }
    }

    const handleBatchDelete = async (ids: number[]) => {
        try {
            const itemsToDelete = ids.map(id => {
                const item = fileList?.value?.find((f: any) => f.id === id)
                return {
                    id,
                    is_folder: item ? !!item.is_folder : false
                }
            })

            await batchDeleteFiles(itemsToDelete)
            Message.success('批量删除成功')
            selectedKeys.value = []
            await fetchFiles()
        } catch {
            Message.error('批量删除失败')
        }
    }

    const handleDownload = async (record: any) => {
        try {
            const blob = await downloadFile(record.id)
            const url = window.URL.createObjectURL(new Blob([blob as any]))
            const link = document.createElement('a')
            link.href = url
            link.setAttribute('download', record.name)
            document.body.appendChild(link)
            link.click()
            link.remove()
        } catch {
            // handled by interceptor
        }
    }

    const handleShare = (record: any) => {
        selectedFile.value = record
        showShareModal.value = true
    }

    const confirmShare = async () => {
        if (!selectedFile.value) return
        try {
            const res: any = await createShare({
                file_id: selectedFile.value.id,
                expires_at: shareForm.expires_at || undefined
            })
            const token = res.token || res.data?.token
            shareUrl.value = `${window.location.origin}/s/${token}`
            showShareModal.value = false
            showShareResult.value = true
        } catch {
            Message.error('创建分享失败')
        }
    }

    const handleRetryEmbedding = async (record: any) => {
        try {
            await retryEmbedding(record.id)
            Message.success('已提交重试请求')
            await fetchFiles()
        } catch {
            Message.error('重试失败')
        }
    }

    const handleRebuildIndexes = async () => {
        try {
            const res: any = await rebuildFailedIndexes()
            const count = res.count || res.data?.count || 0
            Message.success(`已触发批量重建，共 ${count} 个文件`)
            await fetchFiles()
        } catch {
            Message.error('批量重建失败')
        }
    }

    const handleRename = (record: any) => {
        renamingItem.value = record
        renameForm.name = record.name
        showRenameModal.value = true
    }

    const confirmRename = async () => {
        if (!renamingItem.value || !renameForm.name) return
        try {
            if (renamingItem.value.is_folder) {
                await updateFolder(renamingItem.value.id, {name: renameForm.name})
            } else {
                await updateFile(renamingItem.value.id, {name: renameForm.name})
            }
            Message.success('重命名成功')
            showRenameModal.value = false
            await fetchFiles()
        } catch {
            Message.error('重命名失败')
        }
    }

    const handleMove = async (record: any) => {
        movingItem.value = record
        try {
            const res: any = await getAllFolders()
            const folders = res.folders || []

            const buildTree = (parentId: number | null) => {
                return folders
                    .filter((f: any) => f.parent_id === parentId)
                    .map((f: any) => ({
                        id: f.id,
                        name: f.name,
                        children: buildTree(f.id)
                    }))
            }

            const rootRes: any = await getRootFolderId()
            const rootId = rootRes.root_folder_id || rootRes

            folderTree.value = [
                {
                    id: rootId,
                    name: '根目录',
                    children: buildTree(rootId)
                }
            ]
            showMoveModal.value = true
        } catch {
            Message.error('获取文件夹列表失败')
        }
    }

    const handleFolderSelect = (folderId: number) => {
        targetFolderId.value = folderId
    }

    const confirmMove = async () => {
        if (!movingItem.value || targetFolderId.value === undefined) return
        try {
            if (movingItem.value.is_folder) {
                await updateFolder(movingItem.value.id, {
                    parent_id: targetFolderId.value
                })
            } else {
                await updateFile(movingItem.value.id, {
                    parent_id: targetFolderId.value
                })
            }
            Message.success('移动成功')
            showMoveModal.value = false
            await fetchFiles()
        } catch {
            Message.error('移动失败')
        }
    }

    const handleOrganize = async () => {
        try {
            const res: any = await organizeFiles()
            const message = res.message || (Array.isArray(res) ? res[0] : '已开始智能整理')
            Message.success(message)
        } catch {
            Message.error('触发智能整理失败')
        }
    }

    return {
        folderForm,
        showCreateFolder,
        showRenameModal,
        renameForm,
        renamingItem,
        showMoveModal,
        movingItem,
        folderTree,
        targetFolderId,
        showShareModal,
        showShareResult,
        selectedFile,
        shareUrl,
        shareForm,
        handleUpload,
        handleCreateFolder,
        handleDelete,
        handleBatchDelete,
        handleDownload,
        handleShare,
        confirmShare,
        handleRetryEmbedding,
        handleRebuildIndexes,
        handleRename,
        confirmRename,
        handleMove,
        handleFolderSelect,
        confirmMove,
        handleOrganize
    }
}
