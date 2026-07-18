<script lang="ts" setup>
import { reactive, ref } from 'vue'
import { Message } from '@arco-design/web-vue'
import { IconLock, IconUser } from '@arco-design/web-vue/es/icon'
import { useRouter } from 'vue-router'
import { login, register } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'
import { loginSchema, registerSchema } from '@/schemas/auth'
import { logger } from '@/utils/logger'

const router = useRouter()
const auth = useAuthStore()
const isLogin = ref(true)
const loading = ref(false)

const form = reactive({
  username: '',
  password: '',
  confirmPassword: '',
})

const switchMode = () => {
  isLogin.value = !isLogin.value
  form.username = ''
  form.password = ''
  form.confirmPassword = ''
}

interface LoginFormSubmitPayload {
  values: { username: string; password: string; confirmPassword: string }
  errors: unknown
}

const handleSubmit = async ({ values, errors }: LoginFormSubmitPayload) => {
  // 防回车/连点二次提交：loading 期间直接忽略，避免重复登录或并发注册
  if (loading.value) return
  if (errors) return

  // Zod 整体校验：登录只校验非空/长度；注册额外校验密码长度与二次一致
  const schema = isLogin.value ? loginSchema : registerSchema
  const result = schema.safeParse({
    username: values.username,
    password: values.password,
    confirmPassword: values.confirmPassword,
  })
  if (!result.success) {
    Message.error(result.error.issues[0]?.message ?? '表单校验失败')
    return
  }

  loading.value = true
  try {
    if (isLogin.value) {
      const res = await login({
        username: values.username,
        password: values.password,
      })
      auth.setAuth(res.token, {
        id: res.user_id,
        role: res.role,
        username: values.username, // 登录时输入的用户名先存起来，后续 getUserInfo 会刷新
      })
      Message.success('登录成功')
      await router.push('/home')
    } else {
      // api 层 register(data: RegisterInput) 接收完整表单，内部仅发 username+password
      await register({
        username: values.username,
        password: values.password,
        confirmPassword: values.confirmPassword,
      })
      Message.success('注册成功，请登录')
      isLogin.value = true
    }
  } catch (error) {
    // 拦截器已弹唯一错误提示，此处只记日志并吞异常
    logger.warn('登录/注册失败 isLogin={} error={}', isLogin.value, error)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="brand-section">
      <div class="brand-content">
        <h1 class="brand-title">SKYCloud</h1>
        <p class="brand-slogan">开源 智能</p>
        <div class="brand-decoration"></div>
      </div>
    </div>

    <div class="form-section">
      <div class="login-card">
        <div class="login-header">
          <h2 class="title">{{ isLogin ? '账号登录' : '新用户注册' }}</h2>
          <p class="subtitle">{{ isLogin ? '高效、安全的AI云端文件管理' : '加入我们，开启云端之旅' }}</p>
        </div>

        <a-form :model="form" layout="vertical" @submit="handleSubmit">
          <a-form-item :rules="[{ required: true, message: '请输入用户名' }]" field="username" label="用户名">
            <a-input v-model="form.username" placeholder="请输入用户名" size="large">
              <template #prefix>
                <icon-user />
              </template>
            </a-input>
          </a-form-item>

          <a-form-item :rules="[{ required: true, message: '请输入密码' }]" field="password" label="密码">
            <a-input-password v-model="form.password" placeholder="请输入密码" size="large">
              <template #prefix>
                <icon-lock />
              </template>
            </a-input-password>
          </a-form-item>

          <a-form-item
            v-if="!isLogin"
            :rules="[{ required: true, message: '请再次输入密码' }]"
            field="confirmPassword"
            label="确认密码"
          >
            <a-input-password v-model="form.confirmPassword" placeholder="请再次输入密码" size="large">
              <template #prefix>
                <icon-lock />
              </template>
            </a-input-password>
          </a-form-item>

          <a-button :loading="loading" class="login-button" html-type="submit" long size="large" type="primary">
            {{ isLogin ? '立即登录' : '立即注册' }}
          </a-button>
        </a-form>

        <div class="login-footer">
          <p>
            {{ isLogin ? '还没有账号？' : '已有账号？' }}
            <a-link @click="switchMode">{{ isLogin ? '立即注册' : '返回登录' }}</a-link>
          </p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  height: 100vh;
  width: 100vw;
  overflow: hidden;
  background: url('@/assets/images/login-bg.jpg') no-repeat center center;
  background-size: cover;
}

/* 左侧样式 */
.brand-section {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: flex-start;
  padding: 60px 60px 60px 120px;
  background: transparent;
}

.brand-content {
  color: white;
  /* 浅色背景图上提高品牌文案对比度 */
  text-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
}

.brand-title {
  font-size: 64px;
  font-weight: 800;
  margin: 0;
  letter-spacing: 4px;
  font-family: 'PingFang SC', 'Microsoft YaHei', sans-serif;
}

.brand-slogan {
  font-size: 24px;
  margin-top: 10px;
  opacity: 0.9;
  letter-spacing: 2px;
}

.brand-decoration {
  width: 60px;
  height: 4px;
  background: #fff;
  margin-top: 20px;
}

.form-section {
  width: 500px;
  display: flex;
  align-items: center;
  justify-content: center;
  /* 毛玻璃面板：半透明 + 模糊，露出底层背景图 */
  background: rgba(255, 255, 255, 0.6);
  backdrop-filter: blur(5px);
  -webkit-backdrop-filter: blur(20px);
  border-left: 1px solid rgba(255, 255, 255, 0.2);
  box-shadow: -10px 0 30px rgba(0, 0, 0, 0.1);
}

.login-card {
  width: 360px;
  padding: 40px;
  background: transparent;
}

.login-header {
  margin-bottom: 32px;
}

.title {
  font-size: 24px;
  font-weight: 600;
  color: #1d2129;
  margin: 0;
}

.subtitle {
  font-size: 14px;
  color: #4e5969;
  margin-top: 8px;
}

.login-button {
  height: 44px;
  font-size: 16px;
  border-radius: 6px;
  margin-top: 8px;
}

.login-footer {
  margin-top: 24px;
  text-align: center;
  font-size: 14px;
  color: #4e5969;
}

/* 响应式适配 */
@media (max-width: 900px) {
  .brand-section {
    display: none;
  }

  .form-section {
    width: 100%;
    background: rgba(255, 255, 255, 0.8);
  }
}
</style>
