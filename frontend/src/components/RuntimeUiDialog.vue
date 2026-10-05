<template>
  <a-modal :visible="modelValue" @update:visible="emit('update:modelValue', $event)"
    :title="workspace?.name || '工作区'" fullscreen :footer="false" :mask-closable="false" unmount-on-close>
    <iframe v-if="launchUrl" :src="launchUrl" class="runtime-ui-frame"
      sandbox="allow-scripts allow-same-origin allow-downloads allow-forms"
      referrerpolicy="no-referrer" allow="clipboard-write; clipboard-read" />
    <a-spin v-else tip="正在打开工作区…" />
  </a-modal>
</template>

<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { Message } from '@arco-design/web-vue'
import type { WorkspaceInfo } from '@/api/workspace'
import { runtimeUiLaunchUrl, startRuntimeUiRenewal } from '@/utils/runtimeUi'
import { logger } from '@/utils/logger'

const props = defineProps<{ modelValue: boolean; workspace: WorkspaceInfo | null }>()
const emit = defineEmits<{ 'update:modelValue': [visible: boolean] }>()
const launchUrl = ref('')
let generation = 0
let stopRenewal: (() => void) | undefined

function clear() {
  generation += 1
  stopRenewal?.()
  stopRenewal = undefined
  launchUrl.value = ''
}

watch(() => props.modelValue ? props.workspace?.id : null, async (id) => {
  clear()
  if (!id) return
  const current = generation
  try {
    const url = await runtimeUiLaunchUrl(id)
    if (current !== generation) return
    launchUrl.value = url
    stopRenewal = startRuntimeUiRenewal(id, url, () => {
      if (current !== generation) return
      clear()
      emit('update:modelValue', false)
      Message.warning('工作区会话已失效，请重新打开')
    })
  } catch (error) {
    if (current !== generation) return
    logger.warn('工作区入口加载失败', error)
    emit('update:modelValue', false)
  }
}, { immediate: true })

onUnmounted(clear)
</script>

<style scoped>
.runtime-ui-frame { width: 100%; height: calc(100vh - 90px); border: 0; display: block; }
</style>
