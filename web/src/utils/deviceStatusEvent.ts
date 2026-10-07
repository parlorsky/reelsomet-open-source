import type { Device } from '@/api/types'

type DeviceLiveFields = Pick<Device, 'model' | 'battery' | 'active_mode' | 'accessibility_connected'>

function firstString(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) {
      return value
    }
  }
  return null
}

function firstNumber(...values: unknown[]): number | null {
  for (const value of values) {
    if (typeof value === 'number' && Number.isFinite(value)) {
      return value
    }
  }
  return null
}

function firstBoolean(...values: unknown[]): boolean | null {
  for (const value of values) {
    if (typeof value === 'boolean') {
      return value
    }
  }
  return null
}

function normalizeActiveMode(value: string | null): Device['active_mode'] | null {
  switch (value) {
    case 'POSTING':
    case 'ENGAGEMENT':
    case 'INSIGHTS':
    case 'MONITORING':
    case 'NONE':
      return value
    default:
      return null
  }
}

export function parseDeviceStatusEvent(data: Record<string, unknown>): Partial<DeviceLiveFields> {
  const update: Partial<DeviceLiveFields> = {}

  const model = firstString(data.model, data.device_model)
  if (model !== null) {
    update.model = model
  }

  const battery = firstNumber(data.battery, data.battery_level, data.batteryLevel)
  if (battery !== null) {
    update.battery = battery
  }

  const activeMode = normalizeActiveMode(firstString(data.active_mode, data.activeMode))
  if (activeMode !== null) {
    update.active_mode = activeMode
  }

  const accessibilityConnected = firstBoolean(
    data.accessibility_connected,
    data.accessibilityConnected,
  )
  if (accessibilityConnected !== null) {
    update.accessibility_connected = accessibilityConnected
  }

  return update
}
