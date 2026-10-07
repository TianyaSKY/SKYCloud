import type { AxiosProgressEvent, AxiosRequestConfig } from 'axios'
import request from './request'

/** 后端契约：文件/文件夹实体；部分字段在不同接口中可能缺失，故按可选处理 */
export interface FileItem {
  id: number
  name: string
  size?: number
  file_size?: number
  type?: string
  updated_at?: string
  created_at?: string
  is_folder: boolean
  parent_id?: number | null
}

/** Raw API metadata is separate from rows decorated for the file browser. */
export interface FileMetadata {
  id: number
  name: string
  status: string | null
  description: string | null
  file_size: number | null
  mime_type: string | null
  content_hash: string | null
  workspace_id: number
  uploader_id: number | null
  parent_id: number | null
  created_at: string | null
}

export interface FolderMetadata {
  id: number
  name: string
  workspace_id: number
  parent_id: number | null
  path: string
  created_at: string | null
}

/** 列表查询参数；parent_id 为空表示根目录 */
export interface ListFilesParams {
  parent_id?: number | null
  page?: number
  page_size?: number
  name?: string
  sort_by?: string
  order?: 'asc' | 'desc'
}

/** 搜索参数；type 为 fuzzy 模糊或 vector 语义 */
export interface SearchFilesParams {
  q: string
  page?: number
  page_size?: number
  type?: 'fuzzy' | 'vector'
}

/** 分片上传初始化请求；content_hash 用于秒传判定 */
export interface MultipartInitRequest {
  filename: string
  total_size: number
  chunk_size?: number
  parent_id?: number | null
  mime_type?: string
  content_hash?: string
  upload_id?: string
}

/** 分片上传初始化响应；instant_upload=true 时已秒传完成，无需再传分片 */
export interface MultipartInitResponse {
  upload_id: string | null
  chunk_size: number
  total_chunks: number
  uploaded_chunks: number[]
  instant_upload?: boolean
  file?: FileMetadata
}

export interface FilePreflightRequest {
  filename: string
  total_size: number
  parent_id?: number | null
  mime_type?: string
  content_hash: string
}

export interface FilePreflightResponse {
  instant_upload: boolean
  exists: boolean
  file?: FileMetadata
}

/** 后端契约：/files/search 返回的标准分页结构 */
export interface FilePageResult {
  items: FileMetadata[]
  total: number
  page: number
  page_size: number
}

/** Canonical directory listing: paginated files and immediate subfolders. */
export interface FileListResponse {
  files: FilePageResult & { pages: number }
  folders: FolderMetadata[]
}

export interface FolderListResult {
  folders: FolderMetadata[]
}

export interface RootFolderIdResult {
  root_folder_id: number
}

export interface BatchDeleteItem {
  id: number
  is_folder: boolean
}

export interface CreateFolderParams {
  name: string
  parent_id?: number
}

export interface UpdateFileItemParams {
  name?: string
  parent_id?: number | null
}

/** 后端契约：/files/rebuild_failed_indexes 返回，count 为待重建文件数 */
export interface RebuildIndexesResult {
  count: number
}

/** 后端契约：/folder/organize 返回；queued=false 表示已有任务在进行中 */
export interface OrganizeFilesResult {
  message: string
  queued: boolean
}

/** 后端契约：/files/process_status 返回各状态文件计数，键为中文状态名 */
export interface ProcessStatusResult {
  处理中: number
  失败: number
  成功: number
}

export const getFiles = (params?: ListFilesParams) => {
  return request.get<FileListResponse>('/files/list', { params })
}

export const searchFiles = (params: SearchFilesParams) => {
  return request.get<FilePageResult>('/files/search', { params: { ...params, type: params.type === 'vector' ? 'semantic' : params.type } })
}

export const uploadFile = (data: FormData, config?: AxiosRequestConfig) => {
  return request.post<FileMetadata>('/files', data, {
    timeout: 0,
    ...config,
    headers: {
      'Content-Type': 'multipart/form-data',
      ...(config?.headers || {}),
    },
  })
}

export const batchUploadFiles = (data: FormData, config?: AxiosRequestConfig) => {
  return request.post<FileMetadata[]>('/files/batch', data, {
    timeout: 0,
    ...config,
    headers: {
      'Content-Type': 'multipart/form-data',
      ...(config?.headers || {}),
    },
  })
}

export const initMultipartUpload = (data: MultipartInitRequest, config?: AxiosRequestConfig) => {
  return request.post<MultipartInitResponse>('/files/multipart/init', data, {
    timeout: 0,
    ...config,
  })
}

export const preflightFileUpload = (data: FilePreflightRequest, config?: AxiosRequestConfig) => {
  return request.post<FilePreflightResponse>('/files/preflight', data, {
    timeout: 0,
    ...config,
  })
}

export const uploadMultipartChunk = (
  data: FormData,
  onUploadProgress?: (progressEvent: AxiosProgressEvent) => void,
  config?: AxiosRequestConfig,
) => {
  return request.post<void>('/files/multipart/chunk', data, {
    timeout: 0,
    onUploadProgress,
    ...config,
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  })
}

export const completeMultipartUpload = (upload_id: string, config?: AxiosRequestConfig) => {
  return request.post<FileMetadata>('/files/multipart/complete', { upload_id }, { timeout: 0, ...config })
}

export const getMultipartUploadStatus = (upload_id: string) => {
  return request.get<MultipartInitResponse>(`/files/multipart/${upload_id}`, { timeout: 0 })
}

export const abortMultipartUpload = (upload_id: string) => {
  return request.delete<void>(`/files/multipart/${upload_id}`, { timeout: 0 })
}

export const deleteFile = (id: number) => {
  return request.delete<void>(`/files/${id}`)
}

export const deleteFolder = (id: number) => {
  return request.delete<void>(`/folder/${id}`)
}

export const batchDeleteFiles = (items: BatchDeleteItem[]) => {
  return request.post<void>('/files/batch-delete', { items })
}

export const createFolder = (data: CreateFolderParams) => {
  return request.post<FolderMetadata>('/folder', data)
}

export const updateFolder = (id: number, data: UpdateFileItemParams) => {
  return request.put<FolderMetadata>(`/folder/${id}`, data)
}

export const updateFile = (id: number, data: UpdateFileItemParams) => {
  return request.put<FileMetadata>(`/files/${id}`, data)
}

export const downloadFile = (id: number) => {
  return request.get<Blob>(`/files/${id}/download`, { responseType: 'blob' })
}

export const getRootFolderId = () => {
  return request.get<RootFolderIdResult>('/folder/root_id')
}

export const getAllFolders = () => {
  return request.get<FolderListResult>('/folder/all')
}

export const retryEmbedding = (file_id: number) => {
  return request.post<void>('/files/retry_embedding', { file_id })
}

export const rebuildFailedIndexes = () => {
  return request.post<RebuildIndexesResult>('/files/rebuild_failed_indexes')
}

export const organizeFiles = () => {
  return request.post<OrganizeFilesResult>('/folder/organize')
}

export const getProcessStatus = () => {
  return request.get<ProcessStatusResult>('/files/process_status')
}

/** Protected bytes always travel through the request header interceptor. */
export const downloadFileBlob = (id: number, signal?: AbortSignal) => {
  return request.get<Blob>(`/files/${id}/download`, { responseType: 'blob', signal, timeout: 0 })
}
