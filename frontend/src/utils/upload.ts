import {
    completeMultipartUpload,
    initMultipartUpload,
    preflightFileUpload,
    uploadFile,
    uploadMultipartChunk
} from '../api/file'

type ProgressCallback = (percent: number) => void

interface UploadResult {
    instantUpload: boolean
}

const LARGE_FILE_THRESHOLD = 20 * 1024 * 1024
const CLIENT_DEFAULT_CHUNK_SIZE = 2 * 1024 * 1024
const CHUNK_UPLOAD_RETRIES = 3
const CHUNK_UPLOAD_CONCURRENCY = 1
const RETRY_BASE_DELAY_MS = 500

function clampPercent(value: number): number {
    if (!Number.isFinite(value)) return 0
    if (value < 0) return 0
    if (value > 100) return 100
    return value
}

function getUploadId(file: File, parentId?: number | null): string {
    const raw = `${file.name}:${file.size}:${file.lastModified}:${parentId ?? 0}`
    let hash = 0
    for (let i = 0; i < raw.length; i++) {
        hash = ((hash * 31) + raw.charCodeAt(i)) >>> 0
    }
    return `up_${hash.toString(16)}_${file.size.toString(16)}`
}

function sleep(ms: number) {
    return new Promise(resolve => setTimeout(resolve, ms))
}

async function calculateFileHash(file: File): Promise<string> {
    const buffer = await file.arrayBuffer()
    const digest = await crypto.subtle.digest('SHA-256', buffer)
    return Array.from(new Uint8Array(digest))
        .map(value => value.toString(16).padStart(2, '0'))
        .join('')
}

function getChunkSize(file: File, chunkIndex: number, chunkSize: number): number {
    const start = chunkIndex * chunkSize
    const end = Math.min(file.size, start + chunkSize)
    return Math.max(0, end - start)
}

async function uploadChunkWithRetry(
    file: File,
    uploadId: string,
    chunkIndex: number,
    chunkSize: number
) {
    const start = chunkIndex * chunkSize
    const end = Math.min(file.size, start + chunkSize)
    const chunkBlob = file.slice(start, end)

    for (let attempt = 1; attempt <= CHUNK_UPLOAD_RETRIES; attempt++) {
        try {
            const formData = new FormData()
            formData.append('upload_id', uploadId)
            formData.append('chunk_index', chunkIndex.toString())
            formData.append('chunk', chunkBlob, `${file.name}.part${chunkIndex}`)
            await uploadMultipartChunk(formData)
            return
        } catch (error) {
            if (attempt >= CHUNK_UPLOAD_RETRIES) {
                throw error
            }
            await sleep(RETRY_BASE_DELAY_MS * attempt)
        }
    }
}

async function uploadSmallFile(
    file: File,
    contentHash: string,
    parentId?: number | null,
    onProgress?: ProgressCallback
) : Promise<UploadResult> {
    const preflight = await preflightFileUpload({
        filename: file.name,
        total_size: file.size,
        parent_id: parentId ?? undefined,
        mime_type: file.type || undefined,
        content_hash: contentHash
    })
    if (preflight.instant_upload) {
        onProgress?.(100)
        return {instantUpload: true}
    }

    const formData = new FormData()
    formData.append('file', file)
    if (parentId !== undefined && parentId !== null) {
        formData.append('parent_id', String(parentId))
    }

    await uploadFile(formData, {
        onUploadProgress: (event) => {
            const total = event.total || file.size
            if (!total) return
            const percent = clampPercent((event.loaded / total) * 100)
            onProgress?.(percent)
        }
    })
    onProgress?.(100)
    return {instantUpload: false}
}

async function uploadLargeFile(
    file: File,
    contentHash: string,
    parentId?: number | null,
    onProgress?: ProgressCallback
) : Promise<UploadResult> {
    const initRes = await initMultipartUpload({
        filename: file.name,
        total_size: file.size,
        chunk_size: CLIENT_DEFAULT_CHUNK_SIZE,
        parent_id: parentId ?? undefined,
        mime_type: file.type || undefined,
        content_hash: contentHash,
        upload_id: getUploadId(file, parentId)
    })

    const uploadMeta = initRes as any
    if (uploadMeta.instant_upload) {
        onProgress?.(100)
        return {instantUpload: true}
    }
    const uploadId = uploadMeta.upload_id as string
    const chunkSize = uploadMeta.chunk_size as number
    const totalChunks = uploadMeta.total_chunks as number
    const uploadedChunks = new Set<number>((uploadMeta.uploaded_chunks || []) as number[])

    let uploadedBytes = 0
    for (const idx of uploadedChunks) {
        uploadedBytes += getChunkSize(file, idx, chunkSize)
    }
    onProgress?.(clampPercent((uploadedBytes / file.size) * 100))

    const pendingChunks: number[] = []
    for (let i = 0; i < totalChunks; i++) {
        if (!uploadedChunks.has(i)) {
            pendingChunks.push(i)
        }
    }

    let cursor = 0
    let firstError: unknown = null

    const worker = async () => {
        while (true) {
            if (firstError) return
            const current = cursor
            cursor += 1
            if (current >= pendingChunks.length) return

            const chunkIndex = pendingChunks[current]
            if (chunkIndex === undefined) return
            try {
                await uploadChunkWithRetry(file, uploadId, chunkIndex, chunkSize)
                uploadedBytes += getChunkSize(file, chunkIndex, chunkSize)
                onProgress?.(clampPercent((uploadedBytes / file.size) * 100))
            } catch (error) {
                firstError = error
                return
            }
        }
    }

    const workers = Array.from(
        {length: Math.min(CHUNK_UPLOAD_CONCURRENCY, pendingChunks.length)},
        () => worker()
    )
    await Promise.all(workers)

    if (firstError) {
        throw firstError
    }

    await completeMultipartUpload(uploadId)
    onProgress?.(100)
    return {instantUpload: false}
}

export async function uploadFileOptimized(
    file: File,
    parentId?: number | null,
    onProgress?: ProgressCallback
) {
    const contentHash = await calculateFileHash(file)
    if (file.size <= LARGE_FILE_THRESHOLD) {
        return uploadSmallFile(file, contentHash, parentId, onProgress)
    }
    return uploadLargeFile(file, contentHash, parentId, onProgress)
}
