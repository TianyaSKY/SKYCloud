<template>
  <div class="markdown-body" v-html="renderedHtml"></div>
</template>

<script lang="ts" setup>
import {computed} from 'vue';
import {marked} from 'marked';

const props = defineProps<{
  content: string;
}>();

// 配置 marked
marked.setOptions({
  breaks: true,      // 支持回车换行
  gfm: true,         // 启用 GitHub Flavored Markdown
});

const renderer = new marked.Renderer();
const originalImage = renderer.image.bind(renderer);

renderer.image = (token: any) => {
  if (token.href && token.href.includes('/api/file')) {
    // 兼容纠错：
    // 情况 1: AI 可能写成 /api/file/download/1 -> /api/files/1/download
    // 情况 2: AI 可能写成 /api/files/download/1 -> /api/files/1/download
    // 我们的后端路由是 /api/files/{id}/download
    
    let normalizedHref = token.href;
    const downloadMatch = normalizedHref.match(/\/download\/(\d+)/);
    if (downloadMatch) {
      const fileId = downloadMatch[1];
      normalizedHref = `/api/files/${fileId}/download`;
    } else if (!normalizedHref.includes('/api/files/')) {
       // 转换 /api/file/ 为 /api/files/
       normalizedHref = normalizedHref.replace('/api/file/', '/api/files/');
    }

    const authToken = localStorage.getItem('token');
    if (authToken) {
      normalizedHref = `${normalizedHref}${normalizedHref.includes('?') ? '&' : '?'}token=${authToken}`;
    }
    token.href = normalizedHref;
  }
  return originalImage(token);
};

const renderedHtml = computed(() => {
  if (!props.content) return '';

  // 过滤 AI 特殊标记
  const cleanContent = props.content.replace(/<\|begin_of_box\|>|<\|end_of_box\|>|<\|thought\|>|<\/thought>/g, '');

  return marked.parse(cleanContent, {renderer});
});
</script>

<style scoped>
.markdown-body {
  font-size: 14px;
  line-height: 1.6;
  word-break: break-word;
}

/* 基础 Markdown 样式 */
.markdown-body :deep(p) {
  margin: 0 0 8px 0;
}

.markdown-body :deep(p:last-child) {
  margin-bottom: 0;
}

/* 图片样式优化 */
.markdown-body :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 4px;
  margin: 8px 0;
  display: block;
  cursor: zoom-in;
  border: 1px solid var(--color-border);
}

.markdown-body :deep(pre) {
  background-color: var(--color-fill-3);
  padding: 12px;
  border-radius: 6px;
  overflow-x: auto;
  margin: 8px 0;
  border: 1px solid var(--color-border);
}

.markdown-body :deep(code) {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  background-color: var(--color-fill-3);
  padding: 2px 4px;
  border-radius: 4px;
  font-size: 90%;
}

.markdown-body :deep(pre code) {
  padding: 0;
  background-color: transparent;
  border-radius: 0;
  display: block;
}

.markdown-body :deep(ul), .markdown-body :deep(ol) {
  padding-left: 20px;
  margin: 8px 0;
}

.markdown-body :deep(blockquote) {
  margin: 8px 0;
  padding-left: 12px;
  border-left: 4px solid var(--color-border);
  color: var(--color-text-3);
}

.markdown-body :deep(a) {
  color: rgb(var(--arcoblue-6));
  text-decoration: none;
}

.markdown-body :deep(a:hover) {
  text-decoration: underline;
}

.markdown-body :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 8px 0;
}

.markdown-body :deep(th), .markdown-body :deep(td) {
  border: 1px solid var(--color-border);
  padding: 6px 12px;
}

.markdown-body :deep(th) {
  background-color: var(--color-fill-2);
}
</style>
