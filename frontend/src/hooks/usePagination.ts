/**
 * 通用分页 composable：封装 Arco `a-table` 所需的分页 reactive 对象与翻页回调。
 *
 * 字段用普通 number（非 Ref），以便直接绑定 `:pagination="pagination"`；
 * 筛选条件变更后应调用 reset() 回到第 1 页，避免 total 变化后仍停留在空页。
 */
import {reactive} from 'vue'
import {logger} from '@/utils/logger'

export interface PaginationState {
    current: number
    pageSize: number
    total: number
    showTotal: boolean
    showPageSize: boolean
    pageSizeOptions: number[]
}

export interface UsePaginationOptions {
    /** 初始每页条数；默认 10，日志列表等可传 15 */
    defaultPageSize?: number
    /** 每页条数选项；默认 [10, 20, 50, 100] */
    pageSizeOptions?: number[]
    /** 页码或每页条数变化时由调用方拉取数据 */
    onChange: (page: number, pageSize: number) => void | Promise<void>
}

export interface UsePaginationReturn {
    pagination: PaginationState
    handlePageChange: (page: number) => void
    handlePageSizeChange: (size: number) => void
    /** 回到第 1 页（保留 total 与 pageSize），用于筛选条件变更后 */
    reset: () => void
    setTotal: (n: number) => void
}

export function usePagination(options: UsePaginationOptions): UsePaginationReturn {
    const defaultPageSize = options.defaultPageSize ?? 10
    const pageSizeOptions = options.pageSizeOptions ?? [10, 20, 50, 100]

    const pagination = reactive<PaginationState>({
        current: 1,
        pageSize: defaultPageSize,
        total: 0,
        showTotal: true,
        showPageSize: true,
        pageSizeOptions,
    })

    const fireChange = (page: number, pageSize: number) => {
        // onChange 由调用方负责真正的数据拉取（内部通常自带错误处理）；
        // 这里兜底捕获 Promise 拒绝，避免翻页时未处理拒绝污染全局。
        Promise.resolve(options.onChange(page, pageSize)).catch((error) => {
            logger.warn('usePagination onChange 回调失败', {page, pageSize, error})
        })
    }

    const handlePageChange = (page: number) => {
        pagination.current = page
        fireChange(page, pagination.pageSize)
    }

    const handlePageSizeChange = (size: number) => {
        // 切换每页条数后回到首页，与现有 useFileBrowser.handlePageSizeChange / logs 一致。
        pagination.pageSize = size
        pagination.current = 1
        fireChange(1, size)
    }

    const reset = () => {
        pagination.current = 1
    }

    const setTotal = (n: number) => {
        pagination.total = n
    }

    return {
        pagination,
        handlePageChange,
        handlePageSizeChange,
        reset,
        setTotal,
    }
}
