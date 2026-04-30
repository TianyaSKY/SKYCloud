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
      <a-button type="text" @click="collapsed = !collapsed" style="color: var(--color-text-2); font-size: 16px; padding: 0 8px;">
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

      <a-menu-item key="mcp">
        <template #icon>
          <icon-command/>
        </template>
        MCP 服务
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
import {computed, ref} from 'vue'
import {IconCommand, IconEmail, IconFile, IconSettings, IconShareAlt, IconMenuFold, IconMenuUnfold} from '@arco-design/web-vue/es/icon'

defineProps<{
  activeMenu: string
}>()

const emit = defineEmits(['menu-click'])

const collapsed = ref(false)

const isAdmin = computed(() => {
  const userStr = localStorage.getItem('user')
  const role = JSON.parse(userStr || '{}').role
  return role == "admin";
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

