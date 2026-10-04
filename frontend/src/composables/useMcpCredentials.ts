import { onBeforeUnmount, ref, watch } from 'vue'
import { getMcpToken, refreshMcpToken, type McpTokenMeta } from '@/api/auth'

export function useMcpCredentials() {
  const visible = ref(false)
  const loading = ref(false)
  const refreshing = ref(false)
  const rawToken = ref('')
  const metadata = ref<McpTokenMeta | null>(null)
  let generation = 0

  function clear() {
    generation++
    rawToken.value = ''
    metadata.value = null
    loading.value = false
    refreshing.value = false
  }

  watch(visible, (open) => { if (!open) clear() }, { flush: 'sync' })
  onBeforeUnmount(clear)

  async function open() {
    clear()
    visible.value = true
    const current = generation
    loading.value = true
    try {
      const result = await getMcpToken()
      if (visible.value && current === generation) metadata.value = result.token
    } finally {
      if (current === generation) loading.value = false
    }
  }

  async function issue() {
    if (!visible.value || refreshing.value) return false
    const current = generation
    refreshing.value = true
    rawToken.value = ''
    try {
      const result = await refreshMcpToken()
      if (!visible.value || current !== generation) return false
      rawToken.value = result.mcp_token
      metadata.value = result.token
      return true
    } finally {
      if (current === generation) refreshing.value = false
    }
  }

  return { visible, loading, refreshing, rawToken, metadata, open, issue }
}
