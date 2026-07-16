<template>
  <div class="markdown-body" v-html="renderedHtml"></div>
</template>

<script lang="ts" setup>
import {computed} from 'vue';
import {marked, type Tokens} from 'marked';
import DOMPurify from 'dompurify';
import {logger} from '@/utils/logger';

const props = defineProps<{
  content: string;
}>();

/**
 * 为每次渲染单独构造 marked renderer，避免模块级单例 renderer.image 被多实例共享
 * （聊天消息每条都是独立 MarkdownRenderer 实例）造成 token.href 串改/竞态。
 * 返回的 renderer 仅覆盖 image，其它渲染保持 marked 默认行为。
 */
const buildRenderer = () => {
  const renderer = new marked.Renderer();
  const originalImage = renderer.image.bind(renderer);

  renderer.image = (token: Tokens.Image) => {
    if (token.href && token.href.includes('/api/file')) {
      // 兼容纠错：
      // 情况 1: AI 可能写成 /api/file/download/1 -> /api/files/1/download
      // 情况 2: AI 可能写成 /api/files/download/1 -> /api/files/1/download
      // 我们的后端路由是 /api/files/{id}/download

      // 安全说明：img 标签无法附加 Authorization header，仍需通过 query 传 token。
      // TODO(安全)：后续应改为后端签发短时下载 URL，避免 JWT 落入日志/Referer。
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

  return renderer;
};

const renderedHtml = computed(() => {
  if (!props.content) return '';

  // 过滤 AI 特殊标记
  const cleanContent = props.content.replace(/<\|begin_of_box\|>|<\|end_of_box\|>|<\|thought\|>|<\/thought>/g, '');

  let rawHtml: string;
  try {
    // 每次渲染构建独立 renderer，并通过 options 传入 breaks/gfm（与原模块级 setOptions 等价），
    // 避免模块顶层副作用影响其他实例。
    rawHtml = marked.parse(cleanContent, {
      breaks: true,
      gfm: true,
      renderer: buildRenderer(),
    }) as string;
  } catch (err) {
    // 畸形 markdown 不应冒泡到 ErrorBoundary 整页替换；回退为原始文本（已过特殊标记过滤）。
    logger.warn('markdown 解析失败，回退为原始文本', err);
    return DOMPurify.sanitize(cleanContent, {ADD_ATTR: ['target']});
  }
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
