<template>
  <div ref="bodyElement" class="markdown-body" v-html="renderedHtml" @click="downloadLink"></div>
</template>

<script lang="ts" setup>
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { downloadFileBlob } from '../api/file'
import { downloadAttachment, protectedFileId } from '../utils/fileDownload'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { logger } from '@/utils/logger'

const props = defineProps<{
  content: string
}>()

const PLACEHOLDER = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'

/** Normalize both Markdown and raw HTML file references before insertion. */
function protectedMarkup(html: string) {
  const template = document.createElement('template')
  template.innerHTML = html
  for (const element of template.content.querySelectorAll<HTMLElement>('img, a')) {
    element.removeAttribute('data-protected-file')
    const attribute = element.tagName === 'IMG' ? 'src' : 'href'
    const id = protectedFileId(element.getAttribute(attribute) || '')
    if (id === null) continue
    element.dataset.protectedFile = String(id)
    element.setAttribute(attribute, attribute === 'src' ? PLACEHOLDER : `/api/files/${id}/download`)
  }
  return template.innerHTML
}

const renderedHtml = computed(() => {
  if (!props.content) return ''

  // 剥离模型思考/框选控制标记，避免原样进入 HTML
  const cleanContent = props.content.replace(/<\|begin_of_box\|>|<\|end_of_box\|>|<\|thought\|>|<\/thought>/g, '')

  let rawHtml: string
  try {
    // 每次渲染构建独立 renderer，并通过 options 传入 breaks/gfm（与原模块级 setOptions 等价），
    // 避免模块顶层副作用影响其他实例。
    rawHtml = marked.parse(cleanContent, {
      breaks: true,
      gfm: true,
      renderer: new marked.Renderer(),
    }) as string
  } catch (err) {
    // 畸形 markdown 不应冒泡到 ErrorBoundary 整页替换；回退为原始文本（已过特殊标记过滤）。
    logger.warn('markdown 解析失败，回退为原始文本', err)
    return protectedMarkup(DOMPurify.sanitize(cleanContent, { ADD_ATTR: ['target'] }))
  }
  // 防御 XSS：marked 默认不过滤 HTML，AI 输出与用户上传 markdown 均经过此组件渲染，
  // 必须用 DOMPurify 净化后再 v-html，阻断存储型/反射型 XSS。
  return protectedMarkup(DOMPurify.sanitize(rawHtml, { ADD_ATTR: ['target'] }))
})

const bodyElement = ref<HTMLElement | null>(null)
let generation = 0
const controllers = new Set<AbortController>()
const objectUrls = new Set<string>()

function clearImages() {
  generation += 1
  for (const controller of controllers) controller.abort()
  controllers.clear()
  for (const url of objectUrls) URL.revokeObjectURL(url)
  objectUrls.clear()
}

async function loadImages() {
  clearImages()
  const current = generation
  const images = bodyElement.value?.querySelectorAll<HTMLImageElement>('img[data-protected-file]') || []
  await Promise.all(Array.from(images).map(async (image) => {
    const controller = new AbortController()
    controllers.add(controller)
    try {
      const blob = await downloadFileBlob(Number(image.dataset.protectedFile), controller.signal)
      if (current !== generation || !image.isConnected) return
      const url = URL.createObjectURL(blob)
      objectUrls.add(url)
      image.src = url
    } catch (error) {
      if (!controller.signal.aborted) logger.warn('Markdown 文件图片加载失败', error)
    } finally {
      controllers.delete(controller)
    }
  }))
}

watch(renderedHtml, loadImages, { flush: 'post' })
onMounted(loadImages)

function downloadLink(event: MouseEvent) {
  const link = (event.target as HTMLElement | null)?.closest<HTMLAnchorElement>('a[data-protected-file]')
  if (!link || !bodyElement.value?.contains(link)) return
  event.preventDefault()
  void downloadAttachment(Number(link.dataset.protectedFile), link.textContent || 'download')
    .catch((error) => logger.warn('Markdown 文件下载失败', error))
}

onBeforeUnmount(clearImages)

</script>

<style scoped>
.markdown-body {
  font-size: 14px;
  line-height: 1.6;
  word-break: break-word;
}

.markdown-body :deep(p) {
  margin: 0 0 8px 0;
}

.markdown-body :deep(p:last-child) {
  margin-bottom: 0;
}

/* 防止宽图撑破消息气泡 */
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

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
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

.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid var(--color-border);
  padding: 6px 12px;
}

.markdown-body :deep(th) {
  background-color: var(--color-fill-2);
}
</style>
