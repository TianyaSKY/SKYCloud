export interface ApiErrorDetail { code: string; message: string }

/** Parse the public REST error envelope; never display arbitrary response data. */
export function apiErrorDetail(payload: unknown, fallback: string): ApiErrorDetail {
  if (payload && typeof payload === 'object' && 'success' in payload && payload.success === false && 'error' in payload) {
    const error = payload.error
    if (error && typeof error === 'object' && 'code' in error && 'message' in error &&
        typeof error.code === 'string' && typeof error.message === 'string') {
      return { code: error.code, message: error.message }
    }
  }
  return { code: 'HTTP_ERROR', message: fallback }
}
