/**
 * 通用分页 composable：封装 Arco `a-table` 所需的分页 reactive 对象与翻页回调，
 * 替换 useFileBrowser / TokenUsageView / AdminTokenUsageView 中重复的翻页样板。
 *
 * 绑定方式（Wave 2 直接使用）：
 *   <a-table :pagination="pagination" @page-change="handlePageChange" @page-size-change="handlePageSizeChange" />
 *
 * 裁决说明：任务给出的接口草案里 PaginationState 字段为 `Ref<number>`，但 Arco `a-table`
 * 的 `pagination` 属性按现有用法需要的是 reactive 对象的「普通字段」（current/pageSize/total
 * 均为 number，参见 useFileBrowser.pagination 与 logsPagination）。为满足「Wave 2 直接绑定
 * `:pagination="pagination"`」，这里采用 reactive 构造、字段为普通 number，PaginationState
 * 接口相应声明为普通字段（非 Ref），与现有 Arco 用法严格一致。
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
    // 初始每页条数，默认 10（与 useFileBrowser 一致；logs 场景可传 15）。
    defaultPageSize?: number
    // 可选每页条数下拉，默认 [10, 20, 50, 100]（logs 场景可传 [15, 30, 50]）。
    pageSizeOptions?: number[]
    // 页码或每页条数变化时的回调（由调用方触发数据刷新）。
    onChange: (page: number, pageSize: number) => void | Promise<void>
}

export interface UsePaginationReturn {
    // 直接绑定到 a-table 的 :pagination。
    pagination: PaginationState
    // 绑定到 a-table 的 @page-change。
    handlePageChange: (page: number) => void
    // 绑定到 a-table 的 @page-size-change。
    handlePageSizeChange: (size: number) => void
    // 回到首页（保留 total 与 pageSize），对应现有 handleFilterChange「筛选条件变更后回到第 1 页」语义。
    reset: () => void
    // 设置总条数。
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
