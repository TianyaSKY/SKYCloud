import { beforeEach, describe, expect, it, vi } from 'vitest'
import { shallowMount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import WorkspaceManageView from '../WorkspaceManageView.vue'
import RuntimeUiDialog from '@/components/RuntimeUiDialog.vue'

const store = vi.hoisted(() => ({ workspaces: [] as Record<string, unknown>[], loading: false,
  currentWorkspace: null, switchWorkspace: vi.fn(), loadWorkspaces: vi.fn() }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ user: { id: 1 }, sessionVersion: 0 }) }))
vi.mock('@/stores/workspace', () => ({ useWorkspaceStore: () => store }))
vi.mock('@/api/workspace', () => ({ createWorkspace: vi.fn(), deleteWorkspace: vi.fn(),
  getWorkspace: vi.fn(), inviteMember: vi.fn(), listMembers: vi.fn(), removeMember: vi.fn(),
  updateMemberRole: vi.fn(), startSkycode: vi.fn(), stopSkycode: vi.fn() }))
const slot = defineComponent({ template: '<div><slot /></div>' })
const button = defineComponent({ template: '<button><slot /></button>' })
function render(role: string, status = 'running') {
  store.workspaces = [{ id: 7, name: 'Workspace', my_role: role, status, access_url: null }]
  return shallowMount(WorkspaceManageView, { global: { stubs: {
    MainLayout: slot, 'a-spin': slot, 'a-card': slot, 'a-button': button,
  } } })
}
beforeEach(() => vi.clearAllMocks())

describe('routed workspace runtime entry', () => {
  it.each(['admin', 'editor'])('opens a running %s workspace without a legacy access URL', async role => {
    const wrapper = render(role)
    const open = wrapper.findAll('button').find(node => node.text() === '打开 SKYcode')!
    expect(open).toBeDefined()
    await open.trigger('click')
    const dialog = wrapper.findComponent(RuntimeUiDialog)
    expect(dialog.props('modelValue')).toBe(true)
    expect(dialog.props('workspace')).toEqual(store.workspaces[0])
    dialog.vm.$emit('update:modelValue', false)
    await wrapper.vm.$nextTick()
    expect(dialog.props('modelValue')).toBe(false)
    wrapper.unmount()
  })
  it.each(['running', 'stopped'])('hides runtime actions from viewers of a %s workspace', status => {
    const wrapper = render('viewer', status)
    expect(wrapper.text()).not.toMatch(/打开 SKYcode|启动 SKYcode|停止 SKYcode/)
    expect(wrapper.findComponent(RuntimeUiDialog).props('modelValue')).toBe(false)
    wrapper.unmount()
  })
  it('offers startup instead of opening a stopped editor workspace', () => {
    const wrapper = render('editor', 'stopped')
    expect(wrapper.text()).toContain('启动 SKYcode')
    expect(wrapper.text()).not.toContain('打开 SKYcode')
    wrapper.unmount()
  })
})
