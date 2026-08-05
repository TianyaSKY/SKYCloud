import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import PermissionRequestCard from '../PermissionRequestCard.vue'

describe('PermissionRequestCard', () => {
  it('renders structured tool details instead of object stringification', () => {
    const wrapper = mount(PermissionRequestCard, {
      props: {
        permission: {
          permission_id: 'perm-1',
          title: '专家请求执行高风险操作',
          tool: { name: 'apply_patch', input: { path: 'src/App.vue' } },
        },
      },
      global: {
        stubs: {
          'a-button': { template: '<button><slot /></button>' },
          'icon-exclamation-circle': true,
        },
      },
    })

    expect(wrapper.find('.permission-tool').text()).toContain('apply_patch')
    expect(wrapper.find('.permission-tool').text()).toContain('src/App.vue')
    expect(wrapper.text()).not.toContain('[object Object]')
  })
})
