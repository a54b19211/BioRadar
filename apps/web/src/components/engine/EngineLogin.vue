<script setup lang="ts">
import { ref } from "vue";
import { ElButton, ElInput } from "element-plus";
import { post } from "../../api/engine";
const emit = defineEmits<{ authenticated: [] }>();
const credential = ref("");
const error = ref("");
const busy = ref(false);
async function login() {
  if (!credential.value.trim() || busy.value) return;
  busy.value = true;
  error.value = "";
  const entered = credential.value;
  credential.value = "";
  try {
    await post("/session", { credential: entered });
    emit("authenticated");
  } catch (e) {
    error.value = e instanceof Error ? e.message : "登录失败";
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <section class="engine-panel login-panel">
    <div class="eyebrow">RESEARCH WORKSPACE</div>
    <h2>进入研究空间</h2>
    <p class="description">登录后查看已采用的资料、分析记录和待核实线索。</p>
    <form @submit.prevent="login" class="login-form">
      <label for="engine-credential">本地研究用户凭据</label>
      <ElInput
        id="engine-credential"
        v-model="credential"
        type="password"
        autocomplete="current-password"
        placeholder="输入管理员提供的登录凭据"
      />
      <p v-if="error" role="alert" class="error-inline">{{ error }}</p>
      <ElButton
        native-type="submit"
        type="primary"
        :loading="busy"
        :disabled="!credential.trim()"
        >进入工作台</ElButton
      >
    </form>
  </section>
</template>
