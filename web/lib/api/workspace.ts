import type { components } from './generated';
import { apiClient, toApiFailure } from './client';
export type Workspace = components['schemas']['WorkspaceOut'];
export type WorkspaceCommand = Omit<components['schemas']['WorkspaceCommand'], 'expected_revision' | 'idempotency_key'>;
export type WorkspaceAction = components['schemas']['ActionOut'];
export async function getWorkspace(): Promise<Workspace> {
  const { data, error, response } = await apiClient.GET('/workspace');
  if (!data || !Number.isInteger(data.revision) || !Array.isArray(data.selection)) throw await toApiFailure(response, error || data);
  return data;
}
export async function updateWorkspace(command: WorkspaceCommand, revision: number, key: string): Promise<WorkspaceAction> {
  const { data, error, response } = await apiClient.POST('/workspace/actions', { body: { ...command, expected_revision: revision, idempotency_key: key } });
  if (!data || !data.workspace || !Number.isInteger(data.workspace.revision) || typeof data.operation_id !== 'string') throw await toApiFailure(response, error || data);
  return data;
}
export async function undoWorkspace(id: string, revision: number): Promise<WorkspaceAction> {
  const { data, error, response } = await apiClient.POST('/workspace/actions/{operation_id}/undo', { params: { path: { operation_id: id } }, body: { expected_revision: revision } });
  if (!data || !data.workspace || !Number.isInteger(data.workspace.revision) || typeof data.operation_id !== 'string') throw await toApiFailure(response, error || data);
  return data;
}
