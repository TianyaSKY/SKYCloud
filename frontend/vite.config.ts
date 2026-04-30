import {fileURLToPath, URL} from 'node:url'

import {defineConfig} from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import {ArcoResolver} from 'unplugin-vue-components/resolvers'

// https://vite.dev/config/
export default defineConfig(({mode}) => ({
    plugins: [
        vue(),
        ...(mode !== 'production' ? [vueDevTools()] : []),
        AutoImport({
            resolvers: [ArcoResolver()],
        }),
        Components({
            resolvers: [
                ArcoResolver({
                    sideEffect: true
                })
            ],
        }),
    ],
    resolve: {
        alias: {
            '@': fileURLToPath(new URL('./src', import.meta.url))
        },
    },
    build: {
        rollupOptions: {
            output: {
                manualChunks: {
                    'vendor-vue': ['vue', 'vue-router'],
                    'vendor-arco': ['@arco-design/web-vue'],
                    'vendor-office': ['@vue-office/docx', '@vue-office/excel', '@vue-office/pdf'],
                    'vendor-markdown': ['marked'],
                }
            }
        }
    },
    server: {
        proxy: {
            '/api': {
                target: 'http://localhost:5000',
                changeOrigin: true,
                // rewrite: (path) => path.replace(/^\/api/, ''), // If the backend doesn't have /api prefix, uncomment this
            }
        }
    }
}))
