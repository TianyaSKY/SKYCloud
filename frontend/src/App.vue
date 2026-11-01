<script lang="ts" setup>
import { computed } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import ErrorBoundary from '@/components/ErrorBoundary.vue'
import AssistantWidget from '@/components/assistant/AssistantWidget.vue'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const auth = useAuthStore()

// 助手挂载在应用级，路由切换只显隐、不销毁 Composer 子树。
const showAssistant = computed(() => auth.isAuthenticated && route.name === 'home')
</script>

<template>
  <ErrorBoundary>
    <RouterView />
    <AssistantWidget :show="showAssistant" />
  </ErrorBoundary>
</template>

<style>
body {
  margin: 0;
  padding: 0;
  font-family:
    'Inter',
    -apple-system,
    BlinkMacSystemFont,
    'Segoe UI',
    Roboto,
    Oxygen,
    Ubuntu,
    Cantarell,
    'Open Sans',
    'Helvetica Neue',
    sans-serif;
}
</style>
