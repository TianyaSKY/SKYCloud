import {createApp} from 'vue'
import {createPinia} from 'pinia'
import {Message} from '@arco-design/web-vue'
import App from './App.vue'
import router from './router'
import ArcoVue from '@arco-design/web-vue'
import {logger} from './utils/logger'
import '@arco-design/web-vue/dist/arco.css'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)
app.use(ArcoVue)

// 全局兜底异常处理，避免未捕获错误导致整棵组件树白屏
app.config.errorHandler = (err, instance, info) => {
    const componentName = (instance?.$options as {__name?: string} | undefined)?.__name ?? 'Unknown'
    logger.error('全局异常: {} | info={} | component={}', err, info, componentName)
    Message.error({content: '应用发生异常，请刷新重试', position: 'top', closable: true})
}

app.mount('#app')
