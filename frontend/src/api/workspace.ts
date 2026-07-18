import request from './request'
import {
    workspaceInfoSchema,
    workspaceListResponseSchema,
    type CreateWorkspaceInput,
    type WorkspaceInfo,
    type WorkspaceListResult,
} from '../schemas/workspace'

export type {WorkspaceInfo, WorkspaceListResult}

export const listWorkspaces = () => {
    return request.get<unknown>('/workspace').then(workspaceListResponseSchema.parse)
}

export const getWorkspace = (id: number) => {
    return request.get<unknown>(`/workspace/${id}`).then(workspaceInfoSchema.parse)
}

export const createWorkspace = (data: CreateWorkspaceInput) => {
    return request.post<unknown>('/workspace', data).then(workspaceInfoSchema.parse)
}

export const startWorkspace = (id: number) => {
    return request.post<unknown>(`/workspace/${id}/start`).then(workspaceInfoSchema.parse)
}

export const stopWorkspace = (id: number) => {
    return request.post<unknown>(`/workspace/${id}/stop`).then(workspaceInfoSchema.parse)
}

export const deleteWorkspace = (id: number) => {
    return request.delete<void>(`/workspace/${id}`)
}

export const restartWorkspace = (id: number) => {
    return request.post<unknown>(`/workspace/${id}/restart`, null, {timeout: 30000}).then(workspaceInfoSchema.parse)
}
