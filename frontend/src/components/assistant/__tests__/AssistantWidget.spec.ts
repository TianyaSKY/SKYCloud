import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { reactive, nextTick } from 'vue'
import AssistantWidget from '../AssistantWidget.vue'
const mocks = vi.hoisted(() => ({ assistant: {} as Record<string, unknown>, workspace: {} as Record<string, unknown>, bind: vi.fn(), cancel: vi.fn(), error: vi.fn() }))
vi.mock('@/stores/assistant', () => ({ useAssistantStore: () => mocks.assistant }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ user: { id: 7 }, sessionVersion: 0 }) }))
vi.mock('@/stores/workspace', () => ({ useWorkspaceStore: () => mocks.workspace }))
vi.mock('@arco-design/web-vue', () => ({ Message: { error: mocks.error }, Modal: { confirm: vi.fn() } }))
beforeEach(() => {
  vi.resetAllMocks(); mocks.bind.mockResolvedValue(undefined)
  mocks.assistant = reactive({ currentMode: 'expert', currentConversations: [], messages: [], loading: false, initialized: true, initializing: false, expertAvailable: true, bindWorkspace: mocks.bind, cancel: mocks.cancel })
  mocks.workspace = reactive({ canWrite: true, currentWorkspace: { id: 11 } })
})
async function open() {
  const wrapper = mount(AssistantWidget, { props: { show: true }, global: { stubs: {
    AssistantHeader: true, AssistantConversationList: true, AssistantMessageList: true,
    AssistantComposer: { name: 'AssistantComposer', props: ['disabled', 'loading'], emits: ['cancel'], template: '<button @click="$emit(\'cancel\')">Stop</button>' },
  } } })
  await wrapper.find('.assistant-trigger').trigger('click')
  return wrapper
}
describe('assistant widget permissions and disposal', () => {
  it('disables expert composition after role removal and during navigation', async () => {
    const wrapper = await open(); const composer = wrapper.findComponent({ name: 'AssistantComposer' })
    expect(composer.props('disabled')).toBe(false)
    mocks.workspace.canWrite = false; await nextTick(); expect(composer.props('disabled')).toBe(true)
    mocks.assistant.currentMode = 'fast'; await nextTick(); expect(composer.props('disabled')).toBe(false)
    mocks.assistant.initializing = true; await nextTick(); expect(composer.props('disabled')).toBe(true)
    wrapper.unmount(); expect(mocks.bind).toHaveBeenLastCalledWith(null)
  })
  it('shows cancellation failure without an unhandled event promise', async () => {
    mocks.cancel.mockRejectedValue(new Error('server unavailable'))
    const wrapper = await open(); await wrapper.findComponent({ name: 'AssistantComposer' }).find('button').trigger('click')
    await vi.waitFor(() => expect(mocks.error).toHaveBeenCalledWith('server unavailable'))
    wrapper.unmount()
  })
})
