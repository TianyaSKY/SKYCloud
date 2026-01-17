import {onMounted, reactive, ref} from 'vue'
import {getFiles, getRootFolderId, searchFiles} from '../api/file'

export function useFileBrowser() {
    const loading = ref(false)
    const fileList = ref<any[]>([])
    const searchKey = ref('')
    const searchType = ref<'fuzzy' | 'vector'>('vector')
    const isVectorSearch = ref(true)
    const isSearching = ref(false)
    const currentParentId = ref<number | null>(null)
    const breadcrumbs = ref<{ id: number, name: string }[]>([])
    const selectedKeys = ref<number[]>([])

    const pagination = reactive({
        current: 1,
        pageSize: 10,
        total: 0,
        showTotal: true,
        showPageSize: true,
        pageSizeOptions: [10, 20, 50, 100]
    })

    const sortState = reactive({
        sortBy: 'created_at',
        order: 'desc'
    })

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

    const fetchFiles = async () => {
        loading.value = true
        try {
            let res: any
            if (isSearching.value && searchKey.value) {
                res = await searchFiles({
                    q: searchKey.value,
                    page: pagination.current,
                    page_size: pagination.pageSize,
                    type: searchType.value
                })
            } else {
                res = await getFiles({
                    parent_id: currentParentId.value || undefined,
                    page: pagination.current,
                    page_size: pagination.pageSize,
                    sort_by: sortState.sortBy,
                    order: sortState.order as 'asc' | 'desc'
                })
            }

            const data = res.data || res
            let folders: any[] = []
            let files: any[] = []
            let total = 0

            if (data.items) {
                files = data.items
                total = data.total
            } else {
                if (data.files?.items) {
                    files = data.files.items
                    total = data.files.total
                } else if (Array.isArray(data.files)) {
                    files = data.files
                }
                if (Array.isArray(data.folders)) {
                    folders = data.folders
                }
            }

            const processedFolders = folders.map((f: any) => ({
                ...f, is_folder: true, size: 0, updated_at: f.updated_at || f.created_at
            }))
            const processedFiles = files.map((f: any) => ({
                ...f, is_folder: false, size: f.file_size || f.size, updated_at: f.updated_at || f.created_at
            }))

            fileList.value = [...processedFolders, ...processedFiles]
            pagination.total = total || fileList.value.length
        } catch (error) {
            console.error('Fetch files error:', error)
        } finally {
            loading.value = false
        }
    }

    const handleSorterChange = ({dataIndex, direction}: { dataIndex: string, direction: string }) => {
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
            const res: any = await getRootFolderId()
            currentParentId.value = res.root_folder_id || res
            breadcrumbs.value = []
            selectedKeys.value = []
            resetSearchAndFetch()
        } catch (error) {
            console.error('Get root folder id error:', error)
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

    const enterFolder = (record: any) => {
        currentParentId.value = record.id
        breadcrumbs.value.push({id: record.id, name: record.name})
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
        saveSearchType
    }
}
