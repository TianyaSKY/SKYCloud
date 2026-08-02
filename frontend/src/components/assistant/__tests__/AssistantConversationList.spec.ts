import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { AssistantConversation } from '@/api/assistant'
import AssistantConversationList from '../AssistantConversationList.vue'

function conversation(
  id: number,
  status: 'active' | 'archived',
  title: string,
): AssistantConversation {
  return {
    id,
    workspace_id: 1,
    user_id: 2,
    mode: 'expert',
    title,
    status,
    source_conversation_id: null,
    opencode_runtime_id: null,
    opencode_session_id: null,
    created_at: '2026-08-02T10:00:00+08:00',
    updated_at: '2026-08-02T10:00:00+08:00',
    last_message_at: null,
  }
}

function mountList(conversations: AssistantConversation[]) {
  return mount(AssistantConversationList, {
    props: { conversations, currentId: conversations[0]?.id || null },
    global: {
      stubs: {
        'a-dropdown': { template: '<div><slot /><slot name="content" /></div>' },
        'a-button': { template: '<button><slot name="icon" /></button>' },
        'a-doption': {
          emits: ['click'],
          template: '<button class="a-doption" @click="$emit(\'click\')"><slot name="icon" /><slot /></button>',
        },
        'icon-delete': true,
        'icon-edit': true,
        'icon-more': true,
      },
    },
  })
}

describe('AssistantConversationList', () => {
  it('renders active and archived sessions and emits selection', async () => {
    const wrapper = mountList([
      conversation(1, 'active', '当前专家任务'),
      conversation(2, 'archived', '历史专家任务'),
    ])

    expect(wrapper.findAll('.session-item')).toHaveLength(2)
    expect(wrapper.text()).toContain('已归档')

    await wrapper.findAll('.session-button')[1].trigger('click')
    expect(wrapper.emitted('select')).toEqual([[2]])
  })

  it('emits rename/archive/delete for active and rename/restore/delete for archived', async () => {
    const active = conversation(1, 'active', '当前专家任务')
    const archived = conversation(2, 'archived', '历史专家任务')
    const wrapper = mountList([active, archived])
    const options = wrapper.findAll('.a-doption')

    expect(options).toHaveLength(6)
    await options[0].trigger('click')
    await options[1].trigger('click')
    await options[2].trigger('click')
    await options[3].trigger('click')
    await options[4].trigger('click')
    await options[5].trigger('click')

    expect(wrapper.emitted('rename')).toEqual([[active], [archived]])
    expect(wrapper.emitted('archive')).toEqual([[1]])
    expect(wrapper.emitted('restore')).toEqual([[2]])
    expect(wrapper.emitted('delete')).toEqual([[1], [2]])
  })
})
