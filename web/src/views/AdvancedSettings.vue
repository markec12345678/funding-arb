<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { NCard, NText, NIcon, NDivider, NSpin, NInput, NButton, NSpace, useMessage } from 'naive-ui'
import { CheckmarkCircleOutline, CloseCircleOutline, LockClosedOutline } from '@vicons/ionicons5'
import { getCredentialsStatus, getApiToken, setApiToken, clearApiToken } from '@/composables/useApi'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()
const message = useMessage()
const credentials = getCredentialsStatus()

// ─── API access token (optional server auth) ───────────────────────
const tokenInput = ref('')
const tokenSaved = ref(false)

const tokenConfigured = computed(() => tokenSaved.value || !!getApiToken())

onMounted(async () => {
  tokenSaved.value = !!getApiToken()
  await credentials.refresh()
})

function saveToken() {
  const trimmed = tokenInput.value.trim()
  if (!trimmed) return
  setApiToken(trimmed)
  tokenInput.value = ''
  tokenSaved.value = true
  message.success(t('settings.apiTokenSaved'))
}

function removeToken() {
  clearApiToken()
  tokenInput.value = ''
  tokenSaved.value = false
  message.success(t('settings.apiTokenCleared'))
}
</script>

<template>
  <div class="settings-page">
    <n-card :title="t('settings.credentialBackends')" size="small">
      <n-spin :show="credentials.loading.value">
        <div class="backend-list">
          <div v-for="(info, name) in credentials.data.value?.backends" :key="name" class="backend-item">
            <div class="backend-left">
              <n-icon v-if="info.available" color="#18a058" size="18"><CheckmarkCircleOutline /></n-icon>
              <n-icon v-else color="#d03050" size="18"><CloseCircleOutline /></n-icon>
              <n-text>{{ name }}</n-text>
            </div>
            <n-text depth="3" style="font-size: 11px">{{ info.description }}</n-text>
          </div>
        </div>
        <n-divider style="margin: 12px 0" />
        <div class="backend-summary">
          <n-text depth="3" style="font-size: 12px">{{ t('settings.configuredVenues') }}: {{ credentials.data.value?.venues_configured?.join(', ') || t('settings.none') }}</n-text>
        </div>
        <div class="backend-summary" style="margin-top: 4px">
          <n-text depth="3" style="font-size: 12px">{{ t('settings.missingVenues') }}: {{ credentials.data.value?.venues_missing?.join(', ') || t('settings.none') }}</n-text>
        </div>
      </n-spin>
    </n-card>

    <n-card :title="t('settings.apiToken')" size="small" style="margin-top: 16px">
      <div class="token-status">
        <n-icon :color="tokenConfigured ? '#18a058' : '#d03050'" size="18">
          <LockClosedOutline v-if="tokenConfigured" />
          <CloseCircleOutline v-else />
        </n-icon>
        <n-text depth="2" style="font-size: 12px">
          {{ tokenConfigured ? t('settings.apiTokenSet') : t('settings.apiTokenNotSet') }}
        </n-text>
      </div>
      <n-space style="margin-top: 12px" :wrap="false" :size="8">
        <n-input
          v-model:value="tokenInput"
          type="password"
          show-password-on="click"
          :placeholder="t('settings.apiTokenPlaceholder')"
          style="flex: 1"
          @keyup.enter="saveToken"
        />
        <n-button size="small" type="primary" :disabled="!tokenInput.trim()" @click="saveToken">
          {{ t('settings.apiTokenSave') }}
        </n-button>
        <n-button v-if="tokenConfigured" size="small" quaternary type="error" @click="removeToken">
          {{ t('settings.apiTokenClear') }}
        </n-button>
      </n-space>
      <n-text depth="3" style="font-size: 11px; display: block; margin-top: 8px">
        {{ t('settings.apiTokenHint') }}
      </n-text>
    </n-card>
  </div>
</template>

<style scoped>
.settings-page {
  height: 100%;
  max-width: 900px;
  margin: 0 auto;
}
.backend-list { display: flex; flex-direction: column; gap: 10px; }
.backend-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 12px;
  background: rgba(255, 255, 255, 0.03);
  border-radius: 6px;
}
.backend-left { display: flex; align-items: center; gap: 8px; }
.backend-summary { font-weight: 500; }
.token-status {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: rgba(255, 255, 255, 0.03);
  border-radius: 6px;
}

@media (max-width: 700px) {
  .backend-item {
    align-items: flex-start;
    flex-direction: column;
    gap: 6px;
  }
}
</style>
