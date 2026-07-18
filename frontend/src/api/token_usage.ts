import request from './request'

/** 后端契约：当前用户 Token 累计统计 */
export interface TokenStats {
    user_id: number
    total_prompt_tokens: number
    total_completion_tokens: number
    total_tokens: number
    last_active_at: string | null
}

/** 后端契约：单条 Token 用量日志 */
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

/** 后端契约：用量日志分页 */
export interface TokenUsageLogsResponse {
    total: number
    page: number
    page_size: number
    items: TokenUsageLog[]
}

/** 后端契约：按日聚合统计 */
export interface DailyStat {
    date: string
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    request_count: number
}

/** 用量日志查询参数 */
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

// —— 管理员接口（需 admin 角色）——

/** 后端契约：管理员视角下各用户 Token 汇总 */
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

/** 后端契约：管理员用量日志（附带 username） */
export interface AdminTokenUsageLog extends TokenUsageLog {
    username: string
}

/** 后端契约：管理员用量日志分页 */
export interface AdminTokenUsageLogsResponse {
    total: number
    page: number
    page_size: number
    items: AdminTokenUsageLog[]
}

/** 后端契约：按用户+日聚合 */
export interface PerUserDailyStat extends DailyStat {
    user_id: number
    username: string
}

/** 管理员日志查询，可按 user_id 筛选 */
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
