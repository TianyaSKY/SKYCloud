import request from './request'

export interface Message {
    id: number
    title: string
    content: string
    is_read: boolean
    created_at: string
}

export interface InboxResponse {
    items: Message[]
    total: number
    page: number
    per_page: number
}

export const getInboxMessages = (page = 1, per_page = 10) => {
    return request.get<InboxResponse>('/inbox', {
        params: {page, per_page}
    })
}

export const markAsRead = (id: number) => {
    return request.put(`/inbox/${id}/read`)
}

export const markAllAsRead = () => {
    return request.put('/inbox/read-all')
}

export const deleteMessage = (id: number) => {
    return request.delete(`/inbox/${id}`)
}
