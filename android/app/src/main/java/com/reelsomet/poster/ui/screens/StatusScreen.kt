package com.reelsomet.poster.ui.screens

import android.content.Intent
import android.provider.Settings
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Error
import androidx.compose.material.icons.filled.ExpandLess
import androidx.compose.material.icons.filled.ExpandMore
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.reelsomet.poster.automation.InstagramAutomationService
import com.reelsomet.poster.server.VideoTransferService
import com.reelsomet.poster.util.NetworkHelper
import com.reelsomet.poster.ws.VpsConfig
import com.reelsomet.poster.ws.WebSocketClientService
import kotlinx.coroutines.delay

@Composable
fun StatusScreen() {
    val context = LocalContext.current

    // States with polling
    var a11yConnected by remember { mutableStateOf(false) }
    var serverRunning by remember { mutableStateOf(false) }
    var serverIp by remember { mutableStateOf("") }
    var vpsEnabled by remember { mutableStateOf(false) }
    var vpsDeviceId by remember { mutableStateOf(0) }
    var vpsConnected by remember { mutableStateOf(false) }
    var vpsConfig by remember { mutableStateOf(VpsConfig("", "", 0, false)) }
    var showVpsConfigDialog by remember { mutableStateOf(false) }

    // Poll status every 2 seconds
    LaunchedEffect(Unit) {
        while (true) {
            a11yConnected = InstagramAutomationService.instance != null
            serverRunning = VideoTransferService.isRunning
            serverIp = NetworkHelper.getWifiIpAddress(context).ifEmpty { "N/A" }
            val config = VpsConfig.loadOrImport(context)
            vpsConfig = config
            vpsEnabled = config.enabled && config.isValid()
            vpsDeviceId = config.deviceId
            vpsConnected = WebSocketClientService.isConnected
            delay(2000)
        }
    }

    if (showVpsConfigDialog) {
        VpsConfigDialog(
            initialConfig = vpsConfig,
            onDismiss = { showVpsConfigDialog = false },
            onSave = { config ->
                VpsConfig.save(context, config)
                vpsConfig = config
                vpsEnabled = true
                vpsDeviceId = config.deviceId
                showVpsConfigDialog = false
                WebSocketClientService.reconnect(context)
            }
        )
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        // Title
        Text(
            "Reelsomet",
            style = MaterialTheme.typography.headlineLarge,
            fontWeight = FontWeight.Bold
        )

        Spacer(modifier = Modifier.height(8.dp))

        // Accessibility Service Card
        StatusCard(
            title = "Accessibility Service",
            isOk = a11yConnected,
            statusText = if (a11yConnected) "Connected" else "Not Connected",
            actionButton = {
                Button(
                    onClick = {
                        context.startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
                    }
                ) {
                    Text(if (a11yConnected) "Open Settings" else "Enable Service")
                }
            }
        )

        // HTTP Server Card
        StatusCard(
            title = "HTTP Server",
            isOk = serverRunning,
            statusText = if (serverRunning) "Running at $serverIp:8080" else "Stopped",
            actionButton = {
                if (serverRunning) {
                    OutlinedButton(
                        onClick = {
                            context.stopService(Intent(context, VideoTransferService::class.java))
                        }
                    ) {
                        Text("Stop Server")
                    }
                } else {
                    Button(
                        onClick = {
                            val intent = Intent(context, VideoTransferService::class.java)
                            context.startForegroundService(intent)
                        }
                    ) {
                        Text("Start Server")
                    }
                }
            }
        )

        StatusCard(
            title = "VPS WebSocket",
            isOk = vpsConnected,
            statusText = when {
                vpsConnected -> "Connected as device #$vpsDeviceId"
                vpsEnabled -> "Configured as device #$vpsDeviceId"
                else -> "Not configured"
            },
            actionButton = {
                if (vpsConnected) {
                    OutlinedButton(
                        onClick = { WebSocketClientService.stop(context) }
                    ) {
                        Text("Stop VPS")
                    }
                } else {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(
                            onClick = {
                                if (vpsEnabled) {
                                    WebSocketClientService.reconnect(context)
                                } else {
                                    showVpsConfigDialog = true
                                }
                            }
                        ) {
                            Text(if (vpsEnabled) "Connect VPS" else "Configure VPS")
                        }
                        if (vpsEnabled) {
                            OutlinedButton(onClick = { showVpsConfigDialog = true }) {
                                Text("Edit")
                            }
                        }
                    }
                }
            }
        )

        // Xiaomi/MIUI Setup Card (expandable)
        XiaomiSetupCard()

        // Battery Optimization Card
        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(
                containerColor = MaterialTheme.colorScheme.surfaceVariant
            )
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Text(
                    "Battery Optimization",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold
                )
                Spacer(Modifier.height(8.dp))
                Text(
                    "Disable battery optimization to prevent Android from killing the app.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Spacer(Modifier.height(12.dp))
                OutlinedButton(
                    onClick = {
                        context.startActivity(
                            Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
                        )
                    }
                ) {
                    Text("Open Battery Settings")
                }
            }
        }
    }
}

@Composable
private fun VpsConfigDialog(
    initialConfig: VpsConfig,
    onDismiss: () -> Unit,
    onSave: (VpsConfig) -> Unit
) {
    var serverUrl by remember(initialConfig) {
        mutableStateOf(initialConfig.serverUrl.ifBlank { VpsConfig.DEFAULT_SERVER_URL })
    }
    var deviceToken by remember(initialConfig) { mutableStateOf(initialConfig.deviceToken) }
    var deviceId by remember(initialConfig) {
        mutableStateOf(if (initialConfig.deviceId > 0) initialConfig.deviceId.toString() else "1")
    }
    var showError by remember { mutableStateOf(false) }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("VPS Connection") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                OutlinedTextField(
                    value = serverUrl,
                    onValueChange = {
                        serverUrl = it
                        showError = false
                    },
                    label = { Text("Server URL") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )
                OutlinedTextField(
                    value = deviceToken,
                    onValueChange = {
                        deviceToken = it
                        showError = false
                    },
                    label = { Text("Device Token") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )
                OutlinedTextField(
                    value = deviceId,
                    onValueChange = {
                        deviceId = it
                        showError = false
                    },
                    label = { Text("Device ID") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )
                if (showError) {
                    Text(
                        "Enter server URL, device token, and device ID greater than 0.",
                        color = MaterialTheme.colorScheme.error,
                        style = MaterialTheme.typography.bodySmall
                    )
                }
            }
        },
        confirmButton = {
            Button(
                onClick = {
                    val config = VpsConfig.fromSetupFields(
                        serverUrl = serverUrl,
                        deviceToken = deviceToken,
                        deviceIdText = deviceId
                    )
                    if (config == null) {
                        showError = true
                    } else {
                        onSave(config)
                    }
                }
            ) {
                Text("Save & Connect")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("Cancel")
            }
        }
    )
}

@Composable
private fun StatusCard(
    title: String,
    isOk: Boolean,
    statusText: String,
    actionButton: @Composable () -> Unit
) {
    val containerColor = if (isOk) {
        Color(0xFF1B5E20).copy(alpha = 0.1f) // Green tint
    } else {
        Color(0xFFB71C1C).copy(alpha = 0.1f) // Red tint
    }

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = containerColor)
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                Icon(
                    imageVector = if (isOk) Icons.Default.CheckCircle else Icons.Default.Error,
                    contentDescription = null,
                    tint = if (isOk) Color(0xFF4CAF50) else Color(0xFFF44336)
                )
                Text(
                    title,
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold
                )
            }

            Spacer(Modifier.height(8.dp))

            Text(
                statusText,
                style = MaterialTheme.typography.bodyMedium,
                color = if (isOk) Color(0xFF4CAF50) else Color(0xFFF44336)
            )

            Spacer(Modifier.height(12.dp))

            actionButton()
        }
    }
}

@Composable
private fun XiaomiSetupCard() {
    var expanded by remember { mutableStateOf(false) }

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant
        )
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    "Xiaomi/MIUI Setup",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold
                )
                IconButton(onClick = { expanded = !expanded }) {
                    Icon(
                        imageVector = if (expanded) Icons.Default.ExpandLess else Icons.Default.ExpandMore,
                        contentDescription = if (expanded) "Collapse" else "Expand"
                    )
                }
            }

            AnimatedVisibility(visible = expanded) {
                Column(
                    modifier = Modifier.padding(top = 8.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp)
                ) {
                    val steps = listOf(
                        "1. Settings > Apps > Reelsomet > Autostart > ON",
                        "2. Settings > Battery > Reelsomet > No restrictions",
                        "3. Security > Battery > Add Reelsomet to exceptions",
                        "4. Lock app in Recent Apps (swipe down on card)",
                        "5. Keep phone charging 24/7",
                        "6. Disable Instagram auto-update in Play Store"
                    )
                    steps.forEach { step ->
                        Text(
                            step,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
            }

            if (!expanded) {
                Text(
                    "Tap to see setup instructions for Xiaomi devices",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
    }
}
