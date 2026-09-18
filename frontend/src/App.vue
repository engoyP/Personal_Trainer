<template>
  <div class="shell">
    <div class="shell-main">
      <div class="shell-container">
        <nav class="nav">
          <span class="brand">运动分析与训练助手</span>
          <router-link to="/">数据看板</router-link>
          <router-link to="/plan">训练计划</router-link>
          <router-link to="/chat">教练对话</router-link>
          <router-link to="/places">去过的地方</router-link>
          <router-link to="/shoes">跑鞋</router-link>
          <router-link to="/knowledge">训练知识</router-link>
          <router-link to="/profile">个人档案</router-link>
        </nav>
        <router-view />
      </div>
    </div>
    <TracePanel />
  </div>
</template>

<script setup>
import TracePanel from './components/TracePanel.vue'
</script>
