<script setup lang="ts">
import { label, tagType, type LabelKind } from "../labels";

// Shows a backend code as a coloured Chinese tag, e.g. <StatusTag kind="state" value="running" />.
// Without `kind` the value is shown as is.
defineProps<{ value: string | null | undefined; kind?: LabelKind }>();
</script>

<template>
  <el-tag :type="tagType(value)" size="small">{{ kind ? label(kind, value) : value }}</el-tag>
</template>
