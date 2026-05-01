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

export function getMyTokenStats() {
  return request.get<TokenStats>('/token-usage/stats')
}

export function getMyUsageLogs(params: {
  page?: number
  page_size?: number
  action?: string
  start_date?: string
  end_date?: string
}) {
  return request.get<TokenUsageLogsResponse>('/token-usage/logs', { params })
}

export function getMyDailyStats(days = 30) {
  return request.get<DailyStat[]>('/token-usage/daily', { params: { days } })
}
