<template>
  <a-layout-sider :width="220" breakpoint="lg" class="sidebar">
    <div class="logo">
      <span class="title">SKYCloud</span>
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
import {computed} from 'vue'
import {IconEmail, IconFile, IconSettings, IconShareAlt} from '@arco-design/web-vue/es/icon'

defineProps<{
  activeMenu: string
}>()

const emit = defineEmits(['menu-click'])

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
  padding-left: 20px;
  border-bottom: 1px solid var(--color-border);
}

.title {
  font-size: 18px;
  font-weight: bold;
  color: var(--color-text-1);
}
</style>
