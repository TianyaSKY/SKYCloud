<template>
  <div class="markdown-body" v-html="renderedHtml"></div>
</template>

<script lang="ts" setup>
import {computed} from 'vue';
import {marked, type Tokens} from 'marked';
import DOMPurify from 'dompurify';

const props = defineProps<{
  content: string;
}>();

// 配置 Markdown 渲染器
marked.setOptions({
  breaks: true,      // 支持回车换行
  gfm: true,         // 启用 GitHub Flavored Markdown
});

const renderer = new marked.Renderer();
const originalImage = renderer.image.bind(renderer);

renderer.image = (token: Tokens.Image) => {
  if (token.href && token.href.includes('/api/file')) {
    // 兼容 AI 可能生成的两种历史下载路径，统一为后端实际路由。

    // 安全说明：img 标签无法附加 Authorization header，仍需通过 query 传 token。
    // TODO(安全)：后续应改为后端签发短时下载 URL，避免 JWT 落入日志/Referer。
    let normalizedHref = token.href;
    const downloadMatch = normalizedHref.match(/\/download\/(\d+)/);
    if (downloadMatch) {
      const fileId = downloadMatch[1];
      normalizedHref = `/api/files/${fileId}/download`;
    } else if (!normalizedHref.includes('/api/files/')) {
       // 统一单数路径为复数路径。
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

  // 过滤 AI 特殊标记。
  const cleanContent = props.content.replace(/<\|begin_of_box\|>|<\|end_of_box\|>|<\|thought\|>|<\/thought>/g, '');

  const rawHtml = marked.parse(cleanContent, {renderer}) as string;
  // 防御 XSS：marked 默认不过滤 HTML，AI 输出与用户上传 markdown 均经过此组件渲染，
  // 必须用 DOMPurify 净化后再 v-html，阻断存储型/反射型 XSS。
  return DOMPurify.sanitize(rawHtml, {ADD_ATTR: ['target']});
});
</script>

<style scoped>
.markdown-body {
  font-size: 14px;
  line-height: 1.6;
  word-break: break-word;
}

/* Markdown 基础样式 */
.markdown-body :deep(p) {
  margin: 0 0 8px 0;
}

.markdown-body :deep(p:last-child) {
  margin-bottom: 0;
}

/* Markdown 图片样式 */
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
