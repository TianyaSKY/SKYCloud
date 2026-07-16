import { reactive, ref } from "vue";
import type { Ref, ShallowRef } from "vue";
import { Message } from "@arco-design/web-vue";
import {
  batchDeleteFiles,
  createFolder,
  deleteFile,
  deleteFolder,
  getAllFolders,
  getRootFolderId,
  organizeFiles,
  rebuildFailedIndexes,
  retryEmbedding,
  updateFile,
  updateFolder,
} from "../api/file";
import type { FileItem } from "../api/file";
import { createShare } from "../api/share";
import { logger } from "../utils/logger";

/** 移动目标文件夹树节点 */
interface FolderTreeNode {
  id: number;
  name: string;
  children: FolderTreeNode[];
}

export function useFileOperations(
  currentParentId: Ref<number | null>,
  fetchFiles: () => Promise<void>,
  selectedKeys: Ref<number[]>,
  startUpload: (files: File[]) => Promise<void>,
  fileList?: ShallowRef<FileItem[]>,
) {
  const folderForm = reactive({ name: "" });
  const showCreateFolder = ref(false);

  const showRenameModal = ref(false);
  const renameForm = reactive({ name: "" });
  const renamingItem = ref<FileItem | null>(null);

  const showMoveModal = ref(false);
  const movingItem = ref<FileItem | null>(null);
  const folderTree = ref<FolderTreeNode[]>([]);
  const targetFolderId = ref<number | null>(null);

  const showShareModal = ref(false);
  const showShareResult = ref(false);
  const selectedFile = ref<FileItem | null>(null);
  const shareUrl = ref("");
  const shareForm = reactive({ expires_at: "" });

  /** 批量上传文件 —— 委托给统一的上传管理器 */
  const handleBatchUpload = (files: File[]) => startUpload(files);

  const handleCreateFolder = async () => {
    if (!folderForm.name) return;
    try {
      await createFolder({
        name: folderForm.name,
        parent_id: currentParentId.value || undefined,
      });
      Message.success("创建成功");
      showCreateFolder.value = false;
      folderForm.name = "";
      await fetchFiles();
    } catch {
      // 错误提示由拦截器统一处理
    }
  };

  const handleDelete = async (record: FileItem) => {
    try {
      if (record.is_folder) {
        await deleteFolder(record.id);
      } else {
        await deleteFile(record.id);
      }
      Message.success("删除成功");
      await fetchFiles();
    } catch {
      // 错误提示由拦截器统一处理
    }
  };

  const handleBatchDelete = async (ids: number[]) => {
    try {
      const itemsToDelete = ids.map((id) => {
        const item = fileList?.value?.find((f) => f.id === id);
        return {
          id,
          is_folder: item ? !!item.is_folder : false,
        };
      });

      await batchDeleteFiles(itemsToDelete);
      Message.success("批量删除成功");
      selectedKeys.value = [];
      await fetchFiles();
    } catch (error) {
      // 拦截器已弹唯一 Message.error，这里仅记录上下文
      logger.warn("handleBatchDelete 失败 ids={} error={}", ids, error);
    }
  };

  const handleDownload = (record: FileItem) => {
    // 使用直接 URL 跳转触发浏览器原生下载，避免大文件 Blob 占用内存
    const token = localStorage.getItem("token");
    const url = `/api/files/${record.id}/download${token ? `?token=${encodeURIComponent(token)}` : ""}`;
    const link = document.createElement("a");
    link.href = url;
    link.setAttribute("download", record.name);
    document.body.appendChild(link);
    link.click();
    link.remove();
  };

  const handleShare = (record: FileItem) => {
    selectedFile.value = record;
    showShareModal.value = true;
  };

  const confirmShare = async () => {
    const file = selectedFile.value;
    if (!file) return;
    try {
      const res = await createShare({
        file_id: file.id,
        expires_at: shareForm.expires_at || undefined,
      });
      shareUrl.value = `${window.location.origin}/s/${res.token}`;
      showShareModal.value = false;
      showShareResult.value = true;
    } catch (error) {
      logger.warn("confirmShare 创建分享失败 file_id={} error={}", file.id, error);
    }
  };

  const handleRetryEmbedding = async (record: FileItem) => {
    try {
      await retryEmbedding(record.id);
      Message.success("已提交重试请求");
      await fetchFiles();
    } catch (error) {
      logger.warn("handleRetryEmbedding 失败 id={} error={}", record.id, error);
    }
  };

  const handleRebuildIndexes = async () => {
    try {
      const res = await rebuildFailedIndexes();
      Message.success(`已触发批量重建，共 ${res.count} 个文件`);
      await fetchFiles();
    } catch (error) {
      logger.warn("handleRebuildIndexes 失败 error={}", error);
    }
  };

  const handleRename = (record: FileItem) => {
    renamingItem.value = record;
    renameForm.name = record.name;
    showRenameModal.value = true;
  };

  const confirmRename = async () => {
    const item = renamingItem.value;
    if (!item || !renameForm.name) return;
    try {
      if (item.is_folder) {
        await updateFolder(item.id, { name: renameForm.name });
      } else {
        await updateFile(item.id, { name: renameForm.name });
      }
      Message.success("重命名成功");
      showRenameModal.value = false;
      await fetchFiles();
    } catch (error) {
      logger.warn("confirmRename 失败 id={} error={}", item.id, error);
    }
  };

  const handleMove = async (record: FileItem) => {
    movingItem.value = record;
    try {
      const res = await getAllFolders();
      const folders = res.folders || [];

      const buildTree = (parentId: number | null): FolderTreeNode[] => {
        return folders
          .filter((f) => f.parent_id === parentId)
          .map((f): FolderTreeNode => ({
            id: f.id,
            name: f.name,
            children: buildTree(f.id),
          }));
      };

      const rootRes = await getRootFolderId();
      const rootId = rootRes.root_folder_id;

      folderTree.value = [
        {
          id: rootId,
          name: "根目录",
          children: buildTree(rootId),
        },
      ];
      showMoveModal.value = true;
    } catch (error) {
      logger.warn("handleMove 获取文件夹列表失败 id={} error={}", record.id, error);
    }
  };

  const handleFolderSelect = (folderId: number) => {
    targetFolderId.value = folderId;
  };

  const confirmMove = async () => {
    const item = movingItem.value;
    if (!item) return;
    try {
      if (item.is_folder) {
        await updateFolder(item.id, {
          parent_id: targetFolderId.value,
        });
      } else {
        await updateFile(item.id, {
          parent_id: targetFolderId.value,
        });
      }
      Message.success("移动成功");
      showMoveModal.value = false;
      await fetchFiles();
    } catch (error) {
      logger.warn("confirmMove 失败 id={} target={} error={}", item.id, targetFolderId.value, error);
    }
  };

  const handleOrganize = async () => {
    try {
      const res = await organizeFiles();
      const message = res.message ?? "已开始智能整理";
      if (res.queued === false) {
        Message.warning(message);
      } else {
        Message.success(message);
      }
    } catch (error) {
      logger.warn("handleOrganize 失败 error={}", error);
    }
  };

  return {
    folderForm,
    showCreateFolder,
    showRenameModal,
    renameForm,
    renamingItem,
    showMoveModal,
    movingItem,
    folderTree,
    targetFolderId,
    showShareModal,
    showShareResult,
    selectedFile,
    shareUrl,
    shareForm,
    handleBatchUpload,
    handleCreateFolder,
    handleDelete,
    handleBatchDelete,
    handleDownload,
    handleShare,
    confirmShare,
    handleRetryEmbedding,
    handleRebuildIndexes,
    handleRename,
    confirmRename,
    handleMove,
    handleFolderSelect,
    confirmMove,
    handleOrganize,
  };
}
