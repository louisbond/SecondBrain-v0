<script setup>
/**
 * ③④ 分区与标签（v41 从 ProcessView.vue 拆出）。
 *
 * 状态与动作来自 `composables/useReview.js` 的注入上下文，
 * 本组件只负责渲染与绑定，不自己发请求、不自己持有表单状态。
 */
import { useReview } from '../../composables/useReview'

const {
  current,
  form,
  otherTags,
  partitionTree,
  expanded,
  newPartition,
  toggleExpand,
  partSelected,
  partCount,
  togglePartPath,
  toggleNewPartition,
  isNewPartitionChecked,
  togglePartition,
  toggleVocabTag,
  toggleNewTag,
  isNewTagChecked,
  addManualTag,
  createPartition
} = useReview()
</script>

<template>
  <div class="taxonomy-sections">
    <!-- 分区树选择 -->
    <div class="section">
      <label>③ 分区（选父分区即含其子树）</label>
      <div class="tree">
        <div v-for="node in partitionTree" :key="node.id" class="tree-node">
          <div class="tree-row">
            <button class="tree-toggle" @click="toggleExpand(node.path)">
              {{ expanded[node.path] ? '▾' : '▸' }}
            </button>
            <span
              class="tree-label"
              :class="{ checked: partSelected(node.path) }"
              @click="togglePartPath(node.path)"
              >{{ node.name }}</span
            >
            <span class="dim mono" style="margin-left: auto; font-size: 11px">
              {{ partCount(node) }}
            </span>
          </div>
          <div v-if="expanded[node.path]" class="tree-children">
            <div
              v-for="child in node.children"
              :key="child.id"
              class="tree-row"
              style="padding-left: 16px"
            >
              <span
                class="tree-label"
                :class="{ checked: partSelected(child.path) }"
                @click="togglePartPath(child.path)"
                >{{ child.name }}</span
              >
              <span class="dim mono" style="margin-left: auto; font-size: 11px">
                {{ partCount(child) }}
              </span>
            </div>
          </div>
        </div>
      </div>
      <div class="row" style="margin-top: 8px">
        <input
          v-model="newPartition"
          type="text"
          placeholder="新分区，回车创建并选中"
          style="flex: 1"
          @keydown.enter.prevent="createPartition($event)"
        />
      </div>
      <div
        v-if="(current.pending_partitions || []).length"
        class="row wrap"
        style="margin-top: 8px"
      >
        <span class="dim" style="font-size: 11.5px; width: 100%">
          待定分区（勾选后自动创建）：
        </span>
        <span
          v-for="p in current.pending_partitions || []"
          :key="p.name"
          class="check-chip pending"
          :class="{ checked: isNewPartitionChecked(p.name) }"
          :title="p.reason"
          @click="toggleNewPartition(p.name)"
        >
          <span>{{ isNewPartitionChecked(p.name) ? '☑' : '☐' }}</span> +{{ p.name }}
        </span>
      </div>
      <div v-if="form.partitions.length" class="row wrap" style="margin-top: 8px">
        <span
          v-for="p in form.partitions"
          :key="p"
          class="check-chip checked"
          @click="togglePartition(p)"
          >{{ p }} ✕</span
        >
      </div>
    </div>

    <!-- 标签 -->
    <div class="section">
      <label>④ 标签（勾选后才写入受控词表）</label>
      <div class="row wrap">
        <span
          v-for="t in current.suggested_tags || []"
          :key="t"
          class="check-chip"
          :class="{ checked: form.tags.includes(t) }"
          @click="toggleVocabTag(t)"
        >
          <span>{{ form.tags.includes(t) ? '☑' : '☐' }}</span> {{ t }}
        </span>
        <span
          v-for="t in otherTags"
          :key="t"
          class="check-chip"
          :class="{ checked: form.tags.includes(t) }"
          @click="toggleVocabTag(t)"
        >
          <span>{{ form.tags.includes(t) ? '☑' : '☐' }}</span> {{ t }}
        </span>
        <span
          v-for="t in current.pending_tags || []"
          :key="t.name"
          class="check-chip pending"
          :class="{ checked: isNewTagChecked(t.name) }"
          @click="toggleNewTag(t.name)"
        >
          <span>{{ isNewTagChecked(t.name) ? '☑' : '☐' }}</span> +{{ t.name }}
        </span>
      </div>
      <div class="row" style="margin-top: 8px">
        <input
          type="text"
          placeholder="手动添加标签（回车）"
          style="flex: 1"
          @keydown.enter.prevent="addManualTag"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.section {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}
.tree-node {
  margin: 2px 0;
}
.tree-row {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 3px 4px;
  border-radius: 6px;
  cursor: pointer;
}
.tree-row:hover {
  background: var(--accent-soft);
}
.tree-toggle {
  width: 16px;
  border: none;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  padding: 0;
}
.tree-label {
  font-size: 12.5px;
}
.tree-label.checked {
  color: var(--accent);
  font-weight: 600;
}
.tree-children {
  border-left: 1px solid var(--line);
  margin-left: 8px;
}
</style>
