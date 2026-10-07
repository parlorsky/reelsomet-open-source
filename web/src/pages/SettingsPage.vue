<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { settingsApi } from '@/api/endpoints'
import { useToast } from '@/composables/useToast'
import PageHeader from '@/components/ui/PageHeader.vue'
import type { AppSettings, SettingsSection } from '@/api/types'

const toast = useToast()
const loading = ref(false)
const activeTab = ref<'general' | 'llm' | 'farm'>('general')
const saving = ref<string | null>(null)

const settings = ref<AppSettings>({
  general: {},
  llm: {},
  farm: {},
})

const tabs = [
  { key: 'general' as const, label: 'General' },
  { key: 'llm' as const, label: 'LLM' },
  { key: 'farm' as const, label: 'Farm / TG Bot' },
]

interface FieldDef {
  key: string
  label: string
  type: 'text' | 'number' | 'boolean' | 'password' | 'textarea'
  description?: string
}

const fieldDefs: Record<string, FieldDef[]> = {
  general: [],
  llm: [
    { key: 'base_url', label: 'Base URL', type: 'text', description: 'OpenAI-compatible API base URL' },
    { key: 'api_key', label: 'API Key', type: 'password', description: 'API key for the LLM provider' },
    { key: 'model', label: 'Model', type: 'text', description: 'Model name (e.g., gpt-4, deepseek-chat)' },
    { key: 'timeout', label: 'Timeout (seconds)', type: 'number', description: 'Request timeout in seconds' },
  ],
  farm: [
    { key: 'telegram_bot_token', label: 'Telegram Bot Token', type: 'password', description: 'Telegram bot token for notifications and commands' },
    { key: 'telegram_admin_chat_ids', label: 'Admin Chat IDs', type: 'text', description: 'Comma-separated list of Telegram chat IDs' },
    { key: 'timezone', label: 'Farm Timezone', type: 'text', description: 'IANA timezone for posting slots, e.g. Europe/Moscow' },
    { key: 'posting_interval_minutes', label: 'Posting Interval (min)', type: 'number', description: 'Minimum minutes between posts per account' },
    { key: 'max_auto_retries', label: 'Max Auto Retries', type: 'number', description: 'Maximum automatic retries for failed posts' },
    { key: 'auto_retry_delay_minutes', label: 'Retry Delay (min)', type: 'number', description: 'Minutes to wait before auto-retry' },
    { key: 'action_blocked_pause_hours', label: 'Block Pause (hours)', type: 'number', description: 'Hours to pause after action-block detected' },
  ],
}

onMounted(async () => {
  loading.value = true
  try {
    const res = await settingsApi.get()
    settings.value = res.data
  } catch (err: any) {
    toast.error('Failed to load settings: ' + (err.response?.data?.detail || err.message))
  } finally {
    loading.value = false
  }

})

async function saveSetting(section: string, key: string) {
  const value = settings.value[section as keyof AppSettings]?.[key]
  saving.value = `${section}.${key}`
  try {
    const res = await settingsApi.update(section, key, value ?? null)
    setFieldValue(section, key, res.data.value)
    toast.success(`Saved ${key}`)
  } catch (err: any) {
    toast.error(`Failed to save ${key}: ${err.response?.data?.detail || err.message}`)
  } finally {
    saving.value = null
  }
}

function getFieldValue(section: string, key: string): any {
  return settings.value[section as keyof AppSettings]?.[key] ?? ''
}

function setFieldValue(section: string, key: string, value: any) {
  if (!settings.value[section as keyof AppSettings]) {
    (settings.value as any)[section] = {}
  }
  (settings.value[section as keyof AppSettings] as SettingsSection)[key] = value
}

function setNumericFieldValue(section: string, key: string, rawValue: string) {
  const trimmed = rawValue.trim()
  if (!trimmed) {
    setFieldValue(section, key, null)
    return
  }

  const parsed = Number(trimmed)
  setFieldValue(section, key, Number.isFinite(parsed) ? parsed : null)
}
</script>

<template>
  <div class="space-y-6">
    <PageHeader
      eyebrow="System"
      title="Settings"
      description="Runtime configuration, LLM credentials, farm cadence, Telegram notifications, and publishing cadence."
      :meta="activeTab"
    />

    <!-- Tabs -->
    <div class="flex gap-1 border-b border-bg-tertiary">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        @click="activeTab = tab.key"
        :class="[
          'px-4 py-2.5 text-sm font-medium transition-colors',
          activeTab === tab.key
            ? 'border-b-2 border-accent text-accent'
            : 'text-text-secondary hover:text-text-primary',
        ]"
      >
        {{ tab.label }}
        <!-- License status dot -->

      </button>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="card py-8 text-center text-text-secondary">
      Loading settings...
    </div>

    <!-- License tab -->
    <!-- Settings fields -->
    <div v-else-if="activeTab === 'general'" class="space-y-4">
      <div class="card space-y-3">
        <h3 class="text-base font-semibold text-text-primary">Account-Level Automation</h3>
        <p class="text-sm text-text-secondary">
          Posting, engagement, and insights switches are configured per account on the Accounts page.
          The VPS does not have persisted global automation toggles for those features.
        </p>
        <p class="text-sm text-text-secondary">
          Monitoring cadence is also not a saved server setting here. Use the Monitor page to manage targets,
          and use the Farm tab for real persisted VPS runtime settings.
        </p>
      </div>
    </div>

    <div v-else class="space-y-4">
      <div
        v-for="field in fieldDefs[activeTab]"
        :key="field.key"
        class="card flex items-start gap-4"
      >
        <div class="min-w-0 flex-1">
          <label class="mb-1 block text-sm font-medium">{{ field.label }}</label>
          <p v-if="field.description" class="mb-2 text-xs text-text-secondary">{{ field.description }}</p>

          <div v-if="field.type === 'boolean'" class="flex items-center gap-2">
            <input
              type="checkbox"
              :checked="!!getFieldValue(activeTab, field.key)"
              @change="setFieldValue(activeTab, field.key, ($event.target as HTMLInputElement).checked)"
              class="accent-accent"
            />
            <span class="text-sm">{{ getFieldValue(activeTab, field.key) ? 'Enabled' : 'Disabled' }}</span>
          </div>

          <textarea
            v-else-if="field.type === 'textarea'"
            :value="getFieldValue(activeTab, field.key)"
            @input="setFieldValue(activeTab, field.key, ($event.target as HTMLTextAreaElement).value)"
            class="input-field resize-none"
            rows="3"
          ></textarea>

          <input
            v-else-if="field.type === 'number'"
            type="number"
            :value="getFieldValue(activeTab, field.key)"
            @input="setNumericFieldValue(activeTab, field.key, ($event.target as HTMLInputElement).value)"
            class="input-field w-48"
          />

          <input
            v-else
            :type="field.type === 'password' ? 'password' : 'text'"
            :value="getFieldValue(activeTab, field.key)"
            @input="setFieldValue(activeTab, field.key, ($event.target as HTMLInputElement).value)"
            class="input-field"
          />
        </div>

        <button
          class="btn-primary btn-sm mt-6 shrink-0"
          :disabled="saving === `${activeTab}.${field.key}`"
          @click="saveSetting(activeTab, field.key)"
        >
          {{ saving === `${activeTab}.${field.key}` ? 'Saving...' : 'Save' }}
        </button>
      </div>
    </div>
  </div>
</template>
