import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { useFileOperations } from '../useFileOperations'
import type { FileItem } from '@/api/file'

const fileApi = vi.hoisted(() => ({
  batchDeleteFiles: vi.fn(),
  createFolder: vi.fn(),
  deleteFile: vi.fn(),
  deleteFolder: vi.fn(),
  getAllFolders: vi.fn(),
  getRootFolderId: vi.fn(),
  organizeFiles: vi.fn(),
  rebuildFailedIndexes: vi.fn(),
  retryEmbedding: vi.fn(),
  updateFile: vi.fn(),
  updateFolder: vi.fn(),
}))
const shareApi = vi.hoisted(() => ({ createShare: vi.fn() }))
const download = vi.hoisted(() => ({ downloadAttachment: vi.fn() }))
const messages = vi.hoisted(() => ({ success: vi.fn(), warning: vi.fn(), error: vi.fn() }))
const logger = vi.hoisted(() => ({ warn: vi.fn(), error: vi.fn() }))

vi.mock('@/api/file', () => fileApi)
vi.mock('@/api/share', () => shareApi)
vi.mock('@/utils/fileDownload', () => download)
vi.mock('@/utils/logger', () => ({ logger }))
vi.mock('@arco-design/web-vue', () => ({ Message: messages }))

const file = { id: 11, name: 'report.txt', is_folder: false } as FileItem
const folder = { id: 12, name: 'docs', is_folder: true } as FileItem

function setup() {
  const currentParentId = ref<number | null>(5)
  const selectedKeys = ref<number[]>([11, 12])
  const fileList = ref<FileItem[]>([file, folder])
  const fetchFiles = vi.fn().mockResolvedValue(undefined)
  const startUpload = vi.fn().mockResolvedValue(undefined)
  const operations = useFileOperations(currentParentId, fetchFiles, selectedKeys, startUpload, fileList)
  return { operations, currentParentId, selectedKeys, fileList, fetchFiles, startUpload }
}

beforeEach(() => {
  vi.clearAllMocks()
  fileApi.createFolder.mockResolvedValue({})
  fileApi.deleteFile.mockResolvedValue(undefined)
  fileApi.deleteFolder.mockResolvedValue(undefined)
  fileApi.batchDeleteFiles.mockResolvedValue(undefined)
  fileApi.updateFile.mockResolvedValue({})
  fileApi.updateFolder.mockResolvedValue({})
  fileApi.getAllFolders.mockResolvedValue({ folders: [{ id: 12, name: 'docs', parent_id: 1 }] })
  fileApi.getRootFolderId.mockResolvedValue({ root_folder_id: 1 })
  fileApi.retryEmbedding.mockResolvedValue(undefined)
  fileApi.rebuildFailedIndexes.mockResolvedValue({ count: 3 })
  fileApi.organizeFiles.mockResolvedValue({ message: undefined, queued: true })
  shareApi.createShare.mockResolvedValue({ token: 'tok-1' })
  download.downloadAttachment.mockResolvedValue(undefined)
})

describe('file operations', () => {
  it('creates a folder in the current parent and refreshes', async () => {
    const { operations, fetchFiles } = setup()
    operations.folderForm.name = 'notes'

    await operations.handleCreateFolder()

    expect(fileApi.createFolder).toHaveBeenCalledWith({ name: 'notes', parent_id: 5 })
    expect(messages.success).toHaveBeenCalled()
    expect(operations.showCreateFolder.value).toBe(false)
    expect(operations.folderForm.name).toBe('')
    expect(fetchFiles).toHaveBeenCalledTimes(1)
  })

  it('ignores an empty folder name', async () => {
    const { operations, fetchFiles } = setup()
    operations.folderForm.name = ''

    await operations.handleCreateFolder()

    expect(fileApi.createFolder).not.toHaveBeenCalled()
    expect(fetchFiles).not.toHaveBeenCalled()
  })

  it('deletes files and folders through the matching endpoint', async () => {
    const { operations, fetchFiles } = setup()

    await operations.handleDelete(file)
    await operations.handleDelete(folder)

    expect(fileApi.deleteFile).toHaveBeenCalledWith(11)
    expect(fileApi.deleteFolder).toHaveBeenCalledWith(12)
    expect(fetchFiles).toHaveBeenCalledTimes(2)
  })

  it('reports every batch item with its folder flag, clears the selection and refreshes', async () => {
    const { operations, selectedKeys, fetchFiles } = setup()

    await operations.handleBatchDelete([11, 12])

    expect(fileApi.batchDeleteFiles).toHaveBeenCalledWith([
      { id: 11, is_folder: false },
      { id: 12, is_folder: true },
    ])
    expect(selectedKeys.value).toEqual([])
    expect(fetchFiles).toHaveBeenCalledTimes(1)
  })

  it('keeps the selection and skips the refresh when a batch delete fails', async () => {
    const { operations, selectedKeys, fetchFiles } = setup()
    fileApi.batchDeleteFiles.mockRejectedValueOnce(new Error('locked'))

    await operations.handleBatchDelete([11])

    expect(selectedKeys.value).toEqual([11, 12])
    expect(fetchFiles).not.toHaveBeenCalled()
    expect(logger.warn).toHaveBeenCalled()
  })

  it('renames a file and a folder, keeping the dialog open on failure', async () => {
    const { operations, fetchFiles } = setup()
    operations.handleRename(file)
    operations.renameForm.name = 'report-v2.txt'
    await operations.confirmRename()
    expect(fileApi.updateFile).toHaveBeenCalledWith(11, { name: 'report-v2.txt' })
    expect(operations.showRenameModal.value).toBe(false)
    expect(fetchFiles).toHaveBeenCalledTimes(1)

    fileApi.updateFolder.mockRejectedValueOnce(new Error('duplicate'))
    operations.handleRename(folder)
    operations.renameForm.name = 'docs-v2'
    await operations.confirmRename()

    expect(fileApi.updateFolder).toHaveBeenCalledWith(12, { name: 'docs-v2' })
    expect(operations.showRenameModal.value).toBe(true)
    expect(fetchFiles).toHaveBeenCalledTimes(1)
  })

  it('builds the folder tree from the workspace root before moving', async () => {
    const { operations } = setup()

    await operations.handleMove(file)

    expect(operations.showMoveModal.value).toBe(true)
    expect(operations.folderTree.value).toEqual([
      { id: 1, name: '根目录', children: [{ id: 12, name: 'docs', children: [] }] },
    ])
  })

  it('moves the selected item into the chosen folder and refreshes', async () => {
    const { operations, fetchFiles } = setup()
    await operations.handleMove(file)
    operations.handleFolderSelect(12)

    await operations.confirmMove()

    expect(fileApi.updateFile).toHaveBeenCalledWith(11, { parent_id: 12 })
    expect(operations.showMoveModal.value).toBe(false)
    expect(fetchFiles).toHaveBeenCalledTimes(1)
  })

  it('exposes the public share link only after the share is created', async () => {
    const { operations } = setup()
    operations.handleShare(file)
    operations.shareForm.expires_at = '2026-12-31T00:00:00Z'

    await operations.confirmShare()

    expect(shareApi.createShare).toHaveBeenCalledWith({ file_id: 11, expires_at: '2026-12-31T00:00:00Z' })
    expect(operations.shareUrl.value).toBe(`${window.location.origin}/s/tok-1`)
    expect(operations.showShareResult.value).toBe(true)
    expect(operations.showShareModal.value).toBe(false)

    const second = setup()
    shareApi.createShare.mockRejectedValueOnce(new Error('quota'))
    second.operations.handleShare(file)
    await second.operations.confirmShare()

    expect(second.operations.shareUrl.value).toBe('')
    expect(second.operations.showShareResult.value).toBe(false)
    expect(logger.warn).toHaveBeenCalled()
  })

  it('downloads with the row name and keeps the row on failure', async () => {
    const { operations } = setup()

    await operations.handleDownload(file)
    expect(download.downloadAttachment).toHaveBeenCalledWith(11, 'report.txt')

    download.downloadAttachment.mockRejectedValueOnce(new Error('expired'))
    await operations.handleDownload(file)

    expect(logger.warn).toHaveBeenCalled()
  })

  it('reports the rebuild count and refreshes the list', async () => {
    const { operations, fetchFiles } = setup()

    await operations.handleRebuildIndexes()
    expect(fileApi.rebuildFailedIndexes).toHaveBeenCalledTimes(1)
    expect(messages.success).toHaveBeenCalledWith(expect.stringContaining('3'))
    expect(fetchFiles).toHaveBeenCalledTimes(1)

    await operations.handleRetryEmbedding(file)
    expect(fileApi.retryEmbedding).toHaveBeenCalledWith(11)
    expect(fetchFiles).toHaveBeenCalledTimes(2)
  })

  it('warns when the organizer cannot queue the task', async () => {
    const { operations } = setup()
    fileApi.organizeFiles.mockResolvedValueOnce({ message: '已有任务在进行', queued: false })

    await operations.handleOrganize()

    expect(messages.warning).toHaveBeenCalledWith('已有任务在进行')
    expect(messages.success).not.toHaveBeenCalled()
  })
})
