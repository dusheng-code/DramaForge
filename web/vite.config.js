import { defineConfig } from 'vite'
import { createHash } from 'node:crypto'
import { parse, compileScript } from 'vue/compiler-sfc'

/**
 * 极简 SFC 编译插件。
 *
 * 为什么自己写：规格 §12 的前端依赖白名单只有 `vue vite axios`，不允许引入
 * `@vitejs/plugin-vue`。而 `@vue/compiler-sfc` 是 `vue` 包自身的依赖，所以这里
 * 直接复用它把 `<script setup>` + `<template>` 编译成单文件 ESM 模块。
 *
 * 局限（本项目刻意遵守，以换取零额外依赖）：
 * 1. 不支持 `<style>` 块 —— 全部样式集中在 src/styles.css；
 * 2. 不做细粒度 HMR，dev 下修改 .vue 触发整页刷新（生产构建不受影响）。
 */
function vueSfc() {
  const scopeHash = (file) => createHash('sha256').update(file).digest('hex').slice(0, 8)

  return {
    name: 'dramaforge-sfc',
    enforce: 'pre',
    transform(code, id) {
      const file = id.split('?')[0]
      if (!file.endsWith('.vue')) return null

      const { descriptor, errors } = parse(code, { filename: file })
      if (errors.length) {
        const detail = errors.map((e) => e.message || String(e)).join('; ')
        throw new Error(`[sfc] ${file} 解析失败：${detail}`)
      }
      if (!descriptor.scriptSetup) {
        throw new Error(`[sfc] ${file}：约定所有组件使用 <script setup>`)
      }
      if (descriptor.styles && descriptor.styles.length) {
        throw new Error(`[sfc] ${file}：不支持 <style> 块，请把样式写入 src/styles.css`)
      }

      // inlineTemplate=true：模板渲染函数直接内联进 setup，<script setup> 的绑定
      // 无需再做 $setup 转发，编译产物即完整组件模块。
      const compiled = compileScript(descriptor, {
        id: `data-v-${scopeHash(file)}`,
        inlineTemplate: true,
        isProd: true,
      })
      return { code: compiled.content, map: null }
    },
  }
}

export default defineConfig({
  base: './',
  plugins: [vueSfc()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // 开发期把 API 与静态产物都转发到本地 FastAPI（:8000），避免跨域
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/var': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2020',
    sourcemap: false,
  },
})
