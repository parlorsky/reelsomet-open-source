<script setup lang="ts">
import { computed, ref } from 'vue'
import { scenariosApi } from '@/api/endpoints'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import DataTable from '@/components/ui/DataTable.vue'
import type { Column } from '@/components/ui/DataTable.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import SectionPanel from '@/components/ui/SectionPanel.vue'
import type { Scenario, ScenarioCreate } from '@/api/types'

const toast = useToast()
const scenarios = ref<Scenario[]>([])
const searchQuery = ref('')
const apiUnavailable = ref(false)

const showAddDialog = ref(false)
const showEditDialog = ref(false)
const showDeleteDialog = ref(false)
const deleteShortcode = ref<string | null>(null)
const saving = ref(false)

const formData = ref<ScenarioCreate>({
  shortcode: '',
  text: '',
  caption: '',
  source: '',
})

const editOriginalShortcode = ref('')

const columns: Column[] = [
  { key: 'shortcode', label: 'Shortcode', sortable: true },
  { key: 'text', label: 'Text Preview' },
  { key: 'source', label: 'Source', sortable: true },
  { key: 'has_text', label: 'Has Text' },
  { key: 'caption', label: 'Caption Preview' },
  { key: 'used_count', label: 'Used', sortable: true },
  { key: 'actions', label: 'Actions', width: '180px' },
]

const filteredScenarios = computed(() => {
  if (!searchQuery.value.trim()) return scenarios.value
  const query = searchQuery.value.toLowerCase()
  return scenarios.value.filter(
    (scenario) =>
      scenario.shortcode.toLowerCase().includes(query) ||
      scenario.text.toLowerCase().includes(query),
  )
})

async function fetchData() {
  try {
    const res = await scenariosApi.list()
    scenarios.value = res.data
    apiUnavailable.value = false
  } catch (err: any) {
    if (err.response?.status === 404 || err.response?.status === 502) {
      apiUnavailable.value = true
      scenarios.value = []
      return
    }
    throw err
  }
}

const { loading, refresh } = usePolling(fetchData, 30000)

function openAddDialog() {
  formData.value = { shortcode: '', text: '', caption: '', source: '' }
  showAddDialog.value = true
}

function openEditDialog(scenario: Scenario) {
  editOriginalShortcode.value = scenario.shortcode
  formData.value = {
    shortcode: scenario.shortcode,
    text: scenario.text,
    caption: scenario.caption,
    source: scenario.source,
  }
  showEditDialog.value = true
}

async function submitAdd() {
  if (!formData.value.shortcode.trim()) {
    toast.warning('Shortcode is required')
    return
  }
  saving.value = true
  try {
    await scenariosApi.create(formData.value)
    showAddDialog.value = false
    toast.success('Scenario created')
    await refresh()
  } catch (err: any) {
    toast.error('Failed to create: ' + (err.response?.data?.detail || err.message))
  } finally {
    saving.value = false
  }
}

async function submitEdit() {
  if (!formData.value.shortcode.trim()) {
    toast.warning('Shortcode is required')
    return
  }
  saving.value = true
  try {
    await scenariosApi.update(editOriginalShortcode.value, formData.value)
    showEditDialog.value = false
    toast.success('Scenario updated')
    await refresh()
  } catch (err: any) {
    toast.error('Failed to update: ' + (err.response?.data?.detail || err.message))
  } finally {
    saving.value = false
  }
}

function confirmDelete(shortcode: string) {
  deleteShortcode.value = shortcode
  showDeleteDialog.value = true
}

async function deleteScenario() {
  if (!deleteShortcode.value) return
  try {
    await scenariosApi.delete(deleteShortcode.value)
    showDeleteDialog.value = false
    deleteShortcode.value = null
    toast.success('Scenario deleted')
    await refresh()
  } catch (err: any) {
    toast.error('Failed to delete: ' + (err.response?.data?.detail || err.message))
  }
}

async function importFromFile() {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.json'
  input.onchange = async () => {
    if (!input.files || input.files.length === 0) return
    try {
      const res = await scenariosApi.importFile(input.files[0])
      toast.success(`Imported ${res.data.imported} scenarios`)
      await refresh()
    } catch (err: any) {
      toast.error('Import failed: ' + (err.response?.data?.detail || err.message))
    }
  }
  input.click()
}

async function exportToJson() {
  try {
    const res = await scenariosApi.exportJson()
    const blob = new Blob([JSON.stringify(res.data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'scenarios.json'
    anchor.click()
    URL.revokeObjectURL(url)
    toast.success('Export downloaded')
  } catch (err: any) {
    toast.error('Export failed: ' + (err.response?.data?.detail || err.message))
  }
}

function truncate(text: string, len: number): string {
  return text.length > len ? text.slice(0, len) + '...' : text
}
</script>

<template>
  <div class="space-y-5">
    <PageHeader
      eyebrow="Content"
      title="Scenarios"
      description="Reusable text and caption templates used by the posting pipeline."
      :meta="`${filteredScenarios.length} visible / ${scenarios.length} total`"
    >
      <template #actions>
        <button class="btn-secondary btn-sm" @click="importFromFile">Import JSON</button>
        <button class="btn-secondary btn-sm" @click="exportToJson">Export JSON</button>
        <button class="btn-primary" @click="openAddDialog">+ Add Scenario</button>
      </template>
    </PageHeader>

    <div v-if="apiUnavailable" class="card flex items-center gap-3 border border-warning/30">
      <span class="text-2xl">&#9888;</span>
      <div>
        <p class="font-semibold text-warning">Scenarios API not configured</p>
        <p class="text-sm text-text-secondary">The `/api/scenarios` endpoint is not available.</p>
      </div>
    </div>

    <template v-else>
      <div class="toolbar">
        <div class="filter-row">
          <input v-model="searchQuery" class="input-field w-72" placeholder="Search by shortcode or text..." />
          <span class="text-sm text-text-secondary">{{ filteredScenarios.length }} scenarios</span>
        </div>
      </div>

      <SectionPanel title="Scenario Library" :count="filteredScenarios.length" flush>
        <DataTable :columns="columns" :rows="filteredScenarios" :loading="loading" empty-text="No scenarios found">
          <template #cell-text="{ row }">
            <span :title="row.text">{{ truncate(row.text || '', 80) }}</span>
          </template>
          <template #cell-has_text="{ row }">
            <span class="text-lg" :class="row.has_text ? 'text-success' : 'text-danger'">&#9679;</span>
          </template>
          <template #cell-caption="{ row }">
            <span :title="row.caption">{{ truncate(row.caption || '', 40) }}</span>
          </template>
          <template #cell-actions="{ row }">
            <div class="flex gap-2">
              <button class="btn-secondary btn-sm" @click="openEditDialog(row as Scenario)">Edit</button>
              <button class="btn-danger btn-sm" @click="confirmDelete(row.shortcode)">Delete</button>
            </div>
          </template>
        </DataTable>
      </SectionPanel>
    </template>

    <Teleport to="body">
      <div
        v-if="showAddDialog"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
        @click.self="showAddDialog = false"
      >
        <div class="card w-full max-w-lg shadow-2xl">
          <h3 class="mb-4 text-lg font-semibold">Add Scenario</h3>
          <form class="space-y-3" @submit.prevent="submitAdd">
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Shortcode</label>
              <input v-model="formData.shortcode" class="input-field" placeholder="e.g. love_story_01" />
            </div>
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Text</label>
              <textarea v-model="formData.text" class="input-field resize-none" rows="4" placeholder="Scenario text..."></textarea>
            </div>
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Caption</label>
              <textarea v-model="formData.caption" class="input-field resize-none" rows="2" placeholder="Instagram caption..."></textarea>
            </div>
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Source</label>
              <input v-model="formData.source" class="input-field" placeholder="e.g. manual, gpt, imported" />
            </div>
            <div class="flex justify-end gap-3 pt-2">
              <button type="button" class="btn-secondary" @click="showAddDialog = false">Cancel</button>
              <button type="submit" class="btn-primary" :disabled="saving">{{ saving ? 'Creating...' : 'Create' }}</button>
            </div>
          </form>
        </div>
      </div>
    </Teleport>

    <Teleport to="body">
      <div
        v-if="showEditDialog"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
        @click.self="showEditDialog = false"
      >
        <div class="card w-full max-w-lg shadow-2xl">
          <h3 class="mb-4 text-lg font-semibold">Edit Scenario</h3>
          <form class="space-y-3" @submit.prevent="submitEdit">
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Shortcode</label>
              <input v-model="formData.shortcode" class="input-field" placeholder="e.g. love_story_01" />
            </div>
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Text</label>
              <textarea v-model="formData.text" class="input-field resize-none" rows="4" placeholder="Scenario text..."></textarea>
            </div>
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Caption</label>
              <textarea v-model="formData.caption" class="input-field resize-none" rows="2" placeholder="Instagram caption..."></textarea>
            </div>
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Source</label>
              <input v-model="formData.source" class="input-field" placeholder="e.g. manual, gpt, imported" />
            </div>
            <div class="flex justify-end gap-3 pt-2">
              <button type="button" class="btn-secondary" @click="showEditDialog = false">Cancel</button>
              <button type="submit" class="btn-primary" :disabled="saving">{{ saving ? 'Saving...' : 'Save' }}</button>
            </div>
          </form>
        </div>
      </div>
    </Teleport>

    <ConfirmDialog
      :open="showDeleteDialog"
      title="Delete Scenario"
      message="Are you sure you want to delete this scenario? This action cannot be undone."
      confirm-text="Delete"
      :confirm-danger="true"
      @confirm="deleteScenario"
      @cancel="showDeleteDialog = false; deleteShortcode = null"
    />
  </div>
</template>
