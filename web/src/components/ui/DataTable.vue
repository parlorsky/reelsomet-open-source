<script setup lang="ts">
import { ref, computed } from 'vue'

export interface Column {
  key: string
  label: string
  sortable?: boolean
  width?: string
}

const props = defineProps<{
  columns: Column[]
  rows: Record<string, any>[]
  loading?: boolean
  emptyText?: string
}>()

const sortKey = ref<string | null>(null)
const sortDir = ref<'asc' | 'desc'>('asc')

function toggleSort(key: string) {
  if (sortKey.value === key) {
    sortDir.value = sortDir.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortKey.value = key
    sortDir.value = 'asc'
  }
}

const sortedRows = computed(() => {
  if (!sortKey.value) return props.rows
  const key = sortKey.value
  const dir = sortDir.value === 'asc' ? 1 : -1
  return [...props.rows].sort((a, b) => {
    const av = a[key]
    const bv = b[key]
    if (av == null && bv == null) return 0
    if (av == null) return 1
    if (bv == null) return -1
    if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * dir
    return String(av).localeCompare(String(bv)) * dir
  })
})
</script>

<template>
  <div class="overflow-x-auto">
    <table class="w-full text-left text-sm">
      <thead>
        <tr class="table-head">
          <th
            v-for="col in columns"
            :key="col.key"
            class="px-4 py-3 font-semibold"
            :style="col.width ? { width: col.width } : {}"
            :class="{ 'cursor-pointer select-none hover:text-text-primary': col.sortable }"
            @click="col.sortable ? toggleSort(col.key) : null"
          >
            <span class="inline-flex items-center gap-1">
              {{ col.label }}
              <span v-if="col.sortable && sortKey === col.key" class="text-accent">
                {{ sortDir === 'asc' ? '\u25B2' : '\u25BC' }}
              </span>
            </span>
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="loading">
          <td :colspan="columns.length" class="px-4 py-8 text-center text-text-secondary">
            Loading...
          </td>
        </tr>
        <tr v-else-if="sortedRows.length === 0">
          <td :colspan="columns.length" class="px-4 py-8 text-center text-text-secondary">
            {{ emptyText || 'No data' }}
          </td>
        </tr>
        <tr
          v-for="(row, idx) in sortedRows"
          :key="idx"
          class="table-row-hover border-b border-border-subtle last:border-b-0"
        >
          <td v-for="col in columns" :key="col.key" class="px-4 py-3 align-top">
            <slot :name="`cell-${col.key}`" :row="row" :value="row[col.key]">
              {{ row[col.key] ?? '-' }}
            </slot>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
