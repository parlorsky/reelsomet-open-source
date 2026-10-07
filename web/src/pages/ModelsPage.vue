<script setup lang="ts">
import { computed, ref } from 'vue'
import { modelsApi } from '@/api/endpoints'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import InspectorPanel from '@/components/ui/InspectorPanel.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import SectionPanel from '@/components/ui/SectionPanel.vue'
import type { Photo, PhotoModel } from '@/api/types'

const MODEL_MEDIA_FOLDER = 'vid_bait'

const toast = useToast()
const models = ref<PhotoModel[]>([])
const apiUnavailable = ref(false)

const selectedModel = ref<string | null>(null)
const selectedFolder = ref<string | null>(null)
const items = ref<Photo[]>([])
const itemsLoading = ref(false)
const selectedItem = ref<Photo | null>(null)

const showProfileDialog = ref(false)
const profileName = ref('')
const profileDescription = ref('')
const savingProfile = ref(false)

const showDeleteDialog = ref(false)
const deleteInfo = ref<{ model: string; folder: string; filename: string } | null>(null)

const uploading = ref(false)
const recataloging = ref(false)
const cataloging = ref(false)
const pausing = ref(false)
const textOverlaySaving = ref(false)

async function fetchData() {
  try {
    const modelsRes = await modelsApi.list()
    models.value = modelsRes.data
      .map((model) => ({
        ...model,
        folders: model.folders.filter((folder) => folder.name === MODEL_MEDIA_FOLDER),
      }))
      .filter((model) => model.folders.length > 0)
    apiUnavailable.value = false
    if (!selectedModel.value && models.value.length > 0) {
      const model = models.value[0]
      const firstFolder = model.folders.find((folder) => folder.photo_count > 0) || model.folders[0]
      if (firstFolder) {
        await selectFolder(model.name, firstFolder.name)
      }
    }
  } catch (err: any) {
    if (err.response?.status === 404 || err.response?.status === 502) {
      apiUnavailable.value = true
      models.value = []
      return
    }
    throw err
  }
}

const { loading, refresh } = usePolling(fetchData, 30000)

const isVideoFolder = computed(() => selectedFolder.value === MODEL_MEDIA_FOLDER)
const selectedModelData = computed(() => models.value.find((model) => model.name === selectedModel.value) || null)
const selectedFolderData = computed(() => selectedModelData.value?.folders.find((folder) => folder.name === selectedFolder.value) || null)
const singleFolderMode = computed(() => selectedModelData.value?.folders.length === 1)
const selectedLibraryTitle = computed(() => {
  if (!selectedModel.value) return ''
  return singleFolderMode.value ? selectedModel.value : `${selectedModel.value} / ${selectedFolder.value}`
})
const selectedLibraryDescription = computed(() => {
  const count = selectedFolderData.value?.photo_count ?? items.value.length
  return singleFolderMode.value ? `${count} video assets in the active library` : `${count} indexed assets in this folder`
})
const activeItems = computed(() => items.value.filter((item) => !item.paused).length)
const pausedItems = computed(() => items.value.filter((item) => item.paused).length)
const noTextItems = computed(() => items.value.filter((item) => item.skip_text_overlay).length)

async function selectFolder(modelName: string, folderName: string) {
  selectedModel.value = modelName
  selectedFolder.value = folderName
  selectedItem.value = null
  itemsLoading.value = true
  try {
    const res = await modelsApi.getPhotos(modelName, folderName)
    items.value = res.data.filter((item) => !item.filename.startsWith('.thumb_'))
  } catch (err: any) {
    toast.error('Failed to load: ' + (err.response?.data?.detail || err.message))
    items.value = []
  } finally {
    itemsLoading.value = false
  }
}

function selectItem(item: Photo) {
  selectedItem.value = selectedItem.value?.filename === item.filename ? null : item
}

async function uploadFiles() {
  if (!selectedModel.value || !selectedFolder.value) return
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = isVideoFolder.value ? 'video/*' : 'image/*'
  input.multiple = true
  input.onchange = async () => {
    if (!input.files?.length) return
    uploading.value = true
    let uploaded = 0
    try {
      for (const file of Array.from(input.files)) {
        await modelsApi.uploadPhoto(selectedModel.value!, selectedFolder.value!, file)
        uploaded += 1
      }
      toast.success(`Uploaded ${uploaded} file${uploaded !== 1 ? 's' : ''}`)
      await selectFolder(selectedModel.value!, selectedFolder.value!)
      await refresh()
    } catch (err: any) {
      toast.error('Upload failed: ' + (err.response?.data?.detail || err.message))
    } finally {
      uploading.value = false
    }
  }
  input.click()
}

function confirmDelete(filename: string) {
  if (!selectedModel.value || !selectedFolder.value) return
  deleteInfo.value = { model: selectedModel.value, folder: selectedFolder.value, filename }
  showDeleteDialog.value = true
}

async function deleteItem() {
  if (!deleteInfo.value) return
  try {
    await modelsApi.deletePhoto(deleteInfo.value.model, deleteInfo.value.folder, deleteInfo.value.filename)
    showDeleteDialog.value = false
    deleteInfo.value = null
    selectedItem.value = null
    toast.success('Deleted')
    if (selectedModel.value && selectedFolder.value) {
      await selectFolder(selectedModel.value, selectedFolder.value)
    }
    await refresh()
  } catch (err: any) {
    toast.error('Delete failed: ' + (err.response?.data?.detail || err.message))
  }
}

async function recatalog(name: string) {
  recataloging.value = true
  try {
    await modelsApi.recatalog(name)
    toast.success('Re-catalog complete')
    await refresh()
    if (selectedModel.value && selectedFolder.value) {
      await selectFolder(selectedModel.value, selectedFolder.value)
    }
  } catch (err: any) {
    toast.error('Failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    recataloging.value = false
  }
}

async function catalogItem() {
  if (!selectedModel.value || !selectedFolder.value || !selectedItem.value) return
  cataloging.value = true
  try {
    const res = await modelsApi.catalogItem(selectedModel.value, selectedFolder.value, selectedItem.value.filename)
    const updated = res.data as Photo
    selectedItem.value = updated
    toast.success('Cataloged')
    await selectFolder(selectedModel.value, selectedFolder.value)
    const current = items.value.find((item) => item.filename === updated.filename)
    if (current) {
      selectedItem.value = current
    }
  } catch (err: any) {
    toast.error('Catalog failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    cataloging.value = false
  }
}

async function togglePaused() {
  if (!selectedModel.value || !selectedFolder.value || !selectedItem.value) return
  const item = selectedItem.value
  const nextPaused = !item.paused
  pausing.value = true
  try {
    const res = await modelsApi.setPhotoPaused(selectedModel.value, selectedFolder.value, item.filename, nextPaused)
    const paused = res.data.paused
    const current = items.value.find((candidate) => candidate.filename === item.filename)
    if (current) {
      current.paused = paused
      selectedItem.value = current
    } else {
      selectedItem.value = { ...item, paused }
    }
    toast.success(paused ? 'File paused' : 'File resumed')
  } catch (err: any) {
    toast.error('Pause failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    pausing.value = false
  }
}

async function toggleSkipTextOverlay() {
  if (!selectedModel.value || !selectedFolder.value || !selectedItem.value) return
  const item = selectedItem.value
  const nextValue = !item.skip_text_overlay
  textOverlaySaving.value = true
  try {
    const res = await modelsApi.setSkipTextOverlay(
      selectedModel.value,
      selectedFolder.value,
      item.filename,
      nextValue,
    )
    const skipTextOverlay = res.data.skip_text_overlay
    const current = items.value.find((candidate) => candidate.filename === item.filename)
    if (current) {
      current.skip_text_overlay = skipTextOverlay
      selectedItem.value = current
    } else {
      selectedItem.value = { ...item, skip_text_overlay: skipTextOverlay }
    }
    toast.success(skipTextOverlay ? 'No text overlay enabled' : 'No text overlay disabled')
  } catch (err: any) {
    toast.error('Failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    textOverlaySaving.value = false
  }
}

function openProfile(model: PhotoModel) {
  profileName.value = model.name
  profileDescription.value = model.description || ''
  showProfileDialog.value = true
}

async function saveProfile() {
  savingProfile.value = true
  try {
    await modelsApi.updateProfile(profileName.value, { description: profileDescription.value })
    showProfileDialog.value = false
    toast.success('Saved')
    await refresh()
  } catch (err: any) {
    toast.error('Failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    savingProfile.value = false
  }
}

function totalPhotos(model: PhotoModel): number {
  return model.folders.reduce((sum, folder) => sum + folder.photo_count, 0)
}

function isVideo(filename: string): boolean {
  return /\.(mp4|mov|webm|avi|mkv)$/i.test(filename)
}

function formatDuration(seconds: number | null | undefined): string {
  if (!seconds) return ''
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`
}
</script>

<template>
  <div class="space-y-5">
    <PageHeader
      eyebrow="Content"
      title="Models"
      description="Asset library for model posting and per-video posting behavior."
      :meta="`${models.length} models`"
    >
      <template #actions>
        <button
          v-if="selectedModel"
          class="btn-secondary btn-sm"
          :disabled="recataloging"
          @click="recatalog(selectedModel)"
        >
          {{ recataloging ? 'Scanning...' : 'Re-catalog model' }}
        </button>
        <button
          v-if="selectedModel && selectedFolder"
          class="btn-primary btn-sm"
          :disabled="uploading"
          @click="uploadFiles"
        >
          {{ uploading ? 'Uploading...' : '+ Add media' }}
        </button>
      </template>
    </PageHeader>

    <SectionPanel v-if="apiUnavailable" title="Models API unavailable">
      <div class="flex items-center gap-3 text-warning">
        <span class="text-2xl">&#9888;</span>
        <p class="font-semibold">The backend endpoint for model media is not responding.</p>
      </div>
    </SectionPanel>

    <template v-else>
      <div v-if="loading && !models.length" class="surface-panel p-8 text-center text-text-secondary">
        Loading models...
      </div>

      <div v-else-if="models.length" class="rail-workspace">
        <SectionPanel title="Library" :count="models.length">
          <div class="space-y-3">
            <button
              v-for="model in models"
              :key="model.name"
              class="w-full rounded-lg border px-3 py-3 text-left transition-colors"
              :class="selectedModel === model.name
                ? 'border-accent bg-accent-muted text-text-primary'
                : 'border-border-default bg-bg-primary hover:bg-bg-hover'"
              @click="model.folders[0] && selectFolder(model.name, model.folders[0].name)"
            >
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <p class="truncate font-semibold capitalize">{{ model.name }}</p>
                  <p class="mt-0.5 text-xs text-text-secondary">{{ totalPhotos(model) }} assets</p>
                </div>
                <button
                  class="rounded-md px-2 py-1 text-xs font-semibold text-text-secondary hover:bg-bg-elevated hover:text-text-primary"
                  @click.stop="openProfile(model)"
                >
                  Edit
                </button>
              </div>
              <div v-if="selectedModel === model.name && model.folders.length > 1" class="mt-3 space-y-1">
                <button
                  v-for="folder in model.folders"
                  :key="folder.name"
                  class="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-sm transition-colors"
                  :class="selectedFolder === folder.name
                    ? 'bg-accent text-accent-fg'
                    : 'text-text-secondary hover:bg-bg-elevated hover:text-text-primary'"
                  @click.stop="selectFolder(model.name, folder.name)"
                >
                  <span class="truncate">{{ folder.name }}</span>
                  <span class="ml-2 text-xs opacity-75">{{ folder.photo_count }}</span>
                </button>
              </div>
            </button>
          </div>
        </SectionPanel>

        <SectionPanel
          v-if="selectedModel && selectedFolder"
          :title="selectedLibraryTitle"
          :description="selectedLibraryDescription"
          :count="items.length"
        >
          <template #actions>
            <span class="rounded-lg bg-success-muted px-2.5 py-1 text-xs font-semibold text-success">
              {{ activeItems }} active
            </span>
            <span class="rounded-lg bg-warning-muted px-2.5 py-1 text-xs font-semibold text-warning">
              {{ pausedItems }} paused
            </span>
            <span
              v-if="isVideoFolder"
              class="rounded-lg bg-bg-tertiary px-2.5 py-1 text-xs font-semibold text-text-secondary"
            >
              {{ noTextItems }} no text
            </span>
          </template>

          <div v-if="itemsLoading" class="flex h-64 items-center justify-center text-text-secondary">
            Loading media...
          </div>
          <EmptyState
            v-else-if="!items.length"
            title="Folder is empty"
            description="Upload media into the selected folder to make it available for posting."
          />
          <div
            v-else
            :class="isVideoFolder
              ? 'grid grid-cols-3 gap-3 sm:grid-cols-4 lg:grid-cols-5 2xl:grid-cols-6'
              : 'grid grid-cols-4 gap-2 sm:grid-cols-5 lg:grid-cols-7 2xl:grid-cols-9'"
          >
            <button
              v-for="item in items"
              :key="item.filename"
              type="button"
              class="group relative overflow-hidden rounded-lg border bg-bg-tertiary text-left transition-all"
              :class="selectedItem?.filename === item.filename
                ? 'border-accent ring-2 ring-accent/25'
                : 'border-border-subtle hover:border-border-strong'"
              @click="selectItem(item)"
            >
              <img
                :src="item.thumb_url || item.url"
                :alt="item.filename"
                loading="lazy"
                :class="[
                  isVideoFolder ? 'aspect-[9/16] w-full object-cover' : 'aspect-square w-full object-cover',
                  item.paused ? 'opacity-45 grayscale' : '',
                ]"
              />
              <div class="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/75 to-transparent p-2">
                <p class="truncate text-[11px] font-semibold text-white">{{ item.filename }}</p>
              </div>
              <div v-if="isVideo(item.filename)" class="pointer-events-none absolute left-2 top-2 rounded bg-black/70 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                Video
              </div>
              <div v-if="item.paused" class="pointer-events-none absolute right-2 top-2 rounded bg-warning px-1.5 py-0.5 text-[10px] font-semibold text-bg-primary">
                Paused
              </div>
              <div v-if="isVideoFolder && isVideo(item.filename) && item.skip_text_overlay" class="pointer-events-none absolute left-2 top-8 rounded bg-black/70 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                No text
              </div>
              <div v-if="item.duration_seconds" class="pointer-events-none absolute right-2 bottom-2 rounded bg-black/70 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                {{ formatDuration(item.duration_seconds) }}
              </div>
            </button>
          </div>
        </SectionPanel>

        <SectionPanel v-else title="Select Folder">
          <EmptyState title="No folder selected" description="Pick a model and a folder from the library rail." />
        </SectionPanel>

        <InspectorPanel
          v-if="selectedItem"
          title="Asset Inspector"
          :subtitle="selectedItem.filename"
        >
          <div class="overflow-hidden rounded-lg bg-bg-tertiary">
            <video v-if="isVideo(selectedItem.filename)" :src="selectedItem.url" class="w-full" controls preload="metadata" />
            <img v-else :src="selectedItem.url" :alt="selectedItem.filename" class="w-full" />
          </div>

          <div class="meta-grid">
            <div v-if="selectedItem.media_type" class="meta-cell">
              <span class="meta-label">Type</span>
              <span class="meta-value capitalize">{{ selectedItem.media_type }}</span>
            </div>
            <div v-if="selectedItem.duration_seconds" class="meta-cell">
              <span class="meta-label">Duration</span>
              <span class="meta-value">{{ formatDuration(selectedItem.duration_seconds) }}</span>
            </div>
            <div v-if="selectedItem.has_original_audio != null" class="meta-cell">
              <span class="meta-label">Audio</span>
              <span class="meta-value">{{ selectedItem.has_original_audio ? 'Yes' : 'No' }}</span>
            </div>
            <div class="meta-cell">
              <span class="meta-label">State</span>
              <span class="meta-value" :class="selectedItem.paused ? 'text-warning' : 'text-success'">
                {{ selectedItem.paused ? 'Paused' : 'Active' }}
              </span>
            </div>
          </div>

          <label
            v-if="isVideoFolder && isVideo(selectedItem.filename)"
            class="flex items-center justify-between rounded-lg border border-border-default bg-bg-primary px-3 py-2"
          >
            <span class="text-sm font-semibold">Post without scenario text</span>
            <input
              type="checkbox"
              class="accent-accent"
              :checked="!!selectedItem.skip_text_overlay"
              :disabled="textOverlaySaving"
              @change="toggleSkipTextOverlay"
            />
          </label>

          <div v-if="selectedItem.description">
            <p class="mb-1 text-xs font-semibold uppercase text-text-muted">Description</p>
            <p class="text-sm leading-relaxed text-text-secondary">{{ selectedItem.description }}</p>
          </div>

          <div v-if="selectedItem.tags?.length">
            <p class="mb-2 text-xs font-semibold uppercase text-text-muted">Tags</p>
            <div class="flex flex-wrap gap-1">
              <span
                v-for="tag in selectedItem.tags"
                :key="tag"
                class="rounded-md bg-bg-tertiary px-2 py-1 text-xs font-medium text-text-secondary"
              >
                {{ tag }}
              </span>
            </div>
          </div>

          <div class="grid grid-cols-2 gap-2">
            <a :href="selectedItem.url" target="_blank" class="btn-secondary btn-sm text-center">Open</a>
            <button
              class="btn-secondary btn-sm"
              :disabled="pausing"
              @click="togglePaused"
            >
              {{ pausing ? 'Saving...' : (selectedItem.paused ? 'Resume' : 'Pause') }}
            </button>
            <button class="btn-secondary btn-sm" :disabled="cataloging" @click="catalogItem">
              {{ cataloging ? 'Analyzing...' : (selectedItem.description ? 'Re-catalog' : 'Catalog') }}
            </button>
            <button class="btn-danger btn-sm" @click="confirmDelete(selectedItem.filename)">Delete</button>
          </div>
        </InspectorPanel>

        <InspectorPanel v-else title="Asset Inspector" subtitle="Nothing selected">
          <EmptyState title="Select an asset" description="Click any item in the grid to inspect metadata and posting controls." />
        </InspectorPanel>
      </div>

      <EmptyState
        v-else
        title="No models found"
        description="The media catalog is empty or still loading from the backend."
      />
    </template>

    <Teleport to="body">
      <div
        v-if="showProfileDialog"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
        @click.self="showProfileDialog = false"
      >
        <div class="card w-full max-w-md shadow-2xl">
          <h3 class="mb-4 text-lg font-semibold">Edit Profile: {{ profileName }}</h3>
          <form class="space-y-3" @submit.prevent="saveProfile">
            <div>
              <label class="mb-1 block text-sm text-text-secondary">Description</label>
              <textarea v-model="profileDescription" class="input-field resize-none" rows="4" placeholder="Model description..."></textarea>
            </div>
            <div class="flex justify-end gap-3 pt-2">
              <button type="button" class="btn-secondary" @click="showProfileDialog = false">Cancel</button>
              <button type="submit" class="btn-primary" :disabled="savingProfile">{{ savingProfile ? 'Saving...' : 'Save' }}</button>
            </div>
          </form>
        </div>
      </div>
    </Teleport>

    <ConfirmDialog
      :open="showDeleteDialog"
      title="Delete"
      message="Delete this file?"
      confirm-text="Delete"
      :confirm-danger="true"
      @confirm="deleteItem"
      @cancel="showDeleteDialog = false; deleteInfo = null"
    />
  </div>
</template>
