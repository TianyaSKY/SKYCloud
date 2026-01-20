<template>
  <div class="main-container">
    <a-layout class="layout-demo">
      <SideBar :active-menu="activeMenu" @menu-click="handleMenuClick"/>
      <a-layout>
        <FileHeader
            :title="title"
            :user-info="userInfo"
            v-bind="$attrs"
            @logout="handleLogout"
            @click-avatar="showAvatarModal = true"
            @click-password="showPasswordModal = true"
        />
        <a-layout-content class="content">
          <slot></slot>
        </a-layout-content>
      </a-layout>
    </a-layout>

    <!-- AI 聊天悬浮球，仅在“全部文件”页面显示 -->
    <ChatWidget :show="activeMenu === 'all'"/>

    <!-- 头像上传裁切弹窗 -->
    <a-modal v-model:visible="showAvatarModal" title="修改头像" @cancel="handleCancelAvatar" @ok="handleUploadAvatar">
      <div class="avatar-upload-container">
        <div v-if="!imgSrc" class="upload-trigger">
          <a-upload
              :auto-upload="false"
              :show-file-list="false"
              @change="onFileChange"
          >
            <template #upload-button>
              <div class="upload-box">
                <icon-plus/>
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
    <a-modal v-model:visible="showPasswordModal" title="修改密码" @cancel="handleCancelPassword"
             @ok="handleUpdatePassword">
      <a-form :model="passwordForm" layout="vertical">
        <a-form-item field="oldPassword" label="旧密码" required>
          <a-input-password v-model="passwordForm.oldPassword" placeholder="请输入旧密码"/>
        </a-form-item>
        <a-form-item field="newPassword" label="新密码" required>
          <a-input-password v-model="passwordForm.newPassword" placeholder="请输入新密码"/>
        </a-form-item>
        <a-form-item field="confirmPassword" label="确认新密码" required>
          <a-input-password v-model="passwordForm.confirmPassword" placeholder="请再次输入新密码"/>
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script lang="ts" setup>
import {onMounted, reactive, ref} from 'vue'
import {useRouter} from 'vue-router'
import {Message} from '@arco-design/web-vue'
import {IconPlus} from '@arco-design/web-vue/es/icon'
import 'vue-cropper/dist/index.css'
import {VueCropper} from "vue-cropper";
import SideBar from './SideBar.vue'
import FileHeader from './FileHeader.vue'
import ChatWidget from './ChatWidget.vue'
import {getUserInfo, updatePassword, uploadAvatar} from '@/api/user'

defineProps<{
  activeMenu: string
  title?: string
}>()

const router = useRouter()
const userInfo = ref({
  id: null,
  username: '',
  avatar: ''
})

// 头像相关
const showAvatarModal = ref(false)
const imgSrc = ref('')
const cropper = ref()

// 密码相关
const showPasswordModal = ref(false)
const passwordForm = reactive({
  oldPassword: '',
  newPassword: '',
  confirmPassword: ''
})

const initUserInfo = async () => {
  const userStr = localStorage.getItem('user')
  if (userStr) {
    try {
      const user = JSON.parse(userStr)
      userInfo.value = {
        id: user.id,
        username: user.username || '',
        avatar: user.avatar || ''
      }

      if (user.id) {
        try {
          const res: any = await getUserInfo(user.id)
          const data = res.data || res
          if (data && data.username) {
            userInfo.value = {
              id: data.id,
              username: data.username,
              avatar: data.avatar || ''
            }
            localStorage.setItem('user', JSON.stringify(data))
          }
        } catch (apiError) {
          console.error('Failed to fetch user info', apiError)
        }
      }
    } catch (e) {
      console.error('Failed to parse user info', e)
    }
  }
}

const onFileChange = (fileList: any) => {
  const file = fileList[0].file
  const reader = new FileReader()
  reader.onload = (e: any) => {
    imgSrc.value = e.target.result
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
      const res: any = await uploadAvatar(userInfo.value.id!, formData)
      const data = res.data || res
      Message.success('头像更新成功')

      // 更新本地状态
      // 强制添加时间戳以刷新图片缓存
      const url = data.avatar || data.url
      let newAvatarUrl = url
      if (url && typeof url === 'string' && !url.startsWith('data:')) {
        newAvatarUrl = url + '?t=' + new Date().getTime()
      }

      userInfo.value.avatar = newAvatarUrl

      const user = JSON.parse(localStorage.getItem('user') || '{}')
      user.avatar = newAvatarUrl
      localStorage.setItem('user', JSON.stringify(user))

      handleCancelAvatar()
    } catch (error) {
      Message.error('头像上传失败')
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
  if (!passwordForm.oldPassword || !passwordForm.newPassword || !passwordForm.confirmPassword) {
    Message.warning('请填写完整信息')
    return
  }
  if (passwordForm.newPassword !== passwordForm.confirmPassword) {
    Message.warning('两次输入的新密码不一致')
    return
  }

  try {
    await updatePassword(userInfo.value.id, {
      old_password: passwordForm.oldPassword,
      new_password: passwordForm.newPassword
    })
    Message.success('密码修改成功，请重新登录')
    handleLogout()
  } catch (error: any) {
    Message.error(error.response?.data?.message || '密码修改失败')
  }
}

onMounted(() => {
  initUserInfo()
})

const handleMenuClick = (key: string) => {
  if (key === 'all') {
    router.push('/home')
  } else if (key === 'share') {
    router.push('/shares')
  } else if (key === 'inbox') {
    router.push('/inbox')
  } else if (key === 'sys-dicts') {
    router.push('/sys_dicts')
  }
}

const handleLogout = () => {
  localStorage.removeItem('token')
  localStorage.removeItem('user')
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
</style>
