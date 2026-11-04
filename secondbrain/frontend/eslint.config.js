import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'

/**
 * ESLint 配置（v39）。
 *
 * 目标不是"零告警"，而是**每条告警都值得看**。
 * vue/recommended 默认带一堆风格噪音（属性连字符、多词组件名、每行属性数…），
 * 存量代码会爆几百条 —— 噪音淹没真问题，CI 就没人看了。
 *
 * 所以这里：
 *   · 保留**会抓 bug 的**规则（未定义变量、未用变量、==/!==、no-eval…）
 *   · 关掉纯风格类（交给 Prettier，或直接不管）
 *   · 存量豁免写在 ignores 里（新代码必须干净）
 */
export default [
  {
    ignores: ['dist/**', 'node_modules/**', 'public/**', 'scripts/**']
  },

  js.configs.recommended,
  ...pluginVue.configs['flat/recommended'],

  // ---- 全局环境 ----
  {
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.es2021
      }
    },
    rules: {
      // 未使用的变量：真问题（拼错名字、忘删的调试变量）
      'no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      // 未定义变量：真 bug
      'no-undef': 'error',
      // 松散相等：真 bug 来源（null/undefined 混用）
      eqeqeq: ['error', 'always', { null: 'ignore' }],
      // 调试残留
      'no-debugger': 'error',
      // eval / with：安全和性能问题
      'no-eval': 'error',
      'no-with': 'error',
      // 隐式全局
      'no-implicit-globals': 'error',
      // 生产代码里的 console：只留 warn/error
      'no-console': ['warn', { allow: ['warn', 'error'] }],
      // 空 catch 块：吞掉错误是万恶之源
      'no-empty': ['error', { allowEmptyCatch: false }],
      // 变量遮蔽（no-shadow 会误报同名不同作用域，先不用）
      'no-var': 'error',
      'prefer-const': 'warn'
    }
  },

  // ---- Vue 组件：只保留会抓 bug 的 ----
  {
    files: ['**/*.vue'],
    rules: {
      // 组件必须多词（防与 HTML 元素撞名）——存量全是单文件视图，豁免
      'vue/multi-word-component-names': 'off',
      // 模板里未定义的变量 → 运行时 undefined
      'vue/no-undef-properties': 'error',
      // 组件名必须 PascalCase
      'vue/component-definition-name-casing': ['error', 'PascalCase'],
      // props 必须在组件里声明
      'vue/require-prop-types': 'off',
      // 每个块只能有一个根元素（Vue 3 其实允许多根）
      'vue/one-component-per-file': 'off',
      // 属性顺序 / 换行类：纯风格，交给 Prettier
      'vue/attribute-hyphenation': 'off',
      'vue/max-attributes-per-line': 'off',
      'vue/singleline-html-element-content-newline': 'off',
      'vue/multiline-html-element-content-newline': 'off',
      'vue/html-self-closing': 'off',
      'vue/html-closing-bracket-newline': 'off',
      'vue/first-attribute-linebreak': 'off',
      'vue/html-indent': 'off',
      'vue/html-closing-bracket-spacing': 'off',
      'vue/mustache-interpolation-spacing': 'off',
      'vue/no-multi-spaces': 'off',
      'vue/attributes-order': 'off',
      'vue/require-default-prop': 'off',
      'vue/no-v-html': 'off',
      'vue/component-tags-order': 'off',
      'vue/block-tag-newline': 'off'
    }
  },

  // ---- 测试脚本：console 是正常输出 ----
  {
    files: ['tests/**', 'scripts/**'],
    rules: {
      'no-console': 'off'
    }
  }
]
