import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { AxiosError } from 'axios'
import request from '../request'
import { useAuthStore } from '../../stores/auth'
import { refreshBrowserSession } from '../../utils/authSession'
import router from '../../router'
vi.mock('../../router',()=>({default:{currentRoute:{value:{name:'home'}},push:vi.fn()}}))
vi.mock('@arco-design/web-vue',()=>({Message:{error:vi.fn()}}))
vi.mock('../../stores/workspace',()=>({useWorkspaceStore:()=>({currentWorkspace:{id:42},reset:vi.fn()})}))
vi.mock('../../utils/authSession',()=>({refreshBrowserSession:vi.fn(),logoutBrowserSession:vi.fn()}))
const refreshed = vi.mocked(refreshBrowserSession)
beforeEach(()=>{vi.clearAllMocks();setActivePinia(createPinia())})

describe('access header and refresh retry',()=>{
  it('coalesces concurrent expiration, injects the fresh token and retries each request once',async()=>{
    const auth = useAuthStore();auth.setAuth('expired',{id:7,username:'u',role:'common'})
    refreshed.mockResolvedValue({token:'fresh',user_id:7,role:'common',expires_in:900,
      user:{id:7,username:'u',role:'common'}})
    const observed: string[] = []
    request.defaults.adapter = async config => {
      const header=String(config.headers.Authorization);observed.push(header)
      expect(config.headers['X-Workspace-Id']).toBe('42')
      if(header==='Bearer expired') throw new AxiosError('expired','ERR_BAD_REQUEST',config,undefined,
        {status:401,statusText:'Unauthorized',headers:{},config,data:{detail:'Token expired!'}})
      return {status:200,statusText:'OK',headers:{},config,data:{accepted:true}}
    }
    expect(await Promise.all([request.get('/protected/a'),request.get('/protected/b')])).toEqual([
      {accepted:true},{accepted:true}])
    expect(refreshed).toHaveBeenCalledTimes(1)
    expect(observed).toEqual(['Bearer expired','Bearer expired','Bearer fresh','Bearer fresh'])
  })
  it('clears a revoked session and does not loop retries',async()=>{
    const auth=useAuthStore();auth.setAuth('revoked',{id:7})
    refreshed.mockResolvedValue(null)
    const adapter=vi.fn(async config=>{throw new AxiosError('revoked','ERR_BAD_REQUEST',config,undefined,
      {status:401,statusText:'Unauthorized',headers:{},config,data:{detail:'Session revoked'}})})
    request.defaults.adapter=adapter
    await expect(request.get('/protected')).rejects.toMatchObject({status:401})
    expect(adapter).toHaveBeenCalledTimes(1)
    expect(auth.token).toBeNull()
    expect(router.push).toHaveBeenCalledWith('/')
  })
  it('keeps memory state and avoids login redirect during a refresh outage',async()=>{
    const auth=useAuthStore();auth.setAuth('expired',{id:7})
    refreshed.mockRejectedValue(new Error('Redis unavailable'))
    request.defaults.adapter=async config=>{throw new AxiosError('expired','ERR_BAD_REQUEST',config,undefined,
      {status:401,statusText:'Unauthorized',headers:{},config,data:{detail:'Token expired!'}})}
    await expect(request.get('/protected')).rejects.toMatchObject({status:503})
    expect(auth.token).toBe('expired')
    expect(router.push).not.toHaveBeenCalled()
  })
})
