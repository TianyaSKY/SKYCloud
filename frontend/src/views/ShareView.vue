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

const route = useRoute()
const loading = ref(false)
const token = route.params.token as string

const handleDownload = async () => {
  loading.value = true
  try {
    // 直接通过浏览器跳转或者使用 axios 下载
    // 由于后端 GET /share/{token} 直接返回文件，我们可以直接 window.open 或创建 a 标签
    const downloadUrl = `/api/share/${token}`

    // 为了更好的体验，我们尝试用 blob 下载以处理错误
    const response = await axios.get(downloadUrl, {
      responseType: 'blob'
    })

    const contentDisposition = response.headers['content-disposition']
    let fileName = 'downloaded_file'
    if (contentDisposition) {
      const fileNameMatch = contentDisposition.match(/filename="?(.+)"?/)
      if (fileNameMatch && fileNameMatch.length === 2) {
        fileName = decodeURIComponent(fileNameMatch[1])
      }
    }

    const url = window.URL.createObjectURL(new Blob([response.data]))
    const link = document.createElement('a')
    link.href = url
    link.setAttribute('download', fileName)
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.URL.revokeObjectURL(url)

    Message.success('开始下载')
  } catch (error) {
    console.error('Download error:', error)
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
