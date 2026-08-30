<script setup lang="ts">
const foundations = [
  { name: "Durable workflow", detail: "Temporal-owned waits and wake conditions", state: "Foundation" },
  { name: "Agent kernel", detail: "Bounded proposals behind typed contracts", state: "Foundation" },
  { name: "Human review", detail: "Approve, revise, discuss, or reconcile", state: "Foundation" },
  { name: "Client packs", detail: "Private extensions over a public core", state: "Contract" },
];
</script>

<template>
  <main class="shell">
    <header class="masthead">
      <a class="brand" href="/" aria-label="Tiramisu home">
        <span class="brand-mark">T</span>
        <span>Tiramisu</span>
      </a>
      <span class="phase">Foundation phase</span>
    </header>

    <section class="hero">
      <p class="eyebrow">Long-running business agents</p>
      <h1>Pick up exactly<br />where you left off.</h1>
      <p class="lede">
        One durable agent follows each customer journey, sleeps between business events,
        and wakes with its context, commitments, and safety boundaries intact.
      </p>
      <div class="actions">
        <a class="primary" href="http://127.0.0.1:8000/docs">Open API docs</a>
        <a class="secondary" href="http://127.0.0.1:8233">Open Temporal UI</a>
      </div>
    </section>

    <section class="foundation" aria-labelledby="foundation-heading">
      <div>
        <p class="section-kicker">The first layer</p>
        <h2 id="foundation-heading">A deterministic shell for autonomous work.</h2>
      </div>
      <div class="cards">
        <article v-for="item in foundations" :key="item.name" class="card">
          <div class="card-heading">
            <h3>{{ item.name }}</h3>
            <span>{{ item.state }}</span>
          </div>
          <p>{{ item.detail }}</p>
        </article>
      </div>
    </section>
  </main>
</template>
