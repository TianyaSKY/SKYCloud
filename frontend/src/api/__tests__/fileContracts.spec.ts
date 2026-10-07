import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import request from '../request'
import { searchFiles, getRootFolderId, initMultipartUpload } from '../file'
vi.mock('../../router',()=>({default:{currentRoute:{value:{name:'home'}},push:vi.fn()}}))
vi.mock('@arco-design/web-vue',()=>({Message:{error:vi.fn()}}))
vi.mock('../../stores/workspace',()=>({useWorkspaceStore:()=>({currentWorkspace:{id:42}})}))
beforeEach(()=>setActivePinia(createPinia()))

describe('file API wire contracts',()=>{
  it.each(['vector','fuzzy'] as const)('maps UI search mode %s to the backend parameter',async type=>{
    request.defaults.adapter=async config=>{
      expect(config.params.type).toBe(type==='vector'?'semantic':'fuzzy')
      return {status:200,statusText:'OK',headers:{},config,data:{success:true,data:{items:[],total:0,page:1,page_size:10}}}
    }
    expect((await searchFiles({q:'query',type})).total).toBe(0)
  })
  it('unwraps folder identifiers and multipart initialization',async()=>{
    request.defaults.adapter=async config=>({status:200,statusText:'OK',headers:{},config,data:{success:true,
      data:config.url==='/folder/root_id'?{root_folder_id:7}:{instant_upload:false,upload_id:'owned',chunk_size:4,total_chunks:1,uploaded_chunks:[]}}})
    expect(await getRootFolderId()).toEqual({root_folder_id:7})
    expect((await initMultipartUpload({filename:'hello',total_size:2})).upload_id).toBe('owned')
  })
})
