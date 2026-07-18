<template>
  <a-layout-sider 
    :width="220" 
    breakpoint="lg" 
    class="sidebar"
    collapsible
    :hide-trigger="true"
    v-model:collapsed="collapsed"
  >
    <div class="logo" :style="{ padding: collapsed ? '0' : '0 16px', justifyContent: collapsed ? 'center' : 'space-between' }">
      <span class="title" v-if="!collapsed">SKYCloud</span>
      <a-button
          type="text"
          :aria-label="collapsed ? '展开侧栏' : '折叠侧栏'"
          :aria-expanded="!collapsed"
          @click="collapsed = !collapsed"
          style="color: var(--color-text-2); font-size: 16px; padding: 0 8px;"
      >
        <template #icon>
          <icon-menu-unfold v-if="collapsed" />
          <icon-menu-fold v-else />
        </template>
      </a-button>
    </div>
    <a-menu
        :selected-keys="[activeMenu]"
        :style="{ width: '100%' }"
        @menu-item-click="handleMenuClick"
    >
      <a-menu-item key="all">
        <template #icon>
          <icon-file/>
        </template>
        全部文件
      </a-menu-item>
      <a-menu-item key="share">
        <template #icon>
          <icon-share-alt/>
        </template>
        我的分享
      </a-menu-item>
      <a-menu-item key="inbox">
        <template #icon>
          <icon-email/>
        </template>
        收件箱
      </a-menu-item>

      <a-menu-item key="workspace">
        <template #icon>
          <icon-desktop/>
        </template>
        工作区
      </a-menu-item>
      <a-menu-item key="token-usage">
        <template #icon>
          <icon-bar-chart/>
        </template>
        用量统计
      </a-menu-item>
      <a-menu-item v-if="isAdmin" key="admin-token-usage">
        <template #icon>
          <icon-dashboard/>
        </template>
        用量管理
      </a-menu-item>
      <a-menu-item v-if="isAdmin" key="sys-dicts">
        <template #icon>
          <icon-settings/>
        </template>
        系统字典
      </a-menu-item>
    </a-menu>
  </a-layout-sider>
</template>

<script lang="ts" setup>
import {ref, watch} from 'vue'
import {storeToRefs} from 'pinia'
import {IconBarChart, IconDashboard, IconDesktop, IconEmail, IconFile, IconSettings, IconShareAlt, IconMenuFold, IconMenuUnfold} from '@arco-design/web-vue/es/icon'
import {useAuthStore} from '@/stores/auth'

defineProps<{
  activeMenu: string
}>()

const emit = defineEmits(['menu-click'])

const auth = useAuthStore()
// 响应式订阅 store，登录态/角色变化会自动刷新菜单
const {isAdmin} = storeToRefs(auth)

const collapsed = ref(localStorage.getItem('sidebar_collapsed') === 'true')

watch(collapsed, (newVal) => {
  localStorage.setItem('sidebar_collapsed', String(newVal))
})

const handleMenuClick = (key: string) => {
  emit('menu-click', key)
}
</script>

<style scoped>
.sidebar {
  height: 100%;
  border-right: 1px solid var(--color-border);
}

.logo {
  height: 64px;
  display: flex;
  align-items: center;
  border-bottom: 1px solid var(--color-border);
  transition: all 0.2s;
}

.title {
  font-size: 18px;
  font-weight: bold;
  color: var(--color-text-1);
}
</style>

