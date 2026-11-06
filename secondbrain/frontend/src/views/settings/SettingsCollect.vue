<script setup>
/**
 * 设置 · 采集与解析（v0.7.3）。
 *
 * 核心说明：抖音 / 快手 / 小红书现在不给未登录请求返回作品正文，
 * 纯本地只能拿到标题和摘要。要完整文案有两条路：
 *   1. 提供登录态 Cookie（本地，不外发）
 *   2. 接一个第三方聚合解析 API（链接会发给对方）
 * 这一页只做第 2 条，并且默认关闭 —— 开不开由用户自己决定。
 */
import { ref, reactive, onMounted } from 'vue'
import { settingsApi } from '../../api/client'

const form = reactive({
  enabled: false,
  url_template: '',
  auth_header: '',
  auth_value: '',
  clear_auth: false,
  timeout: 30
})

const state = reactive({
  loading: true,
  saving: false,
  saved: '',
  error: '',
  authValueMasked: '',
  authValueSet: false
})

const testUrl = ref('')
const testing = ref(false)
const testResult = ref(null)

function errorText(err) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail.length) return detail[0]?.msg || '参数不合法'
  return err?.message || '请求失败'
}

async function load() {
  state.loading = true
  try {
    const { data } = await settingsApi.getParserApi()
    form.enabled = !!data.enabled
    form.url_template = data.url_template || ''
    form.auth_header = data.auth_header || ''
    form.timeout = data.timeout || 30
    state.authValueMasked = data.auth_value_masked || ''
    state.authValueSet = !!data.auth_value_set
    form.auth_value = ''
  } catch (err) {
    state.error = errorText(err)
  } finally {
    state.loading = false
  }
}

async function save() {
  state.saving = true
  state.error = ''
  state.saved = ''
  try {
    const { data } = await settingsApi.saveParserApi({
      enabled: form.enabled,
      url_template: form.url_template,
      auth_header: form.auth_header,
      auth_value: form.auth_value,
      clear_auth: form.clear_auth,
      timeout: Number(form.timeout) || 30
    })
    state.authValueMasked = data.auth_value_masked || state.authValueMasked
    state.authValueSet = !!data.auth_value_set
    form.auth_value = ''
    form.clear_auth = false
    state.saved = '已保存'
    setTimeout(() => (state.saved = ''), 2400)
  } catch (err) {
    state.error = errorText(err)
  } finally {
    state.saving = false
  }
}

async function runTest() {
  testing.value = true
  testResult.value = null
  try {
    const { data } = await settingsApi.testParserApi(testUrl.value)
    testResult.value = data
  } catch (err) {
    testResult.value = { ok: false, error: errorText(err) }
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="settings-group">
    <div class="card">
      <div class="card-title"><span>🧩</span> 第三方解析 API（可选）</div>

      <div class="warn-box">
        开启后，平台适配器抓不到内容时，<b>这条链接会被发到你填的服务</b>。默认关闭，不填就不生效。
        想完全不外发，就走「登录态 Cookie」那条路（尚未实现）。
      </div>

      <div v-if="state.loading" class="dim">加载中……</div>

      <template v-else>
        <div class="row" style="margin-top: 12px">
          <label class="switch">
            <input v-model="form.enabled" type="checkbox" />
            <span>启用第三方解析兜底</span>
          </label>
          <div class="spacer" style="flex: 1"></div>
          <span v-if="state.authValueSet" class="badge ok">
            已存密钥 {{ state.authValueMasked }}
          </span>
        </div>

        <div class="field">
          <div class="field-label">接口地址</div>
          <input
            v-model="form.url_template"
            type="text"
            placeholder="https://api.example.com/parse?url={url}"
            style="width: 100%"
          />
          <div class="hint">
            用 <span class="mono">{url}</span> 占位表示目标链接（会自动 URL
            编码）。不写占位符则自动拼 <span class="mono">?url=</span>。如果服务要求把 key 放
            query，直接写进这个地址即可。
          </div>
        </div>

        <div class="grid2">
          <div class="field">
            <div class="field-label">鉴权头名称（可选）</div>
            <input
              v-model="form.auth_header"
              type="text"
              placeholder="X-API-KEY"
              style="width: 100%"
            />
          </div>
          <div class="field">
            <div class="field-label">鉴权头取值</div>
            <input
              v-model="form.auth_value"
              type="password"
              :placeholder="state.authValueSet ? '留空则保留已保存的值' : '没有鉴权就留空'"
              style="width: 100%"
            />
          </div>
        </div>

        <div class="field" style="max-width: 200px">
          <div class="field-label">超时（秒）</div>
          <input
            v-model.number="form.timeout"
            type="number"
            min="5"
            max="120"
            style="width: 100%"
          />
        </div>

        <div class="row wrap" style="margin-top: 12px">
          <button class="btn" :disabled="state.saving" @click="save">
            {{ state.saving ? '保存中…' : '保存' }}
          </button>
          <label v-if="state.authValueSet" class="switch tiny">
            <input v-model="form.clear_auth" type="checkbox" />
            <span>清除已存密钥</span>
          </label>
          <div class="spacer" style="flex: 1"></div>
          <span v-if="state.saved" class="badge ok">{{ state.saved }}</span>
        </div>

        <div v-if="state.error" class="error-line">{{ state.error }}</div>
      </template>
    </div>

    <div class="card">
      <div class="card-title"><span>🔬</span> 测试解析</div>
      <div class="dim" style="font-size: 12px; margin-bottom: 10px">
        拿一条真实分享链接试一下，不会写库。先保存配置再测试。
      </div>
      <div class="row wrap">
        <input
          v-model="testUrl"
          type="text"
          placeholder="粘贴抖音 / 快手 / 小红书分享链接"
          style="flex: 1; min-width: 260px"
        />
        <button class="btn" :disabled="testing || !testUrl.trim()" @click="runTest">
          {{ testing ? '解析中…' : '测试' }}
        </button>
      </div>

      <div v-if="testResult" class="test-box" :class="{ ok: testResult.ok }">
        <div v-if="testResult.ok">
          <div class="row">
            <span class="badge ok">成功</span>
            <span class="dim">{{ testResult.extractor }}</span>
            <div class="spacer" style="flex: 1"></div>
            <span class="dim">{{ testResult.text_len }} 字</span>
          </div>
          <div class="test-title">{{ testResult.title || '（无标题）' }}</div>
          <div class="test-text">{{ testResult.text_head }}</div>
          <div v-if="testResult.extra?.media?.length" class="hint">
            另返回 {{ testResult.extra.media.length }} 个媒体地址
          </div>
        </div>
        <div v-else class="fail-line">{{ testResult.error }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.warn-box {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-2, #a0a6b8);
  background: color-mix(in srgb, var(--warn, #d9a441) 12%, transparent);
  border-left: 3px solid var(--warn, #d9a441);
  border-radius: 6px;
  padding: 8px 10px;
}

.field {
  margin-top: 10px;
}

.field-label {
  font-size: 12px;
  color: var(--text-2, #a0a6b8);
  margin-bottom: 4px;
}

.hint {
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-3, #7c8397);
  margin-top: 4px;
}

.grid2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.switch {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  cursor: pointer;
}

.switch.tiny {
  font-size: 12px;
  color: var(--text-2, #a0a6b8);
}

.switch input {
  accent-color: var(--accent, #6aa8ff);
}

.error-line {
  margin-top: 10px;
  font-size: 12px;
  color: var(--danger, #e5484d);
}

.test-box {
  margin-top: 12px;
  padding: 10px;
  border-radius: 8px;
  background: var(--surface-2, rgba(255, 255, 255, 0.03));
  border: 1px solid var(--border, rgba(255, 255, 255, 0.08));
}

.test-box.ok {
  border-color: color-mix(in srgb, var(--ok, #3fb950) 45%, transparent);
}

.test-title {
  margin-top: 6px;
  font-weight: 600;
  font-size: 13px;
}

.test-text {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-2, #a0a6b8);
  white-space: pre-wrap;
  max-height: 160px;
  overflow-y: auto;
}

.fail-line {
  font-size: 12px;
  color: var(--danger, #e5484d);
  word-break: break-all;
}

@media (max-width: 720px) {
  .grid2 {
    grid-template-columns: 1fr;
  }
}
</style>
