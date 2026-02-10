import type {AxiosProgressEvent, AxiosRequestConfig} from 'axios'
import request from './request'

export interface FileItem {
    id: number
    name: string
    size: number
    type: string
    updated_at: string
    is_folder: boolean
}

export interface ListFilesParams {
    parent_id?: number | null
    page?: number
    page_size?: number
    name?: string
    sort_by?: string
    order?: 'asc' | 'desc'
}

export interface SearchFilesParams {
    q: string
    page?: number
    page_size?: number
    type?: 'fuzzy' | 'vector'
}

export interface MultipartInitRequest {
    filename: string
    total_size: number
    chunk_size?: number
    parent_id?: number | null
    mime_type?: string
    upload_id?: string
}

export interface MultipartInitResponse {
    upload_id: string
    chunk_size: number
    total_chunks: number
    uploaded_chunks: number[]
}

export const getFiles = (params?: ListFilesParams) => {
    return request.get('/files/list', {params})
}

export const searchFiles = (params: SearchFilesParams) => {
    return request.get('/files/search', {params})
}

export const uploadFile = (data: FormData, config?: AxiosRequestConfig) => {
    return request.post('/files', data, {
        timeout: 0,
        ...config,
        headers: {
            'Content-Type': 'multipart/form-data',
            ...(config?.headers || {})
        }
    })
}

export const batchUploadFiles = (data: FormData, config?: AxiosRequestConfig) => {
    return request.post('/files/batch', data, {
        timeout: 0,
        ...config,
        headers: {
            'Content-Type': 'multipart/form-data',
            ...(config?.headers || {})
        }
    })
}

export const initMultipartUpload = (data: MultipartInitRequest) => {
    return request.post<MultipartInitResponse>('/files/multipart/init', data, {
        timeout: 0
    })
}

export const uploadMultipartChunk = (
    data: FormData,
    onUploadProgress?: (progressEvent: AxiosProgressEvent) => void
) => {
    return request.post('/files/multipart/chunk', data, {
        timeout: 0,
        onUploadProgress,
        headers: {
            'Content-Type': 'multipart/form-data'
        }
    })
}

export const completeMultipartUpload = (upload_id: string) => {
    return request.post('/files/multipart/complete', {upload_id}, {timeout: 0})
}

export const getMultipartUploadStatus = (upload_id: string) => {
    return request.get<MultipartInitResponse>(`/files/multipart/${upload_id}`, {timeout: 0})
}

export const abortMultipartUpload = (upload_id: string) => {
    return request.delete(`/files/multipart/${upload_id}`, {timeout: 0})
}

export const deleteFile = (id: number) => {
    return request.delete(`/files/${id}`)
}

export const deleteFolder = (id: number) => {
    return request.delete(`/folder/${id}`)
}

export const batchDeleteFiles = (items: { id: number, is_folder: boolean }[]) => {
    return request.post('/files/batch-delete', {items})
}

export const createFolder = (data: { name: string, parent_id?: number }) => {
    return request.post('/folder', data)
}

export const updateFolder = (id: number, data: { name?: string, parent_id?: number | null }) => {
    return request.put(`/folder/${id}`, data)
}

export const updateFile = (id: number, data: { name?: string, parent_id?: number | null }) => {
    return request.put(`/files/${id}`, data)
}

export const downloadFile = (id: number) => {
    return request.get(`/files/${id}/download`, {responseType: 'blob'})
}

export const getRootFolderId = () => {
    return request.get('/folder/root_id')
}

export const getAllFolders = () => {
    return request.get('/folder/all')
}

export const retryEmbedding = (file_id: number) => {
    return request.post('/files/retry_embedding', {file_id})
}

export const rebuildFailedIndexes = () => {
    return request.post('/files/rebuild_failed_indexes')
}

export const organizeFiles = () => {
    return request.post('/folder/organize')
}

export const getProcessStatus = () => {
    return request.get('/files/process_status')
}
