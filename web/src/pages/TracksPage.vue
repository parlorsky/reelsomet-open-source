<script setup lang="ts">
import { ref } from 'vue'
import { tracksApi } from '@/api/endpoints'
import { usePolling } from '@/composables/usePolling'
import { useToast } from '@/composables/useToast'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import InspectorPanel from '@/components/ui/InspectorPanel.vue'
import PageHeader from '@/components/ui/PageHeader.vue'
import SectionPanel from '@/components/ui/SectionPanel.vue'
import type { Track, TrackMetadataUpdate } from '@/api/types'

const toast = useToast()
const tracks = ref<Track[]>([])
const selectedTrack = ref<Track | null>(null)
const apiUnavailable = ref(false)
const uploading = ref(false)
const savingMeta = ref(false)
const deleteFilename = ref<string | null>(null)
const showDeleteDialog = ref(false)

const editType = ref<'simple' | 'drop'>('simple')
const editIntroDuration = ref<number | null>(null)
const editBeatInterval = ref<number | null>(null)

async function fetchData() {
  try {
    const res = await tracksApi.list()
    tracks.value = res.data
    apiUnavailable.value = false
    if (selectedTrack.value) {
      const updated = res.data.find((track) => track.filename === selectedTrack.value?.filename)
      selectedTrack.value = updated ?? null
    }
  } catch (err: any) {
    if (err.response?.status === 404 || err.response?.status === 502) {
      apiUnavailable.value = true
      tracks.value = []
      return
    }
    throw err
  }
}

const { loading, refresh } = usePolling(fetchData, 30000)

function selectTrack(track: Track) {
  selectedTrack.value = track
  editType.value = track.type
  editIntroDuration.value = track.intro_duration
  editBeatInterval.value = track.beat_interval
}

function formatDuration(seconds: number): string {
  const minutes = Math.floor(seconds / 60)
  const secs = Math.floor(seconds % 60)
  return `${minutes}:${secs.toString().padStart(2, '0')}`
}

async function uploadTrack() {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.wav,.mp3'
  input.onchange = async () => {
    if (!input.files || input.files.length === 0) return
    uploading.value = true
    try {
      await tracksApi.upload(input.files[0])
      toast.success('Track uploaded')
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
  deleteFilename.value = filename
  showDeleteDialog.value = true
}

async function deleteTrack() {
  if (!deleteFilename.value) return
  try {
    await tracksApi.delete(deleteFilename.value)
    if (selectedTrack.value?.filename === deleteFilename.value) {
      selectedTrack.value = null
    }
    showDeleteDialog.value = false
    deleteFilename.value = null
    toast.success('Track deleted')
    await refresh()
  } catch (err: any) {
    toast.error('Delete failed: ' + (err.response?.data?.detail || err.message))
  }
}

async function saveMetadata() {
  if (!selectedTrack.value) return
  savingMeta.value = true
  try {
    const data: TrackMetadataUpdate = {
      type: editType.value,
      intro_duration: editType.value === 'drop' ? editIntroDuration.value : null,
      beat_interval: editBeatInterval.value,
    }
    await tracksApi.updateMetadata(selectedTrack.value.filename, data)
    toast.success('Metadata saved')
    await refresh()
  } catch (err: any) {
    toast.error('Save failed: ' + (err.response?.data?.detail || err.message))
  } finally {
    savingMeta.value = false
  }
}

function getAudioUrl(filename: string): string {
  return tracksApi.audioUrl(filename)
}
</script>

<template>
  <div class="space-y-5">
    <PageHeader
      eyebrow="Content"
      title="Tracks"
      description="Audio library with preview, drop timing, beat interval metadata, and usage counts."
      :meta="`${tracks.length} tracks`"
    >
      <template #actions>
        <button class="btn-primary" :disabled="uploading" @click="uploadTrack">
          {{ uploading ? 'Uploading...' : '+ Upload Track' }}
        </button>
      </template>
    </PageHeader>

    <div v-if="apiUnavailable" class="card flex items-center gap-3 border border-warning/30">
      <span class="text-2xl">&#9888;</span>
      <div>
        <p class="font-semibold text-warning">Tracks API not configured</p>
        <p class="text-sm text-text-secondary">The `/api/tracks` endpoint is not available.</p>
      </div>
    </div>

    <template v-else>
      <div class="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <SectionPanel class="lg:col-span-2" title="Track Library" :count="tracks.length" flush>
          <div class="overflow-x-auto">
            <table class="w-full text-left text-sm">
              <thead>
                <tr class="border-b border-bg-tertiary text-text-secondary">
                  <th class="px-4 py-3 font-medium">Filename</th>
                  <th class="px-4 py-3 font-medium">Type</th>
                  <th class="px-4 py-3 font-medium">Duration</th>
                  <th class="px-4 py-3 font-medium">Used</th>
                  <th class="px-4 py-3 font-medium" style="width: 120px">Actions</th>
                </tr>
              </thead>
              <tbody>
                <tr v-if="loading">
                  <td colspan="5" class="px-4 py-8 text-center text-text-secondary">Loading...</td>
                </tr>
                <tr v-else-if="tracks.length === 0">
                  <td colspan="5" class="px-4 py-8 text-center text-text-secondary">No tracks in library</td>
                </tr>
                <tr
                  v-for="track in tracks"
                  :key="track.filename"
                  class="table-row-hover cursor-pointer border-b border-bg-tertiary/50"
                  :class="selectedTrack?.filename === track.filename ? 'bg-accent/10' : ''"
                  @click="selectTrack(track)"
                >
                  <td class="px-4 py-3 font-mono text-sm">{{ track.filename }}</td>
                  <td class="px-4 py-3">
                    <span
                      class="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium"
                      :class="track.type === 'drop' ? 'bg-accent/20 text-accent' : 'bg-text-secondary/20 text-text-secondary'"
                    >
                      {{ track.type }}
                    </span>
                  </td>
                  <td class="px-4 py-3">{{ formatDuration(track.duration) }}</td>
                  <td class="px-4 py-3">{{ track.used_count }}</td>
                  <td class="px-4 py-3">
                    <button class="btn-danger btn-sm" @click.stop="confirmDelete(track.filename)">Delete</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </SectionPanel>

        <InspectorPanel v-if="selectedTrack" title="Track Details" :subtitle="selectedTrack.filename">
          <template v-if="selectedTrack">
            <div class="mb-4">
              <label class="mb-1 block text-sm text-text-secondary">Preview</label>
              <audio controls class="w-full" :src="getAudioUrl(selectedTrack.filename)" preload="none">
                Your browser does not support the audio element.
              </audio>
            </div>

            <form class="space-y-3" @submit.prevent="saveMetadata">
              <div>
                <label class="mb-1 block text-sm text-text-secondary">Type</label>
                <select v-model="editType" class="select-field w-full">
                  <option value="simple">Simple</option>
                  <option value="drop">Drop</option>
                </select>
              </div>
              <div v-if="editType === 'drop'">
                <label class="mb-1 block text-sm text-text-secondary">Intro Duration (sec)</label>
                <input v-model.number="editIntroDuration" type="number" step="0.1" min="0" class="input-field" placeholder="e.g. 3.5" />
              </div>
              <div>
                <label class="mb-1 block text-sm text-text-secondary">Beat Interval (sec)</label>
                <input v-model.number="editBeatInterval" type="number" step="0.01" min="0" class="input-field" placeholder="e.g. 0.5" />
              </div>
              <div class="pt-2">
                <button type="submit" class="btn-primary w-full" :disabled="savingMeta">
                  {{ savingMeta ? 'Saving...' : 'Save Metadata' }}
                </button>
              </div>
            </form>
          </template>
        </InspectorPanel>
        <InspectorPanel v-else title="Track Details" subtitle="Nothing selected">
          <EmptyState title="Select a track" description="Click any row to preview audio and edit timing metadata." />
        </InspectorPanel>
      </div>
    </template>

    <ConfirmDialog
      :open="showDeleteDialog"
      title="Delete Track"
      message="Are you sure you want to delete this track? This action cannot be undone."
      confirm-text="Delete"
      :confirm-danger="true"
      @confirm="deleteTrack"
      @cancel="showDeleteDialog = false; deleteFilename = null"
    />
  </div>
</template>
