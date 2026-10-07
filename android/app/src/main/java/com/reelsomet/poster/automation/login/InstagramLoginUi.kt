package com.reelsomet.poster.automation.login

import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo
import com.reelsomet.poster.automation.ui_elements.UiElementFinder
import java.security.MessageDigest

object InstagramLoginUi {
    const val PACKAGE = "com.instagram.android"

    data class LoginFields(
        val username: AccessibilityNodeInfo?,
        val password: AccessibilityNodeInfo?,
        val oneTimeCode: AccessibilityNodeInfo?,
        val editables: List<AccessibilityNodeInfo>
    )

    fun isInstagram(root: AccessibilityNodeInfo): Boolean {
        return root.packageName?.toString() == PACKAGE
    }

    fun findLoginFields(root: AccessibilityNodeInfo): LoginFields {
        val editables = UiElementFinder.findAll(root) { node ->
            val cls = node.className?.toString().orEmpty()
            val id = node.viewIdResourceName.orEmpty()
            node.isEditable ||
                    node.isPassword ||
                    cls.contains("EditText", ignoreCase = true) ||
                    id.contains("edit", ignoreCase = true) ||
                    id.contains("login", ignoreCase = true) ||
                    id.contains("password", ignoreCase = true)
        }.filter { node ->
            val bounds = Rect()
            node.getBoundsInScreen(bounds)
            !bounds.isEmpty && bounds.width() > 80 && bounds.height() > 30
        }.distinctBy { boundsKey(it) }.sortedWith(compareBy({ top(it) }, { left(it) }))

        val password = editables.firstOrNull { node ->
            node.isPassword || nodeLabel(node).contains("password", ignoreCase = true)
        }
        val oneTimeCode = editables.firstOrNull { node ->
            val label = nodeLabel(node)
            label.contains("code", ignoreCase = true) ||
                    label.contains("authentication", ignoreCase = true) ||
                    label.contains("authenticator", ignoreCase = true) ||
                    label.contains("confirmation", ignoreCase = true) ||
                    label.contains("login code", ignoreCase = true) ||
                    label.contains("security", ignoreCase = true) ||
                    label.contains("код", ignoreCase = true)
        } ?: if (editables.size == 1 && containsAny(
                root,
                "authentication code",
                "authenticator app",
                "security code",
                "confirmation code",
                "login code",
                "two-factor",
                "Enter code",
                "Enter the code",
                "6-digit code",
                "аутентификации"
            )
        ) {
            editables.first()
        } else {
            null
        }
        val username = editables.firstOrNull { node ->
            node != password && node != oneTimeCode && isUsernameFieldLabel(nodeLabel(node))
        } ?: editables.firstOrNull { it != password && it != oneTimeCode }

        return LoginFields(username, password, oneTimeCode, editables)
    }

    fun findAction(root: AccessibilityNodeInfo, labels: List<String>): AccessibilityNodeInfo? {
        val candidates = UiElementFinder.findAll(root) { node ->
            val label = nodeLabel(node)
            labels.any { wanted -> label.equals(wanted, ignoreCase = true) } ||
                    labels.any { wanted -> label.contains(wanted, ignoreCase = true) }
        }.mapNotNull { node ->
            UiElementFinder.findClickableParent(node) ?: node.takeIf { it.isClickable }
        }.filter { node ->
            val bounds = Rect()
            node.getBoundsInScreen(bounds)
            !bounds.isEmpty
        }.distinctBy { boundsKey(it) }

        return candidates.maxWithOrNull(
            compareBy<AccessibilityNodeInfo> { nodeScore(it, labels) }
                .thenByDescending { area(it) }
        )
    }

    fun hasTargetUsername(root: AccessibilityNodeInfo, username: String): Boolean {
        val clean = username.trim().removePrefix("@")
        if (clean.isBlank()) return false
        return UiElementFinder.findAll(root) { node ->
            val label = nodeLabel(node).trim().removePrefix("@")
            label.equals(clean, ignoreCase = true) ||
                    label.contains("@$clean", ignoreCase = true)
        }.any()
    }

    fun containsAny(root: AccessibilityNodeInfo, vararg labels: String): Boolean {
        return UiElementFinder.findAll(root) { node ->
            val label = nodeLabel(node)
            labels.any { label.contains(it, ignoreCase = true) }
        }.any()
    }

    fun errorText(root: AccessibilityNodeInfo): String? {
        val markers = listOf(
            "incorrect password",
            "wrong password",
            "couldn't log",
            "could not log",
            "try again later",
            "suspicious",
            "challenge required",
            "help us confirm",
            "check your notifications",
            "another device",
            "approve this login",
            "account disabled",
            "we suspended",
            "password was incorrect",
            "invalid code"
        )
        val match = UiElementFinder.findAll(root) { node ->
            val label = nodeLabel(node)
            markers.any { label.contains(it, ignoreCase = true) }
        }.firstOrNull()
        return match?.let { nodeLabel(it).take(240) }
    }

    fun screenHash(root: AccessibilityNodeInfo): String {
        val text = StringBuilder()
        collectText(root, text, 0)
        val digest = MessageDigest.getInstance("SHA-1")
            .digest(text.toString().take(4000).toByteArray())
        return digest.take(8).joinToString("") { "%02x".format(it) }
    }

    private fun collectText(node: AccessibilityNodeInfo, out: StringBuilder, depth: Int) {
        if (depth > 7 || out.length > 5000) return
        val label = nodeLabel(node)
        if (label.isNotBlank()) out.append(label).append('|')
        for (i in 0 until node.childCount) {
            collectText(node.getChild(i) ?: continue, out, depth + 1)
        }
    }

    private fun isUsernameFieldLabel(label: String): Boolean {
        return label.contains("username", ignoreCase = true) ||
                label.contains("email", ignoreCase = true) ||
                label.contains("phone", ignoreCase = true) ||
                label.contains("mobile", ignoreCase = true) ||
                label.contains("имя пользователя", ignoreCase = true) ||
                label.contains("электрон", ignoreCase = true) ||
                label.contains("телефон", ignoreCase = true)
    }

    private fun nodeLabel(node: AccessibilityNodeInfo): String {
        return listOfNotNull(
            node.text?.toString(),
            node.contentDescription?.toString(),
            node.viewIdResourceName
        ).joinToString(" ")
    }

    private fun nodeScore(node: AccessibilityNodeInfo, labels: List<String>): Int {
        val label = nodeLabel(node)
        val exact = labels.any { label.equals(it, ignoreCase = true) }
        return (if (exact) 100 else 0) +
                (if (node.isClickable) 10 else 0) +
                labels.count { label.contains(it, ignoreCase = true) }
    }

    private fun boundsKey(node: AccessibilityNodeInfo): String {
        val rect = Rect()
        node.getBoundsInScreen(rect)
        return "${rect.left}:${rect.top}:${rect.right}:${rect.bottom}"
    }

    private fun top(node: AccessibilityNodeInfo): Int {
        val rect = Rect()
        node.getBoundsInScreen(rect)
        return rect.top
    }

    private fun left(node: AccessibilityNodeInfo): Int {
        val rect = Rect()
        node.getBoundsInScreen(rect)
        return rect.left
    }

    private fun area(node: AccessibilityNodeInfo): Int {
        val rect = Rect()
        node.getBoundsInScreen(rect)
        return rect.width() * rect.height()
    }
}
