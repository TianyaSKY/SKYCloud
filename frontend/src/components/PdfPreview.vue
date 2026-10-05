<template>
  <div class="pdf-preview">
    <div class="pdf-controls">
      <button :disabled="busy || page <= 1" @click="page -= 1">上一页</button>
      <span role="status">{{ page }} / {{ pageCount }}</span>
      <button :disabled="busy || page >= pageCount" @click="page += 1">下一页</button>
      <button :disabled="busy || scale <= 0.5" @click="scale -= 0.25">缩小</button>
      <button :disabled="busy || scale >= 2" @click="scale += 0.25">放大</button>
      <span v-if="busy">正在加载 PDF…</span>
    </div>
    <div class="pdf-page"><canvas ref="canvas" :aria-label="`PDF 第 ${page} 页`" /></div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onUnmounted, ref, watch } from 'vue'
import { getDocument, GlobalWorkerOptions, type PDFDocumentLoadingTask, type PDFDocumentProxy, type RenderTask } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.mjs?url'

GlobalWorkerOptions.workerSrc = workerUrl
const props = defineProps<{ src: string }>()
const emit = defineEmits<{ error: [error: unknown]; rendered: [] }>()
const canvas = ref<HTMLCanvasElement | null>(null)
const page = ref(1)
const pageCount = ref(0)
const scale = ref(1)
const busy = ref(false)
let generation = 0
let task: PDFDocumentLoadingTask | undefined
let document: PDFDocumentProxy | undefined
let renderTask: RenderTask | undefined

function clear() {
  generation += 1
  renderTask?.cancel()
  renderTask = undefined
  const previous = task
  task = undefined
  document = undefined
  void previous?.destroy().catch(() => {})
}

async function render(current = generation) {
  if (!document || !canvas.value) return
  busy.value = true
  try {
    const pdfPage = await document.getPage(page.value)
    if (current !== generation || !canvas.value) return
    const viewport = pdfPage.getViewport({ scale: scale.value })
    const ratio = Math.min(window.devicePixelRatio || 1, 2)
    // Bound canvas allocation even for adversarial page dimensions.
    if (viewport.width * viewport.height * ratio * ratio > 16_000_000) throw new Error('PDF page is too large')
    canvas.value.width = Math.ceil(viewport.width * ratio)
    canvas.value.height = Math.ceil(viewport.height * ratio)
    canvas.value.style.width = `${viewport.width}px`
    canvas.value.style.height = `${viewport.height}px`
    renderTask = pdfPage.render({ canvas: canvas.value, viewport,
      transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0] })
    await renderTask.promise
    if (current === generation) emit('rendered')
  } catch (error) {
    if (current === generation) emit('error', error)
  } finally {
    if (current === generation) busy.value = false
  }
}

watch(() => props.src, async src => {
  clear()
  const current = generation
  page.value = 1
  pageCount.value = 0
  if (!src) return
  busy.value = true
  try {
    task = getDocument({ url: src,
      cMapUrl: '/pdfjs/cmaps/', cMapPacked: true, standardFontDataUrl: '/pdfjs/standard_fonts/',
      wasmUrl: '/pdfjs/wasm/', iccUrl: '/pdfjs/iccs/' })
    const loaded = await task.promise
    if (current !== generation) return
    document = loaded
    pageCount.value = loaded.numPages
    await nextTick()
    await render(current)
  } catch (error) {
    if (current === generation) { busy.value = false; emit('error', error) }
  }
}, { immediate: true })
watch([page, scale], () => { if (!busy.value) void render() })
onUnmounted(clear)
</script>

<style scoped>
.pdf-controls { display: flex; align-items: center; justify-content: center; gap: 12px; padding: 12px; }
.pdf-page { overflow: auto; max-width: 100%; }
.pdf-page canvas { display: block; margin: auto; background: white; }
</style>
