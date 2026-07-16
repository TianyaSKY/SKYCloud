import request from './request'

export interface TokenStats {
    user_id: number
    total_prompt_tokens: number
    total_completion_tokens: number
    total_tokens: number
    last_active_at: string | null
}

export interface TokenUsageLog {
    id: number
    user_id: number
    action: string
    model_name: string | null
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    query_summary: string | null
    extra_info: string | null
    created_at: string
}

export interface TokenUsageLogsResponse {
    total: number
    page: number
    page_size: number
    items: TokenUsageLog[]
}

export interface DailyStat {
    date: string
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    request_count: number
}

export interface UsageLogQueryParams {
    page?: number
    page_size?: number
    action?: string
    start_date?: string
    end_date?: string
}

export function getMyTokenStats() {
    return request.get<TokenStats>('/token-usage/stats')
}

export function getMyUsageLogs(params: UsageLogQueryParams) {
    return request.get<TokenUsageLogsResponse>('/token-usage/logs', {params})
}

export function getMyDailyStats(days = 30) {
    return request.get<DailyStat[]>('/token-usage/daily', {params: {days}})
}

// ===================== 管理员接口 =====================

export interface UserTokenStats {
    user_id: number
    username: string
    role: string
    avatar: string | null
    total_prompt_tokens: number
    total_completion_tokens: number
    total_tokens: number
    last_active_at: string | null
    created_at: string | null
}

export interface AdminTokenUsageLog extends TokenUsageLog {
    username: string
}

export interface AdminTokenUsageLogsResponse {
    total: number
    page: number
    page_size: number
    items: AdminTokenUsageLog[]
}

export interface PerUserDailyStat extends DailyStat {
    user_id: number
    username: string
}

export interface AdminUsageLogQueryParams extends UsageLogQueryParams {
    user_id?: number
}

export function getAdminAllUsersStats() {
    return request.get<UserTokenStats[]>('/admin/token-usage/users')
}

export function getAdminAllUsageLogs(params: AdminUsageLogQueryParams) {
    return request.get<AdminTokenUsageLogsResponse>('/admin/token-usage/logs', {params})
}

export function getAdminDailyStats(days = 30) {
    return request.get<DailyStat[]>('/admin/token-usage/daily', {params: {days}})
}

export function getAdminPerUserDailyStats(days = 30) {
    return request.get<PerUserDailyStat[]>('/admin/token-usage/daily/per-user', {params: {days}})
}
