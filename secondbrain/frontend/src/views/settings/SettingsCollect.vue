<script setup>
/**
 * 设置 · 采集与解析。
 *
 * 核心说明：抖音 / 快手 / 小红书不给未登录请求返回作品正文，
 * 纯本地只能拿到标题和摘要。完整文案有三条路，这一页把能自动化的两条都做了：
 *   1. 登录态 Cookie（本地，不外发）—— v0.7.8 补上入口，之前只写在错误提示里
 *   2. 第三方聚合解析 API（链接会发给对方）—— 默认关闭
 *   3. 手动粘贴文案 —— 在「任务状态」页失败条目上（100% 可靠）
 *
 * v0.7.8：预设目录里三个已失效的公共接口全部下架（实测 404 / 返回网页 / DNS 失败），
 * 只留实测可用的与自建出口；新增 {key} 占位符，密钥从 secrets.json 取。
 */
import { ref, reactive, computed, onMounted } from 'vue'
import { settingsApi } from '../../api/client'

const FALLBACK_PRESET = {
  id: 'custom',
  name: '自定义（手填地址）',
  kind: 'custom',
  platforms: '',
  url_template: '',
  note: '有自己的解析服务就填这里：地址里用 {url} 占位，或留空让程序自动拼 ?url=。',
  doc_url: ''
}

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

const presets = ref([])
const presetId = ref('custom')

const testUrl = ref('')
const testing = ref(false)
const testResult = ref(null)

const currentPreset = computed(
  () =>
    presets.value.find((p) => p.id === presetId.value) || presets.value[presets.value.length - 1]
)

const kindLabels = {
  keyed: '需密钥',
  selfhost: '自建服务',
  custom: '自定义'
}

function errorText(err) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail.length) return detail[0]?.msg || '参数不合法'
  return err?.message || '请求失败'
}

/** 已有配置反查预设：模板一字不差才算命中，否则就是用户自己填的。 */
function inferPreset(template) {
  const hit = presets.value.find((p) => p.id !== 'custom' && p.url_template === template)
  return hit ? hit.id : 'custom'
}

function applyPreset() {
  const preset = presets.value.find((p) => p.id === presetId.value)
  if (preset) form.url_template = preset.url_template
  testResult.value = null
}

/** 用户手动改了地址 → 归到「自定义」，别让下拉继续显示某个预设。 */
function onTemplateInput() {
  const preset = presets.value.find((p) => p.id === presetId.value)
  if (preset && preset.id !== 'custom' && form.url_template !== preset.url_template) {
    presetId.value = 'custom'
  }
}

async function loadPresets() {
  try {
    // 注意：client.js 的 request() 直接返回响应体，不是 axios 的 { data }
    const payload = await settingsApi.getParserPresets()
    const list = Array.isArray(payload?.presets) ? payload.presets : []
    presets.value = list.length ? list : [FALLBACK_PRESET]
  } catch {
    presets.value = [FALLBACK_PRESET]
  }
  if (!presets.value.some((p) => p.id === 'custom')) presets.value.push(FALLBACK_PRESET)
}

async function load() {
  state.loading = true
  await loadPresets()
  try {
    const data = await settingsApi.getParserApi()
    form.enabled = !!data.enabled
    form.url_template = data.url_template || ''
    form.auth_header = data.auth_header || ''
    form.timeout = data.timeout || 30
    state.authValueMasked = data.auth_value_masked || ''
    state.authValueSet = !!data.auth_value_set
    form.auth_value = ''
    presetId.value = inferPreset(form.url_template)
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
    const data = await settingsApi.saveParserApi({
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
    testResult.value = await settingsApi.testParserApi(testUrl.value)
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
        开启后，平台适配器抓不到内容时，<b>这条链接会被发到你选的服务</b>。默认关闭，不选就不生效。
        公共免费接口随时可能失效（本页预设已实测筛过一轮）；最稳的是上面的 Cookie，或直接在
        「任务状态」页把文案粘贴进失败条目。
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
          <div class="field-label">选择服务</div>
          <div class="row wrap">
            <select v-model="presetId" class="preset-select" @change="applyPreset">
              <option v-for="p in presets" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
            <span v-if="currentPreset?.kind && currentPreset.kind !== 'custom'" class="badge">
              {{ kindLabels[currentPreset.kind] || currentPreset.kind }}
            </span>
            <span v-if="currentPreset?.platforms" class="badge">
              {{ currentPreset.platforms }}
            </span>
          </div>
          <div v-if="currentPreset?.note" class="hint">
            {{ currentPreset.note }}
            <a
              v-if="currentPreset.doc_url"
              class="doc-link"
              :href="currentPreset.doc_url"
              target="_blank"
              rel="noreferrer"
              >项目地址 ↗</a
            >
          </div>
        </div>

        <div class="field">
          <div class="field-label">接口地址</div>
          <input
            v-model="form.url_template"
            type="text"
            placeholder="https://api.example.com/parse?url={url}"
            style="width: 100%"
            @input="onTemplateInput"
          />
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
            <div class="field-label">密钥 / 鉴权值</div>
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
      <div class="row wrap">
        <input
          v-model="testUrl"
          type="text"
          placeholder="粘贴抖音 / 快手 / 小红书分享链接"
          style="flex: 1 1 220px; min-width: 0"
        />
        <button class="btn" :disabled="testing || !testUrl.trim()" @click="runTest">
          {{ testing ? '解析中…' : '测试' }}
        </button>
      </div>

      <div v-if="testResult" class="test-box" :class="{ ok: testResult.ok }">
        <div v-if="testResult.ok">
          <div class="row wrap">
            <span class="badge ok">成功</span>
            <span class="dim">{{ testResult.extractor }}</span>
            <span v-if="!testResult.enabled" class="badge warn">开关还没打开</span>
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

.doc-link {
  margin-left: 4px;
  color: var(--accent, #6aa8ff);
  text-decoration: none;
  white-space: nowrap;
}

.doc-link:hover {
  text-decoration: underline;
}

.preset-select {
  /* 不要写 min-width: 260px —— 窄窗口下卡片内宽只有 240 出头，
     一个不可收缩的下拉就会把整页顶出横向滚动条。 */
  flex: 1 1 260px;
  width: auto;
  min-width: 0;
  max-width: 380px;
  padding: 7px 10px;
  border-radius: 8px;
  border: 1px solid var(--border, rgba(255, 255, 255, 0.12));
  background: var(--surface-2, rgba(255, 255, 255, 0.04));
  color: var(--text, #e6e8ee);
  font-size: 13px;
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

/* 不设 max-height：整页只该有一个滚动条，预览文本最多 300 字，撑不开页面 */
.test-text {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-2, #a0a6b8);
  white-space: pre-wrap;
  word-break: break-word;
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
