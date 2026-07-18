import { onMounted, reactive, ref, shallowRef } from 'vue'
import type { FileItem } from '../api/file'
import { getFiles, getProcessStatus, getRootFolderId, searchFiles } from '../api/file'
import { Notification } from '@arco-design/web-vue'
import { logger } from '../utils/logger'

export function useFileBrowser() {
  const loading = ref(false)
  // 大列表使用 shallowRef 避免对每个元素深度代理，降低开销
  const fileList = shallowRef<FileItem[]>([])
  const searchKey = ref('')
  const searchType = ref<'fuzzy' | 'vector'>('vector')
  const isVectorSearch = ref(true)
  const isSearching = ref(false)
  const currentParentId = ref<number | null>(null)
  const breadcrumbs = ref<{ id: number; name: string }[]>([])
  const selectedKeys = ref<number[]>([])

  const pagination = reactive({
    current: 1,
    pageSize: 10,
    total: 0,
    showTotal: true,
    showPageSize: true,
    pageSizeOptions: [10, 20, 50, 100],
  })

  const sortState = reactive({
    sortBy: 'created_at',
    order: 'desc',
  })

  // fetchFiles 请求序号：快速翻页/排序时丢弃过期响应，防止旧结果覆盖新结果
  let fetchSeq = 0

  const initSearchType = () => {
    const savedType = localStorage.getItem('searchType')
    if (savedType) {
      searchType.value = savedType as 'fuzzy' | 'vector'
      isVectorSearch.value = savedType === 'vector'
    }
  }

  const saveSearchType = (type: 'fuzzy' | 'vector') => {
    localStorage.setItem('searchType', type)
  }

  const initPageSettings = () => {
    const savedPageSize = localStorage.getItem('pageSize')
    if (savedPageSize) {
      pagination.pageSize = parseInt(savedPageSize, 10)
    }

    const savedSortBy = localStorage.getItem('sortBy')
    const savedOrder = localStorage.getItem('sortOrder')
    if (savedSortBy) {
      sortState.sortBy = savedSortBy
    }
    if (savedOrder) {
      sortState.order = savedOrder
    }
  }

  const checkProcessStatus = async () => {
    try {
      const data = await getProcessStatus()
      if (data['处理中'] > 0 || data['失败'] > 0) {
        Notification.info({
          title: '文件处理状态',
          content: `当前有 ${data['处理中']} 个文件正在处理中，${data['失败']} 个文件处理失败。`,
          position: 'bottomRight',
          duration: 5000,
        })
      }
    } catch (error) {
      logger.warn('checkProcessStatus 查询失败 error={}', error)
    }
  }

  const fetchFiles = async () => {
    const myId = ++fetchSeq
    loading.value = true
    try {
      let folders: FileItem[] = []
      let files: FileItem[] = []
      let total = 0

      if (isSearching.value && searchKey.value) {
        const data = await searchFiles({
          q: searchKey.value,
          page: pagination.current,
          page_size: pagination.pageSize,
          type: searchType.value,
        })
        // 请求返回期间若已发起新请求，丢弃本次过期结果
        if (myId !== fetchSeq) return
        files = data.items
        total = data.total
      } else {
        const data = await getFiles({
          parent_id: currentParentId.value || undefined,
          page: pagination.current,
          page_size: pagination.pageSize,
          sort_by: sortState.sortBy,
          order: sortState.order as 'asc' | 'desc',
        })
        if (myId !== fetchSeq) return
        // FileListResponse 为并集结构：优先 items，其次 files（分页或数组），再取 folders
        if (data.items) {
          files = data.items
          total = data.total ?? 0
        } else if (data.files) {
          if (Array.isArray(data.files)) {
            files = data.files
          } else {
            files = data.files.items
            total = data.files.total
          }
        }
        if (Array.isArray(data.folders)) {
          folders = data.folders
        }
      }

      const processedFolders = folders.map((f): FileItem => ({
        ...f,
        is_folder: true,
        size: 0,
        updated_at: f.updated_at || f.created_at,
      }))
      const processedFiles = files.map((f): FileItem => ({
        ...f,
        is_folder: false,
        size: f.file_size || f.size,
        updated_at: f.updated_at || f.created_at,
      }))

      // shallowRef 需整体替换以触发响应
      fileList.value = [...processedFolders, ...processedFiles]
      pagination.total = total || fileList.value.length

      // 列表刷新后同步索引/处理状态角标
      await checkProcessStatus()
    } catch (error) {
      if (myId !== fetchSeq) return
      logger.warn('fetchFiles 失败 error={}', error)
    } finally {
      // 仅当前最新请求负责复位 loading，避免被过期请求误清
      if (myId === fetchSeq) {
        loading.value = false
      }
    }
  }

  const handleSorterChange = ({ dataIndex, direction }: { dataIndex: string; direction: string }) => {
    if (!direction) {
      sortState.sortBy = 'created_at'
      sortState.order = 'desc'
    } else {
      sortState.sortBy = dataIndex === 'updated_at' ? 'updated_at' : dataIndex
      sortState.order = direction === 'ascend' ? 'asc' : 'desc'
    }

    localStorage.setItem('sortBy', sortState.sortBy)
    localStorage.setItem('sortOrder', sortState.order)

    fetchFiles()
  }

  const resetSearchAndFetch = () => {
    pagination.current = 1
    isSearching.value = false
    searchKey.value = ''
    fetchFiles()
  }

  const goRoot = async () => {
    try {
      const res = await getRootFolderId()
      currentParentId.value = res.root_folder_id
      breadcrumbs.value = []
      selectedKeys.value = []
      resetSearchAndFetch()
    } catch (error) {
      logger.warn('goRoot 获取根目录失败 error={}', error)
    }
  }

  const goBreadcrumb = (index: number) => {
    const target = breadcrumbs.value[index]
    if (target) {
      currentParentId.value = target.id
      breadcrumbs.value = breadcrumbs.value.slice(0, index + 1)
      selectedKeys.value = []
      resetSearchAndFetch()
    }
  }

  const enterFolder = (record: FileItem) => {
    currentParentId.value = record.id
    breadcrumbs.value.push({ id: record.id, name: record.name })
    selectedKeys.value = []
    resetSearchAndFetch()
  }

  const handleSearch = () => {
    if (searchKey.value) {
      isSearching.value = true
      pagination.current = 1
      selectedKeys.value = []
      fetchFiles()
    } else {
      handleSearchClear()
    }
  }

  const handleSearchClear = () => {
    isSearching.value = false
    searchKey.value = ''
    pagination.current = 1
    selectedKeys.value = []
    fetchFiles()
  }

  const handlePageChange = (page: number) => {
    pagination.current = page
    fetchFiles()
  }

  const handlePageSizeChange = (pageSize: number) => {
    pagination.pageSize = pageSize
    pagination.current = 1
    localStorage.setItem('pageSize', pageSize.toString())
    fetchFiles()
  }

  onMounted(() => {
    initSearchType()
    initPageSettings()
    goRoot()
  })

  return {
    loading,
    fileList,
    searchKey,
    searchType,
    isVectorSearch,
    isSearching,
    currentParentId,
    breadcrumbs,
    selectedKeys,
    pagination,
    sortState,
    fetchFiles,
    handleSorterChange,
    goRoot,
    goBreadcrumb,
    enterFolder,
    handleSearch,
    handleSearchClear,
    handlePageChange,
    handlePageSizeChange,
    saveSearchType,
  }
}
