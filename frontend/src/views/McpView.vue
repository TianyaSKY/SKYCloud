<template>
  <MainLayout active-menu="mcp" title="MCP 服务">
    <div class="mcp-container">

      <!-- Token Generation Section -->
      <div class="mcp-section token-section">
        <div class="section-header">
          <icon-safe class="section-icon" style="color: #165dff;" />
          <div>
            <h2 class="section-title">MCP Token</h2>
            <p class="section-desc">生成长效 Token 用于 AI 客户端连接你的云盘</p>
          </div>
        </div>

        <div class="token-card">
          <div v-if="!mcpToken" class="token-empty">
            <p>点击下方按钮生成一个 MCP 专用 Token（有效期 365 天）。</p>
            <p class="token-warning">
              <icon-exclamation-circle-fill style="color: #ff7d00; margin-right: 6px;" />
              Token 仅显示一次，请妥善保管。如需新 Token，可重新生成（旧 Token 仍有效直到过期）。
            </p>
            <a-button type="primary" size="large" :loading="generating" @click="handleGenerateToken">
              <template #icon><icon-lock /></template>
              生成 MCP Token
            </a-button>
          </div>

          <div v-else class="token-result">
            <a-alert type="success" banner>Token 生成成功！请立即复制保存，此 Token 不会再次显示。</a-alert>
            <div class="token-display">
              <div class="token-value-row">
                <code class="token-value">{{ mcpToken }}</code>
                <a-button type="outline" size="small" @click="handleCopyToken">
                  <template #icon><icon-copy /></template>
                  复制
                </a-button>
              </div>
              <div class="token-meta">
                <span>有效期：<strong>365 天</strong></span>
                <span>用户：<strong>{{ username }}</strong></span>
              </div>
            </div>
            <a-button type="text" size="small" @click="mcpToken = ''">隐藏 Token</a-button>
          </div>
        </div>
      </div>

      <!-- Connection Config Section -->
      <div class="mcp-section">
        <div class="section-header">
          <icon-settings class="section-icon" style="color: #722ed1;" />
          <div>
            <h2 class="section-title">客户端配置</h2>
            <p class="section-desc">将以下配置添加到你的 AI 客户端中</p>
          </div>
        </div>

        <div class="config-grid">
          <!-- Claude Desktop -->
          <div class="config-card">
            <div class="config-card-header">
              <icon-mind-mapping style="font-size: 22px; color: #722ed1;" />
              <h3>Claude Desktop</h3>
            </div>
            <p class="config-path">
              编辑 <code>claude_desktop_config.json</code>：
            </p>
            <div class="code-block-wrapper">
              <pre class="code-block">{{ claudeConfig }}</pre>
              <a-button class="copy-btn" type="text" size="mini" @click="handleCopy(claudeConfig)">
                <template #icon><icon-copy /></template>
              </a-button>
            </div>
          </div>

          <!-- Cursor -->
          <div class="config-card">
            <div class="config-card-header">
              <icon-thunderbolt style="font-size: 22px; color: #165dff;" />
              <h3>Cursor IDE</h3>
            </div>
            <p class="config-path">
              在项目根目录创建 <code>.cursor/mcp.json</code>：
            </p>
            <div class="code-block-wrapper">
              <pre class="code-block">{{ cursorConfig }}</pre>
              <a-button class="copy-btn" type="text" size="mini" @click="handleCopy(cursorConfig)">
                <template #icon><icon-copy /></template>
              </a-button>
            </div>
          </div>
        </div>
      </div>

      <!-- Service Status Section -->
      <div class="mcp-section">
        <div class="section-header">
          <icon-wifi class="section-icon" style="color: #00b42a;" />
          <div>
            <h2 class="section-title">服务信息</h2>
            <p class="section-desc">MCP Server 连接地址和可用工具</p>
          </div>
        </div>

        <div class="info-grid">
          <div class="info-item">
            <span class="info-label">服务地址</span>
            <code class="info-value">{{ mcpEndpoint }}</code>
          </div>
          <div class="info-item">
            <span class="info-label">传输协议</span>
            <span class="info-value">Streamable HTTP</span>
          </div>
          <div class="info-item">
            <span class="info-label">认证方式</span>
            <span class="info-value">Bearer Token (JWT)</span>
          </div>
          <div class="info-item">
            <span class="info-label">默认端口</span>
            <code class="info-value">5001</code>
          </div>
        </div>
      </div>

      <!-- Tools Reference -->
      <div class="mcp-section">
        <div class="section-header">
          <icon-tool class="section-icon" style="color: #ff7d00;" />
          <div>
            <h2 class="section-title">可用工具</h2>
            <p class="section-desc">MCP 客户端可调用以下工具操作你的云盘</p>
          </div>
        </div>

        <div class="tools-list">
          <div v-for="tool in tools" :key="tool.name" class="tool-card">
            <div class="tool-header">
              <a-tag color="arcoblue" size="small">{{ tool.name }}</a-tag>
            </div>
            <p class="tool-desc">{{ tool.description }}</p>
            <div class="tool-params">
              <span class="params-label">参数：</span>
              <code>{{ tool.params }}</code>
            </div>
          </div>
        </div>
      </div>

      <!-- Usage Tips -->
      <div class="mcp-section">
        <div class="section-header">
          <icon-bulb class="section-icon" style="color: #faad14;" />
          <div>
            <h2 class="section-title">使用提示</h2>
            <p class="section-desc">帮助你更好地使用 MCP 服务</p>
          </div>
        </div>

        <div class="tips-list">
          <a-alert type="info" class="tip-alert">
            <template #title>如何获取 Token？</template>
            点击上方「生成 MCP Token」按钮即可获得一个 365 天有效期的长效 Token。Token 基于你的登录身份生成，MCP 客户端通过此 Token 以你的身份操作云盘。
          </a-alert>
          <a-alert type="info" class="tip-alert">
            <template #title>Token 安全</template>
            Token 等同于你的登录凭证，请勿分享给他人。如果 Token 泄露，可生成新 Token 并更换配置。旧 Token 会在过期后自动失效。
          </a-alert>
          <a-alert type="info" class="tip-alert">
            <template #title>连接不上？</template>
            确认 MCP Server 容器（backend-mcp）已启动，端口 5001 已开放。如使用反向代理，需确保 /mcp 路径已正确代理到 MCP 容器。
          </a-alert>
        </div>
      </div>

    </div>
  </MainLayout>
</template>

<script lang="ts" setup>
import { ref, reactive, computed } from 'vue'
import { Message } from '@arco-design/web-vue'
import {
  IconBulb,
  IconCopy,
  IconExclamationCircleFill,
  IconLock,
  IconMindMapping,
  IconSafe,
  IconSettings,
  IconThunderbolt,
  IconTool,
  IconWifi,
} from '@arco-design/web-vue/es/icon'
import MainLayout from '../components/MainLayout.vue'
import { generateMcpToken } from '@/api/auth'

const mcpToken = ref('')
const generating = ref(false)

const username = computed(() => {
  const userStr = localStorage.getItem('user')
  return JSON.parse(userStr || '{}').username || '未知'
})

// MCP endpoint - same host as current page, port 5001
const mcpEndpoint = computed(() => {
  const host = window.location.hostname
  return `http://${host}:5001/mcp`
})

const configTemplate = (token: string) => {
  const url = mcpEndpoint.value
  return JSON.stringify({
    mcpServers: {
      skycloud: {
        url,
        headers: {
          Authorization: `Bearer ${token || '<YOUR_MCP_TOKEN>'}`
        }
      }
    }
  }, null, 2)
}

const claudeConfig = computed(() => configTemplate(mcpToken.value))
const cursorConfig = computed(() => configTemplate(mcpToken.value))

const tools = reactive([
  { name: 'search_files', description: '模糊搜索或 AI 语义搜索用户文件', params: 'user_id, query, search_type' },
  { name: 'list_files', description: '列出指定目录下的文件和文件夹', params: 'user_id, parent_id, page, sort_by' },
  { name: 'get_file_info', description: '获取文件的详细元数据和描述信息', params: 'user_id, file_id' },
  { name: 'create_folder', description: '在指定位置创建新文件夹', params: 'user_id, name, parent_id' },
  { name: 'move_file', description: '移动文件到其他文件夹或重命名', params: 'user_id, file_id, new_name, new_parent_id' },
  { name: 'delete_file', description: '永久删除文件（不可恢复）', params: 'user_id, file_id' },
])

const handleGenerateToken = async () => {
  generating.value = true
  try {
    const res: any = await generateMcpToken()
    mcpToken.value = res.mcp_token
    Message.success('MCP Token 生成成功')
  } catch (err) {
    Message.error('Token 生成失败')
  } finally {
    generating.value = false
  }
}

const handleCopyToken = () => {
  navigator.clipboard.writeText(mcpToken.value)
  Message.success('Token 已复制到剪贴板')
}

const handleCopy = (text: string) => {
  navigator.clipboard.writeText(text)
  Message.success('已复制到剪贴板')
}
</script>

<style scoped>
.mcp-container {
  max-width: 960px;
  margin: 0 auto;
  padding: 0 8px 60px;
}

.mcp-section {
  margin-bottom: 40px;
}

.section-header {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  margin-bottom: 20px;
}

.section-icon {
  font-size: 26px;
  line-height: 1;
  margin-top: 2px;
  flex-shrink: 0;
}

.section-title {
  font-size: 20px;
  font-weight: 600;
  color: var(--color-text-1);
  margin: 0;
}

.section-desc {
  font-size: 13px;
  color: var(--color-text-3);
  margin: 4px 0 0;
}

/* Token Section */
.token-section {
  margin-top: 8px;
}

.token-card {
  background: var(--color-fill-1);
  border: 1px solid var(--color-border-1);
  border-radius: 12px;
  padding: 28px;
}

.token-empty p {
  margin: 0 0 12px;
  font-size: 14px;
  color: var(--color-text-2);
}

.token-warning {
  display: flex;
  align-items: center;
  font-size: 13px;
  color: #ff7d00;
  margin-bottom: 20px !important;
}

.token-result {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.token-display {
  background: var(--color-fill-2);
  border-radius: 8px;
  padding: 16px;
}

.token-value-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.token-value {
  flex: 1;
  padding: 8px 12px;
  background: var(--color-fill-3);
  border-radius: 6px;
  font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
  font-size: 12px;
  color: var(--color-text-1);
  word-break: break-all;
  line-height: 1.6;
  max-height: 80px;
  overflow-y: auto;
}

.token-meta {
  display: flex;
  gap: 24px;
  margin-top: 12px;
  font-size: 13px;
  color: var(--color-text-3);
}

/* Config Grid */
.config-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.config-card {
  background: var(--color-fill-1);
  border: 1px solid var(--color-border-1);
  border-radius: 12px;
  padding: 24px;
}

.config-card-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.config-card-header h3 {
  font-size: 16px;
  font-weight: 600;
  margin: 0;
  color: var(--color-text-1);
}

.config-path {
  font-size: 13px;
  color: var(--color-text-3);
  margin: 0 0 12px;
}

.config-path code {
  background: var(--color-fill-3);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 12px;
}

.code-block-wrapper {
  position: relative;
}

.code-block {
  background: #1e1e2e;
  color: #cdd6f4;
  border-radius: 8px;
  padding: 16px;
  font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
  font-size: 12px;
  line-height: 1.6;
  overflow-x: auto;
  margin: 0;
  white-space: pre;
}

.copy-btn {
  position: absolute;
  top: 8px;
  right: 8px;
  color: #cdd6f4 !important;
  opacity: 0.6;
}

.copy-btn:hover {
  opacity: 1;
}

/* Info Grid */
.info-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}

.info-item {
  background: var(--color-fill-1);
  border: 1px solid var(--color-border-1);
  border-radius: 10px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.info-label {
  font-size: 12px;
  color: var(--color-text-3);
  font-weight: 500;
}

.info-value {
  font-size: 14px;
  color: var(--color-text-1);
  font-weight: 500;
}

code.info-value {
  font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
  font-size: 13px;
  background: none;
  padding: 0;
}

/* Tools List */
.tools-list {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}

.tool-card {
  background: var(--color-fill-1);
  border: 1px solid var(--color-border-1);
  border-radius: 10px;
  padding: 16px;
  transition: transform 0.15s, box-shadow 0.15s;
}

.tool-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.05);
}

.tool-header {
  margin-bottom: 8px;
}

.tool-desc {
  font-size: 13px;
  color: var(--color-text-2);
  margin: 0 0 10px;
  line-height: 1.5;
}

.tool-params {
  font-size: 12px;
  color: var(--color-text-4);
}

.tool-params code {
  font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
  color: var(--color-text-3);
}

.params-label {
  font-weight: 500;
}

/* Tips */
.tips-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.tip-alert {
  border-radius: 8px;
}

@media (max-width: 768px) {
  .config-grid {
    grid-template-columns: 1fr;
  }

  .info-grid {
    grid-template-columns: repeat(2, 1fr);
  }

  .tools-list {
    grid-template-columns: 1fr;
  }
}
</style>
