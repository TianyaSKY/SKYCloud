import request from './request'

/** 后端契约：创建分享请求体；expires_at 省略表示永久有效 */
export interface CreateShareParams {
  file_id: number
  expires_at?: string
}

/** 后端契约：分享记录；token 用于公开下载链接，日期和文件名可能为空 */
export interface ShareInfo {
  id: number
  token: string
  file_id: number
  expires_at: string | null
  created_at: string | null
  file_name: string | null
  link: string
}

export const createShare = (data: CreateShareParams) => {
  return request.post<ShareInfo>('/share', data)
}

export const getMyShares = () => {
  return request.get<ShareInfo[]>('/share/my')
}

export const cancelShare = (id: number) => {
  return request.delete<{message: string}>(`/share/${id}`)
}
