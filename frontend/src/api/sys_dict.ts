import request from './request'
import type {SysDictInput} from '../schemas/sys_dict'

export interface SysDict {
    id?: number
    key: string
    value: string
    des?: string
    enable: boolean
}

export function getSysDicts() {
    return request.get<SysDict[]>('/sys_dicts')
}

export function getSysDict(id: number) {
    return request.get<SysDict>(`/sys_dicts/${id}`)
}

export function createSysDict(data: SysDictInput) {
    return request.post<SysDict>('/sys_dicts', data)
}

export function updateSysDict(id: number, data: SysDictInput) {
    return request.put<SysDict>(`/sys_dicts/${id}`, data)
}

export function deleteSysDict(id: number) {
    return request.delete<void>(`/sys_dicts/${id}`)
}
