<template>
  <div class="main-container">
    <a-layout class="layout-demo">
      <SideBar :active-menu="activeMenu" @menu-click="handleMenuClick" />
      <a-layout>
        <FileHeader
          :title="title"
          :user-info="userInfo"
          v-bind="$attrs"
          @logout="handleLogout"
          @click-avatar="showAvatarModal = true"
          @click-password="showPasswordModal = true"
          @click-mcp-token="openMcpTokenModal"
          @go-docs="handleMenuClick('docs')"
        />
        <a-layout-content class="content">
          <slot></slot>
        </a-layout-content>
      </a-layout>
    </a-layout>

    <!-- AI 聊天悬浮球，仅在“全部文件”页面显示 -->
    <ChatWidget :show="activeMenu === 'all'" />

    <!-- MCP Token：每用户唯一，可复制 / 刷新 -->
    <a-modal v-model:visible="showMcpTokenModal" title="MCP Token" :footer="false" :width="560" unmount-on-close>
      <a-spin :loading="mcpLoading" style="width: 100%">
        <p class="mcp-hint">
          每个账号自动配置唯一 Token，用于 AI 客户端与工作区连接云盘。工作区启动时会自动注入，无需手动配置。
        </p>
        <div class="mcp-endpoint">
          <span class="mcp-label">服务地址</span>
          <code>{{ mcpEndpoint }}</code>
        </div>
        <div class="mcp-token-box">
          <span class="mcp-label">Token</span>
          <code class="mcp-token-value">{{ mcpToken || '加载中…' }}</code>
        </div>
        <div v-if="mcpExpiresAt" class="mcp-meta">过期时间：{{ formatDate(mcpExpiresAt) }}</div>
        <div class="mcp-actions">
          <a-button type="primary" :disabled="!mcpToken" @click="handleCopyMcpToken"> 复制 Token </a-button>
          <a-button type="outline" status="warning" :loading="mcpRefreshing" @click="handleRefreshMcpToken">
            刷新 Token
          </a-button>
        </div>
        <a-alert type="warning" style="margin-top: 16px">
          刷新后旧 Token 立即失效；运行中的工作区会自动同步新 Token。请勿泄露给他人。
        </a-alert>
      </a-spin>
    </a-modal>

    <!-- 头像上传裁切弹窗 -->
    <a-modal v-model:visible="showAvatarModal" title="修改头像" @cancel="handleCancelAvatar" @ok="handleUploadAvatar">
      <div class="avatar-upload-container">
        <div v-if="!imgSrc" class="upload-trigger">
          <a-upload :auto-upload="false" :show-file-list="false" @change="onFileChange">
            <template #upload-button>
              <div class="upload-box">
                <icon-plus />
                <div>选择图片</div>
              </div>
            </template>
          </a-upload>
        </div>
        <div v-else class="cropper-wrapper">
          <vue-cropper
            ref="cropper"
            :autoCrop="true"
            :autoCropHeight="200"
            :autoCropWidth="200"
            :canMove="true"
            :canMoveBox="true"
            :centerBox="true"
            :fixed="true"
            :fixedBox="false"
            :fixedNumber="[1, 1]"
            :full="true"
            :img="imgSrc"
            :info="false"
            :infoTrue="true"
            :original="false"
            :outputSize="1"
            class="circle-cropper"
            outputType="png"
          ></vue-cropper>
          <div class="cropper-ops">
            <a-button size="mini" @click="imgSrc = ''">重新选择</a-button>
          </div>
        </div>
      </div>
    </a-modal>

    <!-- 修改密码弹窗 -->
    <a-modal
      v-model:visible="showPasswordModal"
      title="修改密码"
      @cancel="handleCancelPassword"
      @ok="handleUpdatePassword"
    >
      <a-form :model="passwordForm" layout="vertical">
        <a-form-item field="oldPassword" label="旧密码" required>
          <a-input-password v-model="passwordForm.oldPassword" placeholder="请输入旧密码" />
        </a-form-item>
        <a-form-item field="newPassword" label="新密码" required>
          <a-input-password v-model="passwordForm.newPassword" placeholder="请输入新密码" />
        </a-form-item>
        <a-form-item field="confirmPassword" label="确认新密码" required>
          <a-input-password v-model="passwordForm.confirmPassword" placeholder="请再次输入新密码" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Message, Modal } from '@arco-design/web-vue'
import type { FileItem as ArcoFileItem } from '@arco-design/web-vue'
import { IconPlus } from '@arco-design/web-vue/es/icon'
import 'vue-cropper/dist/index.css'
import { VueCropper } from 'vue-cropper'
import SideBar from './SideBar.vue'
import FileHeader from './FileHeader.vue'
import ChatWidget from './ChatWidget.vue'
import { getUserInfo, updatePassword, uploadAvatar } from '@/api/user'
import { getMcpToken, refreshMcpToken } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'
import { logger } from '@/utils/logger'
import { passwordChangeSchema } from '@/schemas/user'
import { copyText } from '@/utils/clipboard'
import { formatDate } from '@/utils/format'

defineProps<{
  activeMenu: string
  title?: string
}>()

const router = useRouter()
const auth = useAuthStore()
const userInfo = ref({
  id: auth.user.id,
  username: auth.user.username,
  avatar: auth.user.avatar || '',
})

const showAvatarModal = ref(false)
const imgSrc = ref('')
const cropper = ref()

const showPasswordModal = ref(false)
const passwordForm = reactive({
  oldPassword: '',
  newPassword: '',
  confirmPassword: '',
})

// MCP Token 每用户唯一，刷新会使旧凭证立即失效
const showMcpTokenModal = ref(false)
const mcpLoading = ref(false)
const mcpRefreshing = ref(false)
const mcpToken = ref('')
const mcpExpiresAt = ref<string | null>(null)
const mcpEndpoint = computed(() => {
  const { hostname, protocol } = window.location
  return `${protocol}//${hostname}:5001/mcp`
})

const openMcpTokenModal = async () => {
  showMcpTokenModal.value = true
  mcpLoading.value = true
  try {
    const res = await getMcpToken()
    mcpToken.value = res.mcp_token
    mcpExpiresAt.value = res.token?.expires_at ?? null
  } catch (error) {
    logger.warn('加载 MCP Token 失败', error)
  } finally {
    mcpLoading.value = false
  }
}

const handleCopyMcpToken = async () => {
  if (!mcpToken.value) return
  const ok = await copyText(mcpToken.value)
  if (ok) {
    Message.success('Token 已复制到剪贴板')
  } else {
    Message.warning('复制失败，请手动复制')
  }
}

const handleRefreshMcpToken = () => {
  Modal.confirm({
    title: '刷新 MCP Token',
    content: '刷新后旧 Token 将立即失效，使用旧 Token 的外部客户端需重新配置。运行中的工作区会自动同步。',
    okText: '刷新',
    okButtonProps: { status: 'warning' },
    onOk: async () => {
      mcpRefreshing.value = true
      try {
        const res = await refreshMcpToken()
        mcpToken.value = res.mcp_token
        mcpExpiresAt.value = res.token?.expires_at ?? null
        Message.success('MCP Token 已刷新')
      } catch (error) {
        logger.warn('刷新 MCP Token 失败', error)
      } finally {
        mcpRefreshing.value = false
      }
    },
  })
}

const initUserInfo = async () => {
  const userId = auth.user.id
  if (!userId) return
  // 先用本地缓存的 username/avatar 占位，避免头像加载前空白
  userInfo.value = {
    id: userId,
    username: auth.user.username || '',
    avatar: auth.user.avatar || '',
  }
  try {
    // 新 API：getUserInfo 已解包为 UserProfile，无需再读 res.data
    const data = await getUserInfo(userId)
    if (data && data.username) {
      userInfo.value = {
        id: data.id,
        username: data.username,
        avatar: data.avatar || '',
      }
      auth.setUser({
        id: data.id,
        username: data.username,
        avatar: data.avatar || '',
      })
    }
  } catch (apiError) {
    // 拦截器已弹唯一 Message.error，这里仅日志降级 + 吞异常，避免污染后续渲染
    logger.warn('获取用户信息失败', apiError)
  }
}

const onFileChange = (fileList: ArcoFileItem[]) => {
  const file = fileList[0]?.file
  if (!file) return
  const reader = new FileReader()
  reader.onload = (e: ProgressEvent<FileReader>) => {
    imgSrc.value = e.target?.result as string
  }
  reader.readAsDataURL(file)
}

const handleCancelAvatar = () => {
  imgSrc.value = ''
  showAvatarModal.value = false
}

const handleUploadAvatar = () => {
  if (!userInfo.value.id) return

  cropper.value.getCropBlob(async (blob: Blob) => {
    const formData = new FormData()
    formData.append('avatar', blob, 'avatar.png')

    try {
      // 新 API：uploadAvatar 已解包为 UploadAvatarResult（含 avatar 或 url 字段）
      const data = await uploadAvatar(userInfo.value.id!, formData)
      Message.success('头像更新成功')

      // 追加时间戳以刷新图片缓存后，再更新本地状态。
      const url = data.avatar || data.url
      let newAvatarUrl = url
      if (url && typeof url === 'string' && !url.startsWith('data:')) {
        newAvatarUrl = url + '?t=' + new Date().getTime()
      }

      userInfo.value.avatar = newAvatarUrl || ''
      auth.setUser({ avatar: newAvatarUrl || '' })

      handleCancelAvatar()
    } catch (error) {
      // 拦截器已弹唯一 Message.error，这里仅日志降级
      logger.warn('头像上传失败', error)
    }
  })
}

const handleCancelPassword = () => {
  passwordForm.oldPassword = ''
  passwordForm.newPassword = ''
  passwordForm.confirmPassword = ''
  showPasswordModal.value = false
}

const handleUpdatePassword = async () => {
  if (!userInfo.value.id) return
  // Zod 校验：旧密码非空、新密码长度 >=6、两次新密码一致
  const result = passwordChangeSchema.safeParse({
    oldPassword: passwordForm.oldPassword,
    newPassword: passwordForm.newPassword,
    confirmPassword: passwordForm.confirmPassword,
  })
  if (!result.success) {
    Message.warning(result.error.issues[0]?.message ?? '请填写完整信息')
    return
  }

  try {
    // 新 API：updatePassword 入参为 PasswordChangeInput（camelCase），内部映射 snake_case
    await updatePassword(userInfo.value.id, {
      oldPassword: passwordForm.oldPassword,
      newPassword: passwordForm.newPassword,
      confirmPassword: passwordForm.confirmPassword,
    })
    Message.success('密码修改成功，请重新登录')
    handleLogout()
  } catch (error) {
    // 拦截器已弹唯一 Message.error，这里仅日志降级 + 吞异常
    logger.warn('密码修改失败', error)
  }
}

onMounted(() => {
  initUserInfo()
})

// 侧栏菜单 key 与路由的映射表，替代冗长的 if/else-if 链
const MENU_ROUTE_MAP: Record<string, string> = {
  all: '/home',
  share: '/shares',
  inbox: '/inbox',
  docs: '/docs',
  workspace: '/workspace',
  'token-usage': '/token-usage',
  'admin-token-usage': '/admin/token-usage',
  'sys-dicts': '/sys_dicts',
}

const handleMenuClick = (key: string) => {
  const target = MENU_ROUTE_MAP[key]
  if (target) {
    router.push(target)
  }
}

const handleLogout = () => {
  auth.logout()
  router.push('/')
}
</script>

<style scoped>
.main-container {
  height: 100vh;
  background-color: var(--color-fill-2);
}

.layout-demo {
  height: 100%;
}

.content {
  padding: 20px;
  background-color: #fff;
  margin: 20px;
  border-radius: 4px;
  position: relative;
  overflow: auto;
}

.avatar-upload-container {
  width: 100%;
  height: 300px;
  display: flex;
  justify-content: center;
  align-items: center;
  background-color: var(--color-fill-1);
}

.upload-box {
  width: 120px;
  height: 120px;
  border: 1px dashed var(--color-border-3);
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  cursor: pointer;
  transition: all 0.2s;
}

.upload-box:hover {
  border-color: var(--color-primary-light-4);
  color: var(--color-primary-light-4);
}

.cropper-wrapper {
  width: 100%;
  height: 100%;
  position: relative;
}

.cropper-ops {
  position: absolute;
  bottom: 10px;
  right: 10px;
  z-index: 10;
}

/* 让裁切框看起来是圆形的 */
:deep(.circle-cropper .cropper-view-box) {
  border-radius: 50%;
}

:deep(.circle-cropper .cropper-face) {
  border-radius: 50%;
}

.mcp-hint {
  margin: 0 0 16px;
  font-size: 14px;
  color: var(--color-text-2);
  line-height: 1.6;
}

.mcp-endpoint,
.mcp-token-box {
  margin-bottom: 12px;
}

.mcp-label {
  display: block;
  font-size: 13px;
  color: var(--color-text-3);
  margin-bottom: 6px;
}

.mcp-token-value {
  display: block;
  font-family: 'JetBrains Mono', Monaco, monospace;
  font-size: 12px;
  line-height: 1.6;
  word-break: break-all;
  background: var(--color-fill-2);
  padding: 10px 12px;
  border-radius: 4px;
  max-height: 120px;
  overflow-y: auto;
  color: var(--color-text-1);
}

.mcp-meta {
  font-size: 13px;
  color: var(--color-text-3);
  margin-bottom: 16px;
}

.mcp-actions {
  display: flex;
  gap: 12px;
}
</style>
