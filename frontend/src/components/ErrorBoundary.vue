<script lang="ts" setup>
import { ref, onErrorCaptured } from 'vue'
import { Button as AButton } from '@arco-design/web-vue'
import { logger } from '@/utils/logger'

const error = ref<Error | null>(null)

onErrorCaptured((err, instance, info) => {
  error.value = err as Error
  logger.error('组件渲染异常: {} | info={} | component={}', err, info, instance?.$options?.__name ?? 'Unknown')
  // 返回 false 阻止异常继续向上冒泡到 app.config.errorHandler
  return false
})

const reset = () => {
  error.value = null
}
</script>

<template>
  <div v-if="error" class="error-boundary">
    <div class="error-boundary__card">
      <div class="error-boundary__icon">!</div>
      <h3 class="error-boundary__title">页面渲染异常</h3>
      <p class="error-boundary__desc">抱歉，该区域发生错误。可尝试重新加载该区块。</p>
      <a-button type="primary" @click="reset">重试</a-button>
    </div>
  </div>
  <slot v-else />
</template>

<style scoped>
.error-boundary {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px;
  width: 100%;
}

.error-boundary__card {
  text-align: center;
  max-width: 360px;
}

.error-boundary__icon {
  width: 48px;
  height: 48px;
  border-radius: 50%;
  background: rgb(var(--danger-6));
  color: #fff;
  font-size: 24px;
  font-weight: 700;
  line-height: 48px;
  margin: 0 auto 16px;
}

.error-boundary__title {
  margin: 0 0 8px;
  font-size: 16px;
  color: var(--color-text-1);
}

.error-boundary__desc {
  margin: 0 0 20px;
  font-size: 13px;
  color: var(--color-text-3);
}
</style>
