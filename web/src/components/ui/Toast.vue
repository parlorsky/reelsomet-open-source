<script setup lang="ts">
import { useToast } from '@/composables/useToast'

const { toasts, remove } = useToast()
</script>

<template>
  <Teleport to="body">
    <div class="fixed right-4 top-4 z-[100] flex flex-col gap-2">
      <Transition
        v-for="toast in toasts"
        :key="toast.id"
        enter-active-class="transition duration-300 ease-out"
        enter-from-class="translate-x-full opacity-0"
        enter-to-class="translate-x-0 opacity-100"
        leave-active-class="transition duration-200 ease-in"
        leave-from-class="translate-x-0 opacity-100"
        leave-to-class="translate-x-full opacity-0"
      >
        <div
          class="flex min-w-[300px] items-start gap-3 rounded-lg px-4 py-3 shadow-lg"
          :class="{
            'bg-success/90 text-white': toast.type === 'success',
            'bg-danger/90 text-white': toast.type === 'error',
            'bg-warning/90 text-bg-primary': toast.type === 'warning',
            'bg-accent/90 text-bg-primary': toast.type === 'info',
          }"
        >
          <span class="text-lg leading-none">
            {{ toast.type === 'success' ? '\u2713' : toast.type === 'error' ? '\u2717' : toast.type === 'warning' ? '\u26A0' : '\u2139' }}
          </span>
          <span class="flex-1 text-sm">{{ toast.message }}</span>
          <button class="text-lg leading-none opacity-70 hover:opacity-100" @click="remove(toast.id)">&#215;</button>
        </div>
      </Transition>
    </div>
  </Teleport>
</template>
