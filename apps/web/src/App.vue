<script setup lang="ts">
import { useRouter } from "vue-router";
import { auth, logout } from "./api";
import { label } from "./labels";

const router = useRouter();

async function signOut() {
  await logout();
  router.push("/login");
}
</script>

<template>
  <header v-if="auth.signedIn">
    <strong class="brand">BioRadar</strong>
    <nav>
      <RouterLink to="/">提问与调查</RouterLink>
      <RouterLink to="/topics">专题</RouterLink>
      <RouterLink to="/monitor">监测</RouterLink>
    </nav>
    <span class="muted">{{ auth.name }}（{{ label("role", auth.role) }}）</span>
    <el-button size="small" @click="signOut">退出登录</el-button>
  </header>
  <main>
    <RouterView />
  </main>
</template>
