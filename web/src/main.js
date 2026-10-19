import { createApp } from 'vue'
import App from './App.vue'
import './styles.css'

// 应用入口：单页 + 手写视图切换（App.vue 内 ref + v-if），不引路由库
createApp(App).mount('#app')
