import request from './request'

export interface SysDict {
    id?: number
    key: string
    value: string
    des?: string
    enable: boolean
}

export function getSysDicts() {
    return request({
        url: '/sys_dicts',
        method: 'get'
    })
}

export function getSysDict(id: number) {
    return request({
        url: `/sys_dicts/${id}`,
        method: 'get'
    })
}

export function createSysDict(data: SysDict) {
    return request({
        url: '/sys_dicts',
        method: 'post',
        data
    })
}

export function updateSysDict(id: number, data: SysDict) {
    return request({
        url: `/sys_dicts/${id}`,
        method: 'put',
        data
    })
}

export function deleteSysDict(id: number) {
    return request({
        url: `/sys_dicts/${id}`,
        method: 'delete'
    })
}
