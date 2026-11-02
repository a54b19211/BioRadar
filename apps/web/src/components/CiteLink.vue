<script setup lang="ts">
import { inject } from "vue";

// A clickable evidence reference like [E3]. The analysis page provides `cite`, which opens the drawer.
defineProps<{ id: string }>();
const cite = inject<(ref: string) => void>("cite", () => {});
</script>

<template>
  <el-link type="primary" underline="never" @click="cite(id)">[{{ id }}]</el-link>
</template>
