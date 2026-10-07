<script setup lang="ts">
defineProps<{
  open: boolean
  title: string
  message: string
  confirmText?: string
  confirmDanger?: boolean
}>()

const emit = defineEmits<{
  confirm: []
  cancel: []
}>()
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      @click.self="emit('cancel')"
    >
      <div class="card w-full max-w-md shadow-2xl">
        <h3 class="mb-2 text-lg font-semibold">{{ title }}</h3>
        <p class="mb-6 text-text-secondary">{{ message }}</p>
        <div class="flex justify-end gap-3">
          <button class="btn-secondary" @click="emit('cancel')">Cancel</button>
          <button
            :class="confirmDanger ? 'btn-danger' : 'btn-primary'"
            @click="emit('confirm')"
          >
            {{ confirmText || 'Confirm' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
