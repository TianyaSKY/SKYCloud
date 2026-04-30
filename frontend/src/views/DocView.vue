<template>
  <MainLayout active-menu="docs" title="项目文档">
    <div class="docs-container">
      <!-- Hero Section -->
      <div class="docs-hero">
        <div class="hero-icon">
          <icon-cloud style="font-size: 52px; color: rgb(var(--primary-6));" />
        </div>
        <h1 class="hero-title">SKYCloud</h1>
        <p class="hero-subtitle">AI 驱动的云文件管理平台</p>
        <div class="hero-badges">
          <span class="badge badge-blue">FastAPI</span>
          <span class="badge badge-green">Vue 3</span>
          <span class="badge badge-purple">LangChain</span>
          <span class="badge badge-orange">pgvector</span>
          <span class="badge badge-cyan">MCP</span>
        </div>
      </div>

      <!-- Features Section -->
      <div class="docs-section">
        <h2 class="section-title">核心功能</h2>
        <div class="feature-grid">
          <div class="feature-card">
            <div class="feature-icon"><icon-folder style="font-size: 28px; color: #165dff;" /></div>
            <h3>文件管理</h3>
            <p>支持文件上传、下载、预览、分片上传和秒传。拖拽上传，批量操作，文件夹树形结构管理。</p>
          </div>
          <div class="feature-card">
            <div class="feature-icon"><icon-robot style="font-size: 28px; color: #722ed1;" /></div>
            <h3>AI 智能问答</h3>
            <p>基于 RAG 技术的智能问答系统，自动理解文件内容并生成向量索引，支持多查询融合检索和 Rerank 重排序。</p>
          </div>
          <div class="feature-card">
            <div class="feature-icon"><icon-link style="font-size: 28px; color: #00b42a;" /></div>
            <h3>文件分享</h3>
            <p>一键生成分享链接，支持设置过期时间。收件箱功能让文件协作更便捷。</p>
          </div>
          <div class="feature-card">
            <div class="feature-icon"><icon-apps style="font-size: 28px; color: #ff7d00;" /></div>
            <h3>智能整理</h3>
            <p>基于 LangGraph 的 AI Agent 自动分析文件内容，智能归类整理到合适的文件夹结构中。</p>
          </div>
          <div class="feature-card">
            <div class="feature-icon"><icon-command style="font-size: 28px; color: #0fc6c2;" /></div>
            <h3>MCP 协议</h3>
            <p>支持 Model Context Protocol，让 Claude Desktop、Cursor 等 AI 客户端直接操作你的云盘文件。</p>
          </div>
          <div class="feature-card">
            <div class="feature-icon"><icon-storage style="font-size: 28px; color: #f53f3f;" /></div>
            <h3>容器化部署</h3>
            <p>Docker Compose 一键部署全套服务（API、Worker、MCP、前端、数据库、缓存），开箱即用。</p>
          </div>
        </div>
      </div>

      <!-- Architecture Section -->
      <div class="docs-section">
        <h2 class="section-title">系统架构</h2>
        <div class="arch-container">
          <div class="arch-layer">
            <div class="arch-label">客户端</div>
            <div class="arch-items">
              <div class="arch-item arch-frontend">Vue 3 前端</div>
              <div class="arch-item arch-mcp-client">MCP 客户端</div>
            </div>
          </div>
          <div class="arch-arrow">
            <icon-arrow-down />
          </div>
          <div class="arch-layer">
            <div class="arch-label">服务层</div>
            <div class="arch-items">
              <div class="arch-item arch-api">REST API<br/><small>:5000</small></div>
              <div class="arch-item arch-mcp-srv">MCP Server<br/><small>:5001</small></div>
              <div class="arch-item arch-worker">Worker<br/><small>异步任务</small></div>
            </div>
          </div>
          <div class="arch-arrow">
            <icon-arrow-down />
          </div>
          <div class="arch-layer">
            <div class="arch-label">基础设施</div>
            <div class="arch-items">
              <div class="arch-item arch-db">PostgreSQL + pgvector</div>
              <div class="arch-item arch-redis">Redis</div>
              <div class="arch-item arch-llm">LLM / Embedding</div>
            </div>
          </div>
        </div>
      </div>

      <!-- MCP Section -->
      <div class="docs-section">
        <h2 class="section-title">MCP 协议介绍</h2>
        <div class="mcp-intro">
          <div class="mcp-intro-text">
            <p>
              <strong>MCP (Model Context Protocol)</strong> 是由 Anthropic 推出的开放标准协议，
              定义了 AI 应用与外部数据源、工具之间的标准化通信方式。
            </p>
            <p>
              SKYCloud 通过实现 MCP Server，让支持该协议的 AI 客户端（如 Claude Desktop、Cursor IDE、Cline 等）
              可以直接访问你的云盘——搜索文件、浏览目录、管理文件夹，无需手动操作 Web 界面。
            </p>
            <div class="mcp-benefit-list">
              <div class="mcp-benefit">
                <icon-check-circle-fill style="color: #00b42a; font-size: 16px; flex-shrink: 0;" />
                <span>AI 助手可以直接搜索和浏览你的云盘文件</span>
              </div>
              <div class="mcp-benefit">
                <icon-check-circle-fill style="color: #00b42a; font-size: 16px; flex-shrink: 0;" />
                <span>通过自然语言指令管理文件和文件夹</span>
              </div>
              <div class="mcp-benefit">
                <icon-check-circle-fill style="color: #00b42a; font-size: 16px; flex-shrink: 0;" />
                <span>标准化协议，支持多种 AI 客户端</span>
              </div>
              <div class="mcp-benefit">
                <icon-check-circle-fill style="color: #00b42a; font-size: 16px; flex-shrink: 0;" />
                <span>JWT Token 认证，安全可靠</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- MCP Tools Table -->
      <div class="docs-section">
        <h2 class="section-title">MCP 可用工具</h2>
        <a-table :data="mcpTools" :pagination="false" :bordered="false" size="medium">
          <template #columns>
            <a-table-column title="工具名称" data-index="name">
              <template #cell="{ record }">
                <a-tag color="arcoblue">{{ record.name }}</a-tag>
              </template>
            </a-table-column>
            <a-table-column title="功能" data-index="description" />
            <a-table-column title="参数" data-index="params">
              <template #cell="{ record }">
                <span class="param-text">{{ record.params }}</span>
              </template>
            </a-table-column>
          </template>
        </a-table>
      </div>

      <!-- Quick Start -->
      <div class="docs-section">
        <h2 class="section-title">快速开始</h2>
        <div class="steps-container">
          <div class="step-card">
            <div class="step-number">1</div>
            <div class="step-content">
              <h4>部署服务</h4>
              <div class="code-block">
                <code>docker compose up -d --build</code>
              </div>
            </div>
          </div>
          <div class="step-card">
            <div class="step-number">2</div>
            <div class="step-content">
              <h4>获取 MCP Token</h4>
              <p>前往侧栏「MCP 服务」页面，点击生成按钮获取长效 Token。</p>
            </div>
          </div>
          <div class="step-card">
            <div class="step-number">3</div>
            <div class="step-content">
              <h4>配置 AI 客户端</h4>
              <p>将 Token 和服务地址配置到 Claude Desktop 或 Cursor IDE 中即可使用。</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  </MainLayout>
</template>

<script lang="ts" setup>
import { reactive } from 'vue'
import {
  IconApps,
  IconArrowDown,
  IconCheckCircleFill,
  IconCloud,
  IconCommand,
  IconFolder,
  IconLink,
  IconRobot,
  IconStorage,
} from '@arco-design/web-vue/es/icon'
import MainLayout from '../components/MainLayout.vue'

const mcpTools = reactive([
  { name: 'search_files', description: '模糊搜索或 AI 语义搜索文件', params: 'query, search_type' },
  { name: 'list_files', description: '列出指定目录下的文件和文件夹', params: 'parent_id, page, sort_by' },
  { name: 'get_file_info', description: '获取文件的详细元数据和描述', params: 'file_id' },
  { name: 'create_folder', description: '创建新文件夹', params: 'name, parent_id' },
  { name: 'move_file', description: '移动或重命名文件', params: 'file_id, new_name, new_parent_id' },
  { name: 'delete_file', description: '删除文件（不可恢复）', params: 'file_id' },
])
</script>

<style scoped>
.docs-container {
  max-width: 960px;
  margin: 0 auto;
  padding: 0 8px 60px;
}

/* Hero */
.docs-hero {
  text-align: center;
  padding: 48px 0 40px;
  border-bottom: 1px solid var(--color-border-1);
  margin-bottom: 40px;
}

.hero-icon {
  margin-bottom: 12px;
}

.hero-title {
  font-size: 36px;
  font-weight: 700;
  color: var(--color-text-1);
  margin: 0 0 8px;
  letter-spacing: 2px;
}

.hero-subtitle {
  font-size: 16px;
  color: var(--color-text-3);
  margin: 0 0 24px;
}

.hero-badges {
  display: flex;
  justify-content: center;
  gap: 10px;
  flex-wrap: wrap;
}

.badge {
  padding: 4px 14px;
  border-radius: 20px;
  font-size: 13px;
  font-weight: 500;
}

.badge-blue { background: #e8f3ff; color: #165dff; }
.badge-green { background: #e8ffea; color: #00b42a; }
.badge-purple { background: #f0e8ff; color: #722ed1; }
.badge-orange { background: #fff3e8; color: #ff7d00; }
.badge-cyan { background: #e8fffb; color: #0fc6c2; }

/* Sections */
.docs-section {
  margin-bottom: 48px;
}

.section-title {
  font-size: 22px;
  font-weight: 600;
  color: var(--color-text-1);
  margin: 0 0 20px;
  padding-bottom: 10px;
  border-bottom: 2px solid rgb(var(--primary-6));
  display: inline-block;
}

/* Feature Grid */
.feature-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
}

.feature-card {
  padding: 24px;
  background: var(--color-fill-1);
  border-radius: 12px;
  transition: transform 0.2s, box-shadow 0.2s;
  border: 1px solid var(--color-border-1);
}

.feature-card:hover {
  transform: translateY(-3px);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.06);
}

.feature-icon {
  margin-bottom: 12px;
}

.feature-card h3 {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text-1);
  margin: 0 0 8px;
}

.feature-card p {
  font-size: 13px;
  color: var(--color-text-3);
  margin: 0;
  line-height: 1.6;
}

/* Architecture */
.arch-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}

.arch-layer {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 16px;
}

.arch-label {
  width: 72px;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-2);
  text-align: right;
  flex-shrink: 0;
}

.arch-items {
  flex: 1;
  display: flex;
  gap: 12px;
}

.arch-item {
  flex: 1;
  padding: 14px 16px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  text-align: center;
  border: 1px solid var(--color-border-2);
}

.arch-item small {
  display: block;
  font-weight: 400;
  opacity: 0.7;
  margin-top: 2px;
}

.arch-frontend { background: #e8f3ff; color: #165dff; }
.arch-mcp-client { background: #e8fffb; color: #0fc6c2; }
.arch-api { background: #e8ffea; color: #00b42a; }
.arch-mcp-srv { background: #e8fffb; color: #0fc6c2; }
.arch-worker { background: #fff3e8; color: #ff7d00; }
.arch-db { background: #f0e8ff; color: #722ed1; }
.arch-redis { background: #ffe8e8; color: #f53f3f; }
.arch-llm { background: #fff8e8; color: #faad14; }

.arch-arrow {
  font-size: 18px;
  color: var(--color-text-4);
  margin: 2px 0 2px 88px;
}

/* MCP Intro */
.mcp-intro {
  background: linear-gradient(135deg, #f0f9ff 0%, #f0fff4 100%);
  border: 1px solid var(--color-border-1);
  border-radius: 12px;
  padding: 32px;
}

.mcp-intro-text p {
  font-size: 14px;
  line-height: 1.8;
  color: var(--color-text-2);
  margin: 0 0 16px;
}

.mcp-benefit-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 16px;
}

.mcp-benefit {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 14px;
  color: var(--color-text-1);
}

.param-text {
  font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
  font-size: 12px;
  color: var(--color-text-3);
}

/* Steps */
.steps-container {
  display: flex;
  gap: 20px;
}

.step-card {
  flex: 1;
  display: flex;
  gap: 16px;
  padding: 20px;
  background: var(--color-fill-1);
  border-radius: 12px;
  border: 1px solid var(--color-border-1);
}

.step-number {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: rgb(var(--primary-6));
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 16px;
  flex-shrink: 0;
}

.step-content h4 {
  margin: 0 0 8px;
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text-1);
}

.step-content p {
  margin: 0;
  font-size: 13px;
  color: var(--color-text-3);
  line-height: 1.6;
}

.code-block {
  background: var(--color-fill-3);
  border-radius: 6px;
  padding: 8px 14px;
  font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
  font-size: 13px;
  color: var(--color-text-1);
  margin-top: 4px;
}

@media (max-width: 768px) {
  .feature-grid {
    grid-template-columns: 1fr;
  }

  .steps-container {
    flex-direction: column;
  }
}
</style>
