<template>
  <div class="share-container">
    <div class="share-card">
      <div class="icon-wrapper">
        <icon-file :style="{ fontSize: '64px', color: '#165dff' }"/>
      </div>
      <h2 class="file-name">文件分享</h2>
      <p class="share-tip">您正在访问一个公开分享的文件</p>
      <div class="actions">
        <a-button :loading="loading" size="large" type="primary" @click="handleDownload">
          <template #icon>
            <icon-download/>
          </template>
          立即下载
        </a-button>
      </div>
    </div>
  </div>
</template>

<script lang="ts" setup>
import {ref} from 'vue'
import {useRoute} from 'vue-router'
import {Message} from '@arco-design/web-vue'
import {IconDownload, IconFile} from '@arco-design/web-vue/es/icon'
import axios from 'axios'
import {logger} from '@/utils/logger'
import {safeRevokeObjectURL} from '@/utils/blob'

const route = useRoute()
const loading = ref(false)
const token = route.params.token as string

const DEFAULT_FILE_NAME = 'downloaded_file'

const handleDownload = async () => {
  loading.value = true
  try {
    const downloadUrl = `/api/share/${token}`

    // 保留裸 axios 以拿到完整响应头：share token 下载响应的 content-disposition 携带原始文件名，
    // 若改走 request 实例，响应拦截器 (response) => response.data 会剥离为 Blob，丢失文件名解析所需的头；
    // 同时 request.ts 的 module 扩展把 axios.get 限定为单类型参数签名，
    // 故这里用未受扩展影响的 axios.request（继承自 Axios 类，3 类型参数 + R 默认 AxiosResponse<T>）。
    // 公开分享下载无需鉴权，不依赖 request 实例的 Authorization 注入。
    const response = await axios.request<Blob>({
      url: downloadUrl,
      method: 'GET',
      responseType: 'blob',
    })

    let fileName = DEFAULT_FILE_NAME
    const contentDisposition = response.headers['content-disposition']
    if (contentDisposition) {
      const fileNameMatch = contentDisposition.match(/filename="?(.+)"?/)
      if (fileNameMatch && fileNameMatch.length === 2) {
        const rawName = fileNameMatch[1]
        try {
          // content-disposition 含非编码字符（如直接的中文名）会抛 URIError，需包裹
          fileName = decodeURIComponent(rawName) || DEFAULT_FILE_NAME
        } catch (err) {
          logger.warn('解析分享文件名失败，回退默认名 rawName={} err={}', rawName, err)
          fileName = rawName || DEFAULT_FILE_NAME
        }
      }
    }

    const url = window.URL.createObjectURL(new Blob([response.data]))
    const link = document.createElement('a')
    link.href = url
    link.setAttribute('download', fileName)
    document.body.appendChild(link)
    link.click()
    link.remove()
    safeRevokeObjectURL(url)

    Message.success('开始下载')
  } catch (error) {
    // 裸 axios 不走 request 实例的响应拦截器，需自行向用户提示
    logger.warn('分享下载失败 token={} err={}', token, error)
    Message.error('下载失败，分享链接可能已失效')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.share-container {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: var(--color-fill-2);
}

.share-card {
  width: 400px;
  padding: 40px;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 4px 10px rgba(0, 0, 0, 0.1);
  text-align: center;
}

.icon-wrapper {
  margin-bottom: 20px;
}

.file-name {
  margin-bottom: 8px;
  font-size: 20px;
  color: var(--color-text-1);
}

.share-tip {
  color: var(--color-text-3);
  margin-bottom: 30px;
}

.actions {
  display: flex;
  justify-content: center;
}
</style>
