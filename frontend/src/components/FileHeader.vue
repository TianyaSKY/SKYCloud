<template>
  <a-layout-header class="header">
    <div class="header-left">
      <a-breadcrumb v-if="showBreadcrumbs" class="custom-breadcrumb">
        <a-breadcrumb-item
            :class="['breadcrumb-item', { 'breadcrumb-link': breadcrumbs.length > 0, 'breadcrumb-current': breadcrumbs.length === 0 }]"
            @click="breadcrumbs.length > 0 && $emit('go-root')"
        >
          <icon-home class="breadcrumb-icon" />
          <span>全部文件</span>
        </a-breadcrumb-item>
        <a-breadcrumb-item
            v-for="(item, index) in breadcrumbs"
            :key="item.id"
            :class="['breadcrumb-item', { 'breadcrumb-link': index !== breadcrumbs.length - 1, 'breadcrumb-current': index === breadcrumbs.length - 1 }]"
            @click="index !== breadcrumbs.length - 1 && $emit('go-breadcrumb', index)"
        >
          <icon-folder class="breadcrumb-icon" v-if="index !== breadcrumbs.length - 1" />
          <icon-folder class="breadcrumb-icon" v-else style="color: rgb(var(--primary-6));" />
          <span>{{ item.name }}</span>
        </a-breadcrumb-item>
      </a-breadcrumb>
      <div v-else class="page-title">{{ title }}</div>
    </div>
    <div class="header-right">
      <a-space v-if="showSearch">
        <div class="search-switch-wrapper">
          <span :class="{ active: searchType === 'fuzzy' }" @click="handleToggleSearchType('fuzzy')">模糊搜索</span>
          <a-switch :model-value="isVectorSearch" size="small" @change="handleSearchTypeChange"/>
          <span :class="{ active: searchType === 'vector' }" @click="handleToggleSearchType('vector')">智能搜索</span>
        </div>
        <a-input-search
            :model-value="searchKey"
            :style="{ width: '280px' }"
            allow-clear
            placeholder="搜索文件"
            search-button
            @clear="$emit('search-clear')"
            @search="$emit('search')"
            @update:model-value="$emit('update:searchKey', $event)"
            @press-enter="$emit('search')"
        />
      </a-space>

      <a-button type="text" @click="$emit('go-docs')" style="color: var(--color-text-2); display: flex; align-items: center; gap: 4px;">
        <template #icon><icon-book /></template>
        项目文档
      </a-button>

      <a-dropdown trigger="click">
        <div class="user-info">
          <span class="username">{{ userInfo.username || '未登录' }}</span>
          <div
              :style="{
                backgroundColor: userInfo.avatar ? 'transparent' : '#165dff',
                backgroundImage: userInfo.avatar ? `url(${userInfo.avatar})` : 'none'
              }"
              class="custom-avatar"
          >
            <icon-user v-if="!userInfo.avatar" style="font-size: 18px; color: #fff;"/>
          </div>
        </div>
        <template #content>
          <a-doption @click="$emit('click-mcp-token')">MCP Token</a-doption>
          <a-doption @click="$emit('click-avatar')">修改头像</a-doption>
          <a-doption @click="$emit('click-password')">修改密码</a-doption>
          <a-doption @click="$emit('logout')">退出登录</a-doption>
        </template>
      </a-dropdown>
    </div>
  </a-layout-header>
</template>

<script lang="ts" setup>
import {IconUser, IconBook, IconHome, IconFolder} from '@arco-design/web-vue/es/icon'

interface UserInfo {
  id: number | null
  username: string
  avatar: string
}

// 注：searchType / isVectorSearch 默认值对齐 useFileBrowser 的初始值（'vector' / true），
// 避免 HomeView 首屏渲染时父级状态尚未回灌导致 FileHeader 短暂显示「模糊搜索」高亮，
// 与实际请求走 vector 检索不一致的视觉错位。
withDefaults(defineProps<{
  breadcrumbs?: { id: number; name: string }[]
  searchKey?: string
  searchType?: 'fuzzy' | 'vector'
  isVectorSearch?: boolean
  userInfo: UserInfo
  showSearch?: boolean
  showBreadcrumbs?: boolean
  title?: string
}>(), {
  breadcrumbs: () => [],
  searchKey: '',
  searchType: 'vector',
  isVectorSearch: true,
  showSearch: false,
  showBreadcrumbs: false,
  title: ''
})

const emit = defineEmits([
  'go-root',
  'go-breadcrumb',
  'update:searchKey',
  'search',
  'search-clear',
  'go-docs',
  'logout',
  'click-avatar',
  'click-password',
  'click-mcp-token',
  'update:searchType',
  'update:isVectorSearch'
])

const handleSearchTypeChange = (val: boolean | string | number) => {
  const type = val ? 'vector' : 'fuzzy'
  emit('update:searchType', type)
  emit('update:isVectorSearch', !!val)
}

const handleToggleSearchType = (type: 'fuzzy' | 'vector') => {
  emit('update:searchType', type)
  emit('update:isVectorSearch', type === 'vector')
}
</script>

<style scoped>
.header {
  height: 64px;
  background-color: #fff;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  border-bottom: 1px solid var(--color-border);
}

.header-right {
  display: flex;
  align-items: center;
  gap: 20px;
}

.search-switch-wrapper {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  color: var(--color-text-2);
}

.search-switch-wrapper span {
  cursor: pointer;
  transition: color 0.2s;
}

.search-switch-wrapper span.active {
  color: rgb(var(--primary-6));
  font-weight: 500;
}

.custom-breadcrumb {
  font-size: 15px;
  display: flex;
  align-items: center;
}

:deep(.arco-breadcrumb-item) {
  display: inline-flex;
  align-items: center;
}

.breadcrumb-item {
  display: inline-flex;
  align-items: center;
  transition: all 0.2s ease;
  padding: 4px 8px;
  border-radius: 4px;
}

.breadcrumb-icon {
  margin-right: 4px;
  font-size: 16px;
  transform: translateY(-1px);
}

.breadcrumb-link {
  cursor: pointer;
  color: var(--color-text-3);
}

.breadcrumb-link:hover {
  color: rgb(var(--primary-6));
  background-color: var(--color-fill-2);
}

.breadcrumb-current {
  font-weight: 600;
  color: var(--color-text-1);
}

.page-title {
  font-size: 16px;
  font-weight: 500;
  color: var(--color-text-1);
}

.user-info {
  display: flex;
  align-items: center;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 4px;
  transition: all 0.2s;
}

.user-info:hover {
  background-color: var(--color-fill-3);
}

.username {
  margin-right: 12px;
  color: var(--color-text-1);
  font-weight: 500;
  font-size: 14px;
}

.custom-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background-size: cover;
  background-position: center;
  background-repeat: no-repeat;
  border: 1px solid var(--color-border-2);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  flex-shrink: 0;
}
</style>
