import request from './request'

/** 后端契约：站内信单条消息 */
export interface Message {
    id: number
    title: string
    content: string
    is_read: boolean
    created_at: string
}

/** 后端契约：/inbox 分页列表 */
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
    return request.put<void>(`/inbox/${id}/read`)
}

export const markAllAsRead = () => {
    return request.put<void>('/inbox/read-all')
}

export const deleteMessage = (id: number) => {
    return request.delete<void>(`/inbox/${id}`)
}
