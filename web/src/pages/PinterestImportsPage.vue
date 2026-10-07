<script setup lang="ts">
import { ref } from 'vue'
import { pinterestApi } from '@/api/endpoints'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import DataTable from '@/components/ui/DataTable.vue'
import type { Column } from '@/components/ui/DataTable.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import SectionPanel from '@/components/ui/SectionPanel.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import type { PinterestImport } from '@/api/types'

const toast = useToast()
const imports = ref<PinterestImport[]>([])
const manifestText = ref('')
const files = ref<File[]>([])
const uploading = ref(false)

const columns: Column[] = [
  { key: 'import_id', label: 'Import', sortable: true },
  { key: 'status', label: 'Status', sortable: true },
  { key: 'source_name', label: 'Source' },
  { key: 'assets_count', label: 'Assets', sortable: true },
  { key: 'boards_count', label: 'Boards', sortable: true },
  { key: 'pins_count', label: 'Pins', sortable: true },
  { key: 'created_at', label: 'Created', sortable: true },
]

async function fetchImports() {
  const res = await pinterestApi.listImports()
  imports.value = res.data
}

const { loading, refresh } = usePolling(fetchImports, 15000)

function onManifestFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  file.text().then((text) => {
    manifestText.value = text
  })
}

function onImageFiles(event: Event) {
  const input = event.target as HTMLInputElement
  files.value = Array.from(input.files || [])
}

async function submitImport() {
  if (!manifestText.value.trim()) {
    toast.warning('Manifest JSON is required')
    return
  }
  if (files.value.length === 0) {
    toast.warning('Attach the image files referenced by the manifest')
    return
  }
  uploading.value = true
  try {
    await pinterestApi.createImport(manifestText.value, files.value)
    toast.success('Pinterest import created')
    manifestText.value = ''
    files.value = []
    await refresh()
  } catch (err: any) {
    toast.error('Import failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    uploading.value = false
  }
}

function formatDate(ts: string | null): string {
  if (!ts) return '-'
  return new Date(ts).toLocaleString()
}
</script>

<template>
  <div class="space-y-5">
    <PageHeader
      eyebrow="Pinterest"
      title="Imports"
      description="Create pin batches from JSON manifests and image files."
      :meta="`${imports.length} imports`"
    >
      <template #actions>
        <button class="btn-secondary btn-sm" @click="refresh">Refresh</button>
      </template>
    </PageHeader>

    <div class="grid gap-4 xl:grid-cols-[420px_1fr]">
      <SectionPanel title="Create Import">
          <div class="space-y-3">
            <label class="block text-sm text-text-secondary">
              Manifest JSON
              <input type="file" accept="application/json,.json" class="mt-2 input-field" @change="onManifestFile" />
            </label>
            <textarea
              v-model="manifestText"
              class="input-field min-h-[260px] font-mono text-xs"
              spellcheck="false"
              placeholder='{"schema_version":1,"platform":"pinterest",...}'
            />
            <label class="block text-sm text-text-secondary">
              Images
              <input type="file" accept="image/*" multiple class="mt-2 input-field" @change="onImageFiles" />
            </label>
            <div class="text-xs text-text-secondary">{{ files.length }} files selected</div>
            <button class="btn-primary w-full" :disabled="uploading" @click="submitImport">
              {{ uploading ? 'Importing...' : 'Create Import' }}
            </button>
          </div>
      </SectionPanel>

      <SectionPanel title="Import History" :count="imports.length" flush>
        <DataTable :columns="columns" :rows="imports" :loading="loading" empty-text="No Pinterest imports yet">
          <template #cell-status="{ row }"><StatusBadge :status="row.status" /></template>
          <template #cell-created_at="{ row }">{{ formatDate(row.created_at) }}</template>
        </DataTable>
      </SectionPanel>
    </div>
  </div>
</template>
