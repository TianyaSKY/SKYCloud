import request from './request'

export const getUserInfo = (id: number) => {
    return request.get(`/users/${id}`)
}

export const uploadAvatar = (id: number, formData: FormData) => {
    return request.post(`/files/upload/avatar/${id}`, formData, {
        headers: {
            'Content-Type': 'multipart/form-data'
        }
    })
}
