import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import AssistantMessageList from '../AssistantMessageList.vue'
import type { AssistantMessage } from '@/stores/assistant'

function mountList(messages: AssistantMessage[]) {
  return mount(AssistantMessageList, {
    props: {
      messages,
      mode: 'expert',
      canHandoff: false,
    },
    global: {
      stubs: {
        MarkdownRenderer: {
          props: ['content'],
          template: '<div class="markdown-renderer">{{ content }}</div>',
        },
        ExpertToolTimeline: true,
        PermissionRequestCard: true,
        FileDiffCard: true,
        'icon-arrow-right': true,
        'icon-robot': true,
        'icon-user': true,
      },
    },
  })
}

describe('AssistantMessageList', () => {
  it('renders the question, title, and answer as separate sections', () => {
    const wrapper = mountList([
      {
        id: 1,
        role: 'user',
        content: '我想安装前端设计方面的 skill',
        status: 'completed',
      },
      {
        id: 2,
        role: 'assistant',
        title: 'Frontend skill setup',
        content: '可以，我会先确认安装来源。',
        status: 'completed',
      },
    ])

    expect(wrapper.find('.message-type').text()).toBe('询问')
    expect(wrapper.find('.assistant-title').text()).toContain('Frontend skill setup')
    expect(wrapper.find('.answer-label').text()).toBe('回答')
    expect(wrapper.find('.markdown-renderer').text()).toContain('可以，我会先确认安装来源。')
  })

  it('does not render an empty title section for fast answers', () => {
    const wrapper = mountList([
      {
        id: 3,
        role: 'assistant',
        content: '快速回答',
        status: 'completed',
      },
    ])

    expect(wrapper.find('.assistant-title').exists()).toBe(false)
    expect(wrapper.find('.answer-label').text()).toBe('回答')
  })
})
