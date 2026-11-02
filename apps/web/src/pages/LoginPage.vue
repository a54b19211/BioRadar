<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { login } from "../api";

const router = useRouter();
const credential = ref("");
const busy = ref(false);

async function submit() {
  busy.value = true;
  try {
    await login(credential.value);
    router.push("/");
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <el-card style="max-width: 380px; margin: 80px auto">
    <h2>BioRadar 登录</h2>
    <el-form label-position="top" @submit.prevent="submit">
      <el-form-item label="登录凭据">
        <el-input v-model="credential" type="password" show-password autofocus />
      </el-form-item>
      <el-button type="primary" native-type="submit" :loading="busy" :disabled="!credential">登录</el-button>
    </el-form>
  </el-card>
</template>
