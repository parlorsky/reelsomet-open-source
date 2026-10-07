package com.reelsomet.poster.automation.login

enum class InstagramLoginState {
    IDLE,
    OPEN_INSTAGRAM,
    FIND_LOGIN_ENTRY,
    OPEN_ACCOUNT_SWITCHER,
    OPEN_ADD_ACCOUNT,
    FILL_USERNAME,
    FILL_PASSWORD,
    SUBMIT_LOGIN,
    FILL_2FA,
    CLEAR_POST_LOGIN_PROMPTS,
    VERIFY_LOGIN,
    DONE,
    FAILED,
    NEEDS_ATTENTION
}
