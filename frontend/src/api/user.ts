import request from './request'
import type { FileMetadata } from './file'
import type { PasswordChangeInput } from '../schemas/user'

/** 后端契约：/users/{id} 返回的用户资料 */
export interface UserProfile {
  id: number
  username: string
  role: string
  avatar?: string | null
}

/** Validated avatar metadata and the published avatar URL. */
export interface UploadAvatarResult extends FileMetadata {
  avatar: string
  avatar_url: string
}

export const getUserInfo = (id: number) => {
  return request.get<UserProfile>(`/users/${id}`)
}

export const uploadAvatar = (id: number, formData: FormData) => {
  return request.post<UploadAvatarResult>(`/files/upload/avatar/${id}`, formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  })
}

export const updatePassword = (id: number, data: PasswordChangeInput) => {
  // 后端字段为 snake_case，这里从已校验表单映射，confirmPassword 不发送
  return request.put<void>(`/users/${id}/password`, {
    old_password: data.oldPassword,
    new_password: data.newPassword,
  })
}
