<script setup lang="ts">
const navigation = [
  {
    path: "/investigations",
    mark: "⌕",
    title: "提问与调查",
    subtitle: "从问题到证据与报告",
  },
  {
    path: "/history",
    mark: "◴",
    title: "历史资料研究",
    subtitle: "固定预测与独立结局评价",
  },
  {
    path: "/radar",
    mark: "◎",
    title: "情报雷达",
    subtitle: "发现值得调查的变化",
  },
  {
    path: "/decisions",
    mark: "◇",
    title: "决策研究",
    subtitle: "比较方案、约束与不确定性",
  },
  {
    path: "/topics",
    mark: "▤",
    title: "长期专题",
    subtitle: "长期跟踪与判断积累",
  },
  {
    path: "/deliverables",
    mark: "▧",
    title: "情报成果",
    subtitle: "审阅、发布与追溯",
  },
];
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <RouterLink to="/investigations" class="brand"
        ><span class="brand-mark">B</span
        ><span>BioRadar<small>生物医药情报</small></span></RouterLink
      >
      <div class="nav-heading">研究工作台</div>
      <nav aria-label="主导航">
        <RouterLink
          v-for="item in navigation"
          :key="item.path"
          :to="item.path"
          class="nav-item"
        >
          <span class="nav-icon" aria-hidden="true">{{ item.mark }}</span>
          <span
            >{{ item.title }}<small>{{ item.subtitle }}</small></span
          >
        </RouterLink>
      </nav>
      <div class="sidebar-bottom">
        <RouterLink to="/admin/data" class="settings-link"
          >◷　数据与能力接入</RouterLink
        >
        <div class="workspace">
          <span class="avatar">研</span
          ><span>研究工作空间<small>问题 · 证据 · 报告</small></span>
        </div>
      </div>
    </aside>
    <section class="main-shell">
      <header class="topbar">
        <span>情报研究 / 工作台</span><span class="build-label">开发预览</span>
      </header>
      <main><RouterView /></main>
    </section>
  </div>
</template>
