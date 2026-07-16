<template>
  <MainLayout active-menu="mcp" title="MCP 服务">
    <div class="mcp-container">

      <!-- Token Generation Section -->
      <div class="mcp-section">
        <div class="section-header">
          <div class="section-title">
            <icon-safe style="color: #165dff;" />
            <span>MCP Token</span>
          </div>
          <p class="section-desc">生成长效 Token 用于 AI 客户端连接你的云盘</p>
        </div>

        <div class="flat-content">
          <div v-if="!mcpToken" class="token-create-area">
            <p style="margin: 0;">点击下方按钮生成一个 MCP 专用 Token（有效期 365 天）。Token 仅显示一次，请妥善保管。</p>
            <div style="display: flex; align-items: center; gap: 16px;">
              <a-input v-model="tokenName" class="token-name-input" placeholder="Token 名称，例如 Cursor / Claude Desktop" allow-clear />
              <a-button type="primary" :loading="generating" @click="handleGenerateToken">
                生成 MCP Token
              </a-button>
            </div>
          </div>

          <div v-else class="token-result">
            <p style="color: #00b42a; font-weight: 500; margin-top: 0;">Token 生成成功！请立即复制保存，此 Token 不会再次显示。</p>
            <div class="token-display">
              <code class="token-value">{{ mcpToken }}</code>
              <a-button type="outline" size="small" @click="handleCopyToken">
                复制
              </a-button>
            </div>
            <div class="token-meta">
              <span>有效期：365 天</span>
              <a-divider direction="vertical" />
              <span>用户：{{ username }}</span>
              <a-divider direction="vertical" />
              <a-button type="text" size="small" @click="mcpToken = ''" style="padding: 0;">隐藏 Token</a-button>
            </div>
          </div>
        </div>

        <div class="minimal-table-container">
          <div class="minimal-table-header">
            <h3>已生成 Token</h3>
            <a-button size="small" type="text" :loading="loadingTokens" @click="loadTokens">刷新列表</a-button>
          </div>
          <a-table :data="mcpTokens" :pagination="false" :loading="loadingTokens" row-key="id" :bordered="false" class="flat-table">
            <template #columns>
              <a-table-column title="名称" data-index="name" />
              <a-table-column title="Token" data-index="token_preview">
                <template #cell="{ record }">
                  <code>{{ record.token_preview }}</code>
                </template>
              </a-table-column>
              <a-table-column title="创建时间">
                <template #cell="{ record }"><span class="text-secondary">{{ formatDate(record.created_at) }}</span></template>
              </a-table-column>
              <a-table-column title="过期时间">
                <template #cell="{ record }"><span class="text-secondary">{{ formatDate(record.expires_at) }}</span></template>
              </a-table-column>
              <a-table-column title="状态">
                <template #cell="{ record }">
                  <span v-if="record.is_revoked" class="status-gray">已撤销</span>
                  <span v-else-if="record.is_expired" class="status-red">已过期</span>
                  <span v-else class="status-green">有效</span>
                </template>
              </a-table-column>
              <a-table-column title="操作" :width="80">
                <template #cell="{ record }">
                  <a-button type="text" status="danger" size="small" :disabled="record.is_revoked || record.is_expired" @click="handleRevokeToken(record.id)">
                    撤销
                  </a-button>
                </template>
              </a-table-column>
            </template>
          </a-table>
        </div>
      </div>

      <!-- Client Configuration Section -->
      <div class="mcp-section">
        <div class="section-header">
          <div class="section-title">
            <icon-settings style="color: #722ed1;" />
            <span>客户端一键配置</span>
          </div>
          <p class="section-desc">选择你的 AI 客户端，生成专属提示词以快速完成 MCP 接入</p>
        </div>

        <div class="flat-client-area">
          <div class="flat-tabs">
            <div
              v-for="c in clientConfigs"
              :key="c.key"
              class="flat-tab"
              :class="{ active: selectedClient === c.key }"
              @click="selectedClient = c.key"
            >
              <icon-thunderbolt v-if="c.key === 'opencode'" />
              <icon-robot v-else-if="c.key === 'claude'" />
              <icon-code v-else-if="c.key === 'cursor'" />
              <icon-mind-mapping v-else-if="c.key === 'codex'" />
              <span>{{ c.name }}</span>
            </div>
          </div>
          
          <div class="flat-client-detail">
            <div class="flat-client-header">
               <div>
                 <h3 class="flat-client-title">{{ activeClient.name }} 配置说明</h3>
                 <p class="flat-client-desc">{{ activeClient.desc }}</p>
               </div>
               <a-button type="primary" @click="handleCopy(activeClient.prompt)">
                 一键复制提示词
               </a-button>
            </div>
            
            <div class="flat-terminal">
              <pre class="prompt-text">{{ activeClient.prompt }}</pre>
            </div>
          </div>
        </div>
      </div>

      <!-- Service Status Section -->
      <div class="mcp-section">
        <div class="section-header">
          <div class="section-title">
            <icon-wifi style="color: #00b42a;" />
            <span>服务信息</span>
          </div>
          <p class="section-desc">MCP Server 连接地址和可用工具</p>
        </div>

        <div class="flat-kv-grid">
          <div class="kv-item">
            <span class="kv-label">服务地址</span>
            <span class="kv-value"><code>{{ mcpEndpoint }}</code></span>
          </div>
          <div class="kv-item">
            <span class="kv-label">默认端口</span>
            <span class="kv-value"><code>5001</code></span>
          </div>
          <div class="kv-item">
            <span class="kv-label">传输协议</span>
            <span class="kv-value">Streamable HTTP</span>
          </div>
          <div class="kv-item">
            <span class="kv-label">认证方式</span>
            <span class="kv-value">Bearer Token (JWT)</span>
          </div>
        </div>
      </div>

      <!-- Tools Reference -->
      <div class="mcp-section">
        <div class="section-header">
          <div class="section-title">
            <icon-tool style="color: #ff7d00;" />
            <span>可用工具</span>
          </div>
          <p class="section-desc">MCP 客户端可调用以下工具操作你的云盘</p>
        </div>

        <div class="flat-tools-grid">
          <div v-for="tool in tools" :key="tool.name" class="flat-tool-item">
            <div class="tool-name-col">{{ tool.name }}</div>
            <div class="tool-desc-col">
              <span class="tool-desc-text">{{ tool.description }}</span>
              <span class="tool-params-text">Params: <code>{{ tool.params }}</code></span>
            </div>
          </div>
        </div>
      </div>

      <!-- Usage Tips -->
      <div class="mcp-section">
        <div class="section-header">
          <div class="section-title">
            <icon-bulb style="color: #f5319d;" />
            <span>使用提示</span>
          </div>
          <p class="section-desc">帮助你更好地使用 MCP 服务</p>
        </div>

        <div class="flat-callouts">
          <div class="flat-callout">
            <div class="callout-title">如何获取 Token？</div>
            <div class="callout-text">点击上方「生成 MCP Token」按钮即可获得一个 365 天有效期的长效 Token。Token 基于你的登录身份生成，MCP 客户端通过此 Token 以你的身份操作云盘。</div>
          </div>
          <div class="flat-callout warning">
            <div class="callout-title">Token 安全</div>
            <div class="callout-text">Token 等同于你的登录凭证，请勿分享给他人。如果 Token 泄露，可生成新 Token 并更换配置。旧 Token 会在过期后自动失效。</div>
          </div>
          <div class="flat-callout">
            <div class="callout-title">连接不上？</div>
            <div class="callout-text">确认 MCP Server 容器（backend-mcp）已启动，端口 5001 已开放。如使用反向代理，需确保 /mcp 路径已正确代理到 MCP 容器。</div>
          </div>
        </div>
      </div>

    </div>
  </MainLayout>
</template>

<script lang="ts" setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { Message, Modal } from '@arco-design/web-vue'
import {
  IconBulb,
  IconCode,
  IconMindMapping,
  IconRobot,
  IconSafe,
  IconSettings,
  IconThunderbolt,
  IconTool,
  IconWifi,
} from '@arco-design/web-vue/es/icon'
import MainLayout from '../components/MainLayout.vue'
import { createMcpToken, listMcpTokens, revokeMcpToken, type McpTokenRecord } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'
import {formatDate} from '@/utils/format'
import {copyText} from '@/utils/clipboard'
import {logger} from '@/utils/logger'

const mcpToken = ref('')
const tokenName = ref('')
const generating = ref(false)
const loadingTokens = ref(false)
const mcpTokens = ref<McpTokenRecord[]>([])

const auth = useAuthStore()
const username = computed(() => auth.user.username || '未知')

// MCP endpoint - 同源主机，协议跟随当前页面（避免 HTTPS 站点生成 http 链接触发混合内容拦截）
const mcpEndpoint = computed(() => {
  const {hostname, protocol} = window.location
  return `${protocol}//${hostname}:5001/mcp`
})

const selectedClient = ref('opencode')

const clientConfigs = computed(() => {
  const url = mcpEndpoint.value
  const token = mcpToken.value || '<YOUR_MCP_TOKEN>'
  return [
    {
      key: 'opencode',
      name: 'OpenCode',
      desc: '复制提示词发送给 OpenCode 中的 AI 助手，自动完成配置',
      prompt: `帮我把远程 MCP \`SKYCLOUD\` 配到 OpenCode：URL 是 \`${url}\`，Header 是 \`Authorization=Bearer ${token}\`；如果已有同名配置就更新，完成后运行 \`opencode mcp list\` 验证连接，不要回显完整 token。`,
    },
    {
      key: 'claude',
      name: 'Claude Desktop',
      desc: '复制提示词发送给 Claude，让它帮你编辑 claude_desktop_config.json',
      prompt: `帮我在 Claude Desktop 配置文件 claude_desktop_config.json 中添加或更新远程 MCP 服务 \`skycloud\`：\n\n{\n  "mcpServers": {\n    "skycloud": {\n      "url": "${url}",\n      "headers": {\n        "Authorization": "Bearer ${token}"\n      }\n    }\n  }\n}\n\n如果 mcpServers 中已有 skycloud 就覆盖，没有就新增。保存后重启 Claude Desktop 使配置生效。不要回显完整 token。`,
    },
    {
      key: 'cursor',
      name: 'Cursor IDE',
      desc: '复制提示词发送给 Cursor 中的 AI 助手，自动配置 .cursor/mcp.json',
      prompt: `帮我在 Cursor 项目的 .cursor/mcp.json 中添加或更新远程 MCP 服务 \`skycloud\`：\n\n{\n  "mcpServers": {\n    "skycloud": {\n      "url": "${url}",\n      "headers": {\n        "Authorization": "Bearer ${token}"\n      }\n    }\n  }\n}\n\n如果 mcpServers 中已有 skycloud 就覆盖，没有就新增。不要回显完整 token。`,
    },
    {
      key: 'codex',
      name: 'Codex CLI',
      desc: '复制提示词发送给 Codex CLI，自动完成 MCP 配置',
      prompt: `帮我在 Codex CLI 配置中添加或更新远程 MCP 服务 \`SKYCLOUD\`：URL 是 \`${url}\`，Header 是 \`Authorization=Bearer ${token}\`；如果已有同名配置就更新，不要回显完整 token。`,
    },
  ]
})

const activeClient = computed(() => {
  return clientConfigs.value.find(c => c.key === selectedClient.value) || clientConfigs.value[0]!
})

const tools = reactive([
  { name: 'get_current_user', description: '获取当前用户信息（用户名、角色、Token 用量）', params: '无' },
  { name: 'search_files', description: '模糊搜索或 AI 语义搜索用户文件', params: 'query, page, page_size, search_type' },
  { name: 'list_files', description: '列出指定目录下的文件和文件夹', params: 'parent_id, page, page_size, name, sort_by, order' },
  { name: 'get_file_info', description: '获取文件的详细元数据和描述信息', params: 'file_id' },
  { name: 'create_folder', description: '在指定位置创建新文件夹', params: 'name, parent_id' },
  { name: 'move_file', description: '移动文件到其他文件夹或重命名', params: 'file_id, new_name, new_parent_id' },
  { name: 'delete_file', description: '永久删除文件（不可恢复）', params: 'file_id' },
  { name: 'get_file_download_url', description: '生成文件的临时下载链接', params: 'file_id, expires_hours' },
  { name: 'read_file_content', description: '读取文本文件内容（txt, md, csv, json, 代码文件等）', params: 'file_id, encoding' },
  { name: 'move_folder', description: '移动或重命名文件夹', params: 'folder_id, new_name, new_parent_id' },
  { name: 'delete_folder', description: '删除文件夹及其所有内容（不可恢复）', params: 'folder_id' },
  { name: 'get_storage_overview', description: '获取云盘存储概览（文件数、大小、状态统计）', params: '无' },
  { name: 'batch_delete', description: '批量删除多个文件和/或文件夹', params: 'items: [{id, is_folder}]' },
  { name: 'get_folder_tree', description: '获取文件夹树形结构，了解目录布局', params: 'max_depth' },
])

const handleGenerateToken = async () => {
  // 生成防重入：避免连点导致重复签发 Token
  if (generating.value) return
  generating.value = true
  try {
    const res = await createMcpToken({ name: tokenName.value })
    mcpToken.value = res.mcp_token
    tokenName.value = ''
    await loadTokens()
    Message.success('MCP Token 生成成功')
  } catch (error) {
    // 拦截器已弹全局错误提示，此处仅记录上下文
    logger.warn('生成 MCP Token 失败', {name: tokenName.value, error})
  } finally {
    generating.value = false
  }
}

const loadTokens = async () => {
  loadingTokens.value = true
  try {
    const res = await listMcpTokens()
    mcpTokens.value = res
  } catch (error) {
    logger.warn('加载 MCP Token 列表失败', {error})
  } finally {
    loadingTokens.value = false
  }
}

const handleRevokeToken = (id: number) => {
  Modal.confirm({
    title: '撤销 MCP Token',
    content: '撤销后，使用该 Token 的 MCP 客户端将无法继续访问你的云盘。',
    okText: '撤销',
    okButtonProps: { status: 'danger' },
    onOk: async () => {
      await revokeMcpToken(id)
      await loadTokens()
      Message.success('MCP Token 已撤销')
    }
  })
}

const handleCopyToken = async () => {
  const ok = await copyText(mcpToken.value)
  if (ok) {
    Message.success('Token 已复制到剪贴板')
  } else {
    Message.warning('复制失败，请手动复制')
  }
}

const handleCopy = async (text: string) => {
  const ok = await copyText(text)
  if (ok) {
    Message.success('已复制到剪贴板')
  } else {
    Message.warning('复制失败，请手动复制')
  }
}

onMounted(() => {
  loadTokens()
})
</script>


<style scoped>
.mcp-container {
  max-width: 800px;
  margin: 0 auto;
  padding: 40px 8px 80px;
  animation: fadeIn 0.4s ease-out;
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

.mcp-section {
  margin-bottom: 64px;
}

.section-header {
  margin-bottom: 32px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--color-border-2);
}

.section-title {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 20px;
  font-weight: 600;
  color: var(--color-text-1);
  margin: 0 0 8px;
}

.section-desc {
  font-size: 14px;
  color: var(--color-text-3);
  margin: 0;
  padding-left: 32px;
}

/* Tokens */
.flat-content {
  margin-bottom: 32px;
}

.token-create-area {
  padding: 24px;
  background: var(--color-fill-2);
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  font-size: 14px;
  color: var(--color-text-2);
}

.token-name-input {
  max-width: 320px;
}

.token-result {
  padding: 24px;
  background: rgba(0, 180, 42, 0.05);
  border-left: 4px solid #00b42a;
  border-radius: 4px;
}

.token-display {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin: 16px 0;
  min-width: 0;
}

.token-value {
  font-family: 'JetBrains Mono', Monaco, monospace;
  font-size: 13px;
  color: var(--color-text-1);
  background: var(--color-fill-2);
  padding: 8px 12px;
  border-radius: 4px;
  word-break: break-all;
  flex: 1;
  min-width: 0;
  line-height: 1.6;
  max-height: 80px;
  overflow-y: auto;
}

.token-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--color-text-3);
}

/* Tables */
.minimal-table-container {
  margin-top: 40px;
}

.minimal-table-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  margin-bottom: 16px;
}

.minimal-table-header h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text-1);
}

.text-secondary {
  color: var(--color-text-3);
}

.status-gray { color: var(--color-text-4); }
.status-red { color: rgb(var(--danger-6)); }
.status-green { color: rgb(var(--success-6)); }

.flat-table :deep(.arco-table-th) {
  background: transparent;
  border-bottom: 1px solid var(--color-border-2);
  color: var(--color-text-3);
  font-weight: 500;
}
.flat-table :deep(.arco-table-td) {
  border-bottom: 1px dashed var(--color-border-1);
}

code {
  font-family: 'JetBrains Mono', Monaco, monospace;
  background: var(--color-fill-2);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
  color: var(--color-text-2);
}

/* Flat Tabs */
.flat-client-area {
  margin-top: 24px;
}

.flat-tabs {
  display: flex;
  gap: 32px;
  border-bottom: 1px solid var(--color-border-2);
  margin-bottom: 32px;
  overflow-x: auto;
}

.flat-tab {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 4px 12px;
  cursor: pointer;
  color: var(--color-text-3);
  font-size: 15px;
  font-weight: 500;
  position: relative;
  transition: color 0.2s;
  white-space: nowrap;
}

.flat-tab:hover {
  color: var(--color-text-2);
}

.flat-tab.active {
  color: rgb(var(--primary-6));
}

.flat-tab.active::after {
  content: '';
  position: absolute;
  bottom: -1px;
  left: 0;
  right: 0;
  height: 2px;
  background: rgb(var(--primary-6));
}

.flat-client-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 20px;
}

.flat-client-title {
  margin: 0 0 4px;
  font-size: 16px;
  font-weight: 600;
}

.flat-client-desc {
  margin: 0;
  font-size: 14px;
  color: var(--color-text-3);
}

.flat-terminal {
  background: #1e1e1e;
  border-radius: 6px;
  padding: 20px;
}

.prompt-text {
  margin: 0;
  color: #d4d4d4;
  font-family: 'SF Mono', Monaco, monospace;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}

/* KV list */
.flat-kv-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 24px 48px;
}

.kv-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-bottom: 16px;
  border-bottom: 1px dashed var(--color-border-2);
}

.kv-label {
  font-size: 13px;
  color: var(--color-text-3);
}

.kv-value {
  font-size: 15px;
  color: var(--color-text-1);
}

/* Flat Tools */
.flat-tools-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 16px;
}

.flat-tool-item {
  display: grid;
  grid-template-columns: 200px 1fr;
  gap: 24px;
  padding: 16px 0;
  border-bottom: 1px dashed var(--color-border-2);
}

.tool-name-col {
  font-family: 'JetBrains Mono', Monaco, monospace;
  font-weight: 600;
  color: var(--color-text-1);
  font-size: 14px;
}

.tool-desc-col {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.tool-desc-text {
  font-size: 14px;
  color: var(--color-text-2);
}

.tool-params-text {
  font-size: 13px;
  color: var(--color-text-3);
}

/* Callouts */
.flat-callouts {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.flat-callout {
  padding: 16px 20px;
  border-left: 3px solid var(--color-text-3);
  background: var(--color-fill-1);
}
.flat-callout.warning {
  border-left-color: #ff7d00;
}

.callout-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text-1);
  margin-bottom: 8px;
}

.callout-text {
  font-size: 14px;
  color: var(--color-text-2);
  line-height: 1.6;
}

@media (max-width: 768px) {
  .flat-kv-grid {
    grid-template-columns: 1fr;
  }
  .flat-tool-item {
    grid-template-columns: 1fr;
    gap: 12px;
  }
  .flat-client-header {
    flex-direction: column;
    gap: 16px;
  }
}
</style>
