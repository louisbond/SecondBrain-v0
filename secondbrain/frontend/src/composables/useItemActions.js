/**
 * 任务条目的行内编辑 / 删除。
 *
 * 从 StatusView.vue 拆出（那个文件已顶到单文件 500 行上限）。
 * 采集阶段标题抓错、素材不想要，都不该逼用户走完整个入库流程。
 */
import { ref } from 'vue'
import { itemsApi } from '../api/client'

export function useItemActions(toast, onChanged) {
  const editing = ref(null)
  const editTitle = ref('')
  const editSummary = ref('')
  const editBusy = ref(false)
  const removing = ref(null)

  async function openEdit(item) {
    if (editing.value === item.id) {
      editing.value = null
      return
    }
    try {
      const d = await itemsApi.get(item.id)
      editTitle.value = d.title || item.title || ''
      editSummary.value = d.ai_summary || ''
      editing.value = item.id
    } catch (e) {
      toast?.('读取条目失败：' + e.message, 'error')
    }
  }

  async function saveEdit(id) {
    editBusy.value = true
    try {
      await itemsApi.patch(id, {
        title: editTitle.value.trim(),
        ai_summary: editSummary.value
      })
      toast?.('已保存', 'success')
      editing.value = null
      await onChanged?.()
    } catch (e) {
      toast?.('保存失败：' + e.message, 'error')
    } finally {
      editBusy.value = false
    }
  }

  async function removeItem(item) {
    const name = item.title || '未命名'
    if (!confirm(`删除「${name}」？\n会清掉这条采集记录、A 层草稿和归档音频；已入库的笔记保留。`)) {
      return
    }
    removing.value = item.id
    try {
      const r = await itemsApi.remove(item.id)
      toast?.(`已删除${r?.kept_notes ? '（已入库笔记保留）' : ''}`, 'success')
      if (editing.value === item.id) editing.value = null
      await onChanged?.()
    } catch (e) {
      toast?.('删除失败：' + e.message, 'error')
    } finally {
      removing.value = null
    }
  }

  return {
    editing,
    editTitle,
    editSummary,
    editBusy,
    removing,
    openEdit,
    saveEdit,
    removeItem
  }
}
