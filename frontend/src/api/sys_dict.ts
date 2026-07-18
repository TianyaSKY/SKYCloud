import request from './request'
import type { SysDictInput } from '../schemas/sys_dict'

/** 后端契约：系统字典项；id 创建前可空 */
export interface SysDict {
  id?: number
  key: string
  value: string
  des?: string
  enable: boolean
}

/** 列出全部系统字典 */
export function getSysDicts() {
  return request.get<SysDict[]>('/sys_dicts')
}

/** 获取单条字典 */
export function getSysDict(id: number) {
  return request.get<SysDict>(`/sys_dicts/${id}`)
}

/** 创建字典项 */
export function createSysDict(data: SysDictInput) {
  return request.post<SysDict>('/sys_dicts', data)
}

/** 更新字典项 */
export function updateSysDict(id: number, data: SysDictInput) {
  return request.put<SysDict>(`/sys_dicts/${id}`, data)
}

/** 删除字典项 */
export function deleteSysDict(id: number) {
  return request.delete<void>(`/sys_dicts/${id}`)
}
