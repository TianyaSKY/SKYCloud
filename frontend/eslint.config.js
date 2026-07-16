import pluginVue from 'eslint-plugin-vue'
import {vueTsConfigs, withVueTs} from '@vue/eslint-config-typescript'
import skipFormatting from 'eslint-config-prettier'

export default withVueTs(
    {
        name: 'app/files-to-ignore',
        ignores: [
            '**/dist/**',
            '**/dist-ssr/**',
            '**/coverage/**',
            '**/node_modules/**',
            'auto-imports.d.ts',
            'components.d.ts',
            'env.d.ts',
        ],
    },
    pluginVue.configs['flat/essential'],
    vueTsConfigs.recommended,
    skipFormatting,
    {
        rules: {
            // 视图组件常用单个词命名（如 Home、Inbox），放开此规则
            'vue/multi-word-component-names': 'off',
            // 既有代码 any 较多，先警告不阻断
            '@typescript-eslint/no-explicit-any': 'warn',
            '@typescript-eslint/no-unused-vars': ['warn', {argsIgnorePattern: '^_', varsIgnorePattern: '^_'}],
            // 允许 console（前端日志统一通过 utils/logger，但保留 console 不报错）
            'no-console': 'off',
        },
    },
)
