import { beforeEach, describe, expect, it, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { defineComponent, reactive } from 'vue'
import WorkspaceManageView from '../WorkspaceManageView.vue'
import RuntimeUiDialog from '@/components/RuntimeUiDialog.vue'
import * as api from '@/api/workspace'
import type { MemberInfo, WorkspaceInfo } from '@/api/workspace'
const mocks = vi.hoisted(() => ({ confirm: vi.fn(), success: vi.fn(), warning: vi.fn() }))
vi.mock('@arco-design/web-vue', () => ({ Message: { success: mocks.success, warning: mocks.warning }, Modal: { confirm: mocks.confirm } }))
let store: { workspaces: WorkspaceInfo[]; loading: boolean; currentWorkspace: WorkspaceInfo | null; switchWorkspace: ReturnType<typeof vi.fn>; loadWorkspaces: ReturnType<typeof vi.fn> }
let auth: { user: { id: number }; sessionVersion: number }
vi.mock('@/stores/workspace', () => ({ useWorkspaceStore: () => store }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))
vi.mock('@/api/workspace', () => ({ createWorkspace: vi.fn(), deleteWorkspace: vi.fn(), inviteMember: vi.fn(), listMembers: vi.fn(), removeMember: vi.fn(), updateMemberRole: vi.fn(), startSkycode: vi.fn(), stopSkycode: vi.fn() }))
const slot = defineComponent({ template: '<div><slot /><slot name="actions" /><slot name="description" /></div>' })
const meta = defineComponent({ props: ['title'], template: '<div>{{ title }}<slot name="description" /></div>' })
const button = defineComponent({ template: '<button><slot /></button>' })
const overlay = defineComponent({ props: ['visible'], template: '<section v-if="visible"><slot /></section>' })
const input = defineComponent({ props: ['modelValue'], emits: ['update:modelValue'], template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />' })
const select = defineComponent({ props: ['modelValue'], emits: ['change', 'update:modelValue'], template: '<select :value="modelValue" @change="$emit(\'change\', $event.target.value); $emit(\'update:modelValue\', $event.target.value)"><option>admin</option><option>editor</option><option>viewer</option></select>' })
const ws = (id: number, role = 'admin', owner = 1): WorkspaceInfo => ({ id, name: `space-${id}`, owner_id: owner, my_role: role, status: 'running' })
const member = (user: number): MemberInfo => ({ id: user, workspace_id: 1, user_id: user, role: 'editor', username: `user-${user}` })
const members = (...users: number[]) => ({ code: 200 as const, members: users.map(member) })
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
function render() { return shallowMount(WorkspaceManageView, { global: { stubs: { MainLayout: slot, 'a-spin': slot, 'a-card': slot, 'a-button': button, 'a-drawer': overlay, 'a-modal': overlay, 'a-list': slot, 'a-list-item': slot, 'a-list-item-meta': meta, 'a-tag': slot, 'a-form': slot, 'a-form-item': slot, 'a-textarea': input, 'a-option': true, 'a-avatar': true, 'a-input': input, 'a-select': select } } }) }
type Wrapper = ReturnType<typeof render>
const controls = (wrapper: Wrapper, text: string) => wrapper.findAll('button').filter(node => node.text() === text)
async function open(wrapper: Wrapper, index = 0) { await controls(wrapper, '成员管理')[index]!.trigger('click'); await flushPromises() }
const confirmation = () => mocks.confirm.mock.calls.at(-1)![0] as { onOk: () => Promise<void> }
beforeEach(() => {
  vi.resetAllMocks()
  auth = reactive({ user: { id: 1 }, sessionVersion: 0 })
  store = reactive({ workspaces: [ws(1), ws(2)], loading: false, currentWorkspace: null, switchWorkspace: vi.fn(), loadWorkspaces: vi.fn().mockResolvedValue(undefined) })
  vi.mocked(api.listMembers).mockResolvedValue(members(1, 3))
})
describe('workspace member mutations and stale UI', () => {
  it.each(['viewer', 'editor', 'unknown'])('permits %s member listing while hiding admin mutation controls', async role => {
    store.workspaces = [ws(1, role)]
    const wrapper = render(); await open(wrapper)
    expect(api.listMembers).toHaveBeenCalledWith(1)
    expect(controls(wrapper, '邀请')).toHaveLength(0); expect(controls(wrapper, '移除')).toHaveLength(0)
    expect(wrapper.find('select').exists()).toBe(false)
    if (role === 'unknown') expect(controls(wrapper, '打开 SKYcode')).toHaveLength(0)
    wrapper.unmount()
  })
  it('restricts workspace deletion to its owner rather than every workspace admin', () => {
    store.workspaces = [ws(1, 'admin', 8)]
    const wrapper = render(); expect(controls(wrapper, '删除')).toHaveLength(0); wrapper.unmount()
  })
  it('clears old members immediately and discards out-of-order detail responses', async () => {
    const first = deferred<ReturnType<typeof members>>(); const second = deferred<ReturnType<typeof members>>()
    vi.mocked(api.listMembers).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const wrapper = render(); await open(wrapper); await open(wrapper, 1)
    second.resolve(members(5)); await flushPromises(); expect(wrapper.text()).toContain('user-5')
    first.resolve(members(9)); await flushPromises(); expect(wrapper.text()).not.toContain('user-9'); wrapper.unmount()
  })
  it('never retargets a removal confirmation after opening another workspace', async () => {
    const wrapper = render(); await open(wrapper); await controls(wrapper, '移除')[0]!.trigger('click')
    const pending = confirmation(); await open(wrapper, 1); await pending.onOk()
    expect(api.removeMember).not.toHaveBeenCalled(); wrapper.unmount()
  })
  it('rechecks admin permission before a deferred removal confirmation', async () => {
    const wrapper = render(); await open(wrapper); await controls(wrapper, '移除')[0]!.trigger('click')
    store.workspaces[0]!.my_role = 'viewer'; await wrapper.vm.$nextTick(); await confirmation().onOk()
    expect(api.removeMember).not.toHaveBeenCalled(); expect(controls(wrapper, '邀请')).toHaveLength(0); wrapper.unmount()
  })
  it('blocks duplicate removals and uses the captured workspace for all requests', async () => {
    const pending = deferred<void>(); vi.mocked(api.removeMember).mockReturnValue(pending.promise)
    const wrapper = render(); await open(wrapper); await controls(wrapper, '移除')[0]!.trigger('click')
    const confirm = confirmation(); const first = confirm.onOk(); await confirm.onOk()
    expect(api.removeMember).toHaveBeenCalledTimes(1); expect(api.removeMember).toHaveBeenCalledWith(1, 3)
    pending.resolve(); await first; expect(store.loadWorkspaces).toHaveBeenCalledTimes(1); wrapper.unmount()
  })
  it('discards a late invitation result without resetting or refreshing the newer drawer', async () => {
    const pending = deferred<unknown>(); vi.mocked(api.inviteMember).mockReturnValue(pending.promise)
    const wrapper = render(); await open(wrapper); await wrapper.find('input').setValue(' new-user ')
    await controls(wrapper, '邀请')[0]!.trigger('click'); await controls(wrapper, '邀请')[0]!.trigger('click')
    expect(api.inviteMember).toHaveBeenCalledTimes(1); expect(api.inviteMember).toHaveBeenCalledWith(1, { username: 'new-user', role: 'editor' })
    await open(wrapper, 1); await wrapper.find('input').setValue('keep-new-draft')
    pending.resolve({}); await flushPromises(); expect(wrapper.find('input').element.value).toBe('keep-new-draft')
    expect(api.listMembers).toHaveBeenCalledTimes(2); expect(mocks.success).not.toHaveBeenCalled(); wrapper.unmount()
  })
  it('protects the owner, updates a non-owner role, and refreshes authoritative membership', async () => {
    const wrapper = render(); await open(wrapper)
    expect(wrapper.findAll('select')).toHaveLength(2) // invite role and one non-owner role
    await wrapper.findAll('select')[1]!.setValue('viewer'); await flushPromises()
    expect(api.updateMemberRole).toHaveBeenCalledWith(1, 3, 'viewer'); expect(store.loadWorkspaces).toHaveBeenCalledTimes(1)
    expect(controls(wrapper, '移除')).toHaveLength(1); wrapper.unmount()
  })
  it.each(['logout', 'unmount'])('discards pending member replies and confirmation callbacks after %s', async action => {
    const wrapper = render(); await open(wrapper); await controls(wrapper, '移除')[0]!.trigger('click')
    const confirm = confirmation()
    if (action === 'logout') { auth.sessionVersion += 1; await wrapper.vm.$nextTick() }
    else wrapper.unmount()
    await confirm.onOk(); expect(api.removeMember).not.toHaveBeenCalled()
    if (action === 'logout') { expect(controls(wrapper, '邀请')).toHaveLength(0); wrapper.unmount() }
  })
  it('revokes write permission immediately after self-demotion while refresh is pending', async () => {
    store.workspaces = [ws(1, 'admin', 8)]; store.currentWorkspace = store.workspaces[0]!
    const pending = deferred<ReturnType<typeof members>>()
    vi.mocked(api.listMembers).mockResolvedValueOnce(members(1, 3)).mockReturnValueOnce(pending.promise)
    const wrapper = render(); await open(wrapper)
    await wrapper.findAll('select')[1]!.setValue('viewer'); await flushPromises()
    expect(api.updateMemberRole).toHaveBeenCalledWith(1, 1, 'viewer')
    expect(store.currentWorkspace.my_role).toBe('viewer'); expect(controls(wrapper, '邀请')).toHaveLength(0)
    pending.resolve(members(1, 3)); await flushPromises(); wrapper.unmount()
  })
  it('clears membership after self-removal and reloads spaces without querying the forbidden member list', async () => {
    store.workspaces = [ws(1, 'admin', 8)]; store.currentWorkspace = store.workspaces[0]!
    const wrapper = render(); await open(wrapper); await controls(wrapper, '移除')[0]!.trigger('click')
    await confirmation().onOk(); await flushPromises()
    expect(api.removeMember).toHaveBeenCalledWith(1, 1); expect(api.listMembers).toHaveBeenCalledTimes(1)
    expect(store.currentWorkspace).toBeNull(); expect(store.workspaces).toEqual([])
    expect(store.loadWorkspaces).toHaveBeenCalledTimes(1); expect(controls(wrapper, '邀请')).toHaveLength(0)
    wrapper.unmount()
  })
  it('drops pending member lists after logout and after the drawer closes', async () => {
    const pending = deferred<ReturnType<typeof members>>()
    vi.mocked(api.listMembers).mockReturnValue(pending.promise)
    const wrapper = render(); await open(wrapper)
    auth.sessionVersion += 1; await wrapper.vm.$nextTick()
    pending.resolve(members(9)); await flushPromises()
    expect(wrapper.text()).not.toContain('user-9'); expect(controls(wrapper, '邀请')).toHaveLength(0)
    wrapper.unmount()
  })
  it('allows retry after a failed mutation without reporting success or changing the selected role', async () => {
    vi.mocked(api.updateMemberRole).mockRejectedValueOnce(new Error('outage')).mockResolvedValueOnce({})
    const wrapper = render(); await open(wrapper)
    await wrapper.findAll('select')[1]!.setValue('viewer'); await flushPromises()
    expect(mocks.success).not.toHaveBeenCalled(); expect(store.loadWorkspaces).not.toHaveBeenCalled()
    await wrapper.findAll('select')[1]!.setValue('admin'); await flushPromises()
    expect(api.updateMemberRole).toHaveBeenCalledTimes(2); expect(store.loadWorkspaces).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
  it('ignores late mutation completion after unmount', async () => {
    const pending = deferred<unknown>(); vi.mocked(api.updateMemberRole).mockReturnValue(pending.promise)
    const wrapper = render(); await open(wrapper); await wrapper.findAll('select')[1]!.setValue('viewer')
    wrapper.unmount(); pending.resolve({}); await flushPromises()
    expect(mocks.success).not.toHaveBeenCalled(); expect(api.listMembers).toHaveBeenCalledTimes(1)
    expect(store.loadWorkspaces).not.toHaveBeenCalled()
  })
  it('closes the runtime dialog immediately when authoritative membership is demoted', async () => {
    const wrapper = render(); await controls(wrapper, '打开 SKYcode')[0]!.trigger('click')
    expect(wrapper.findComponent(RuntimeUiDialog).props('modelValue')).toBe(true)
    store.workspaces[0]!.my_role = 'viewer'; await wrapper.vm.$nextTick()
    expect(wrapper.findComponent(RuntimeUiDialog).props('modelValue')).toBe(false); wrapper.unmount()
  })
})
