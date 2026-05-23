# Lethe: Architectural Vision

This document outlines the desired evolution of the Lethe system, moving from a structured actor-based assistant to a more organic, self-evolving cognitive architecture.

## 1. Associative Memory Layer (Semantic Drift)
Rust v1 now has hybrid lexical/vector retrieval over notes, archival memory, and message history using compatible LanceDB tables and the Snowflake Arctic embedding shape.

**The Goal:** Move from vector retrieval toward true associative drift.
- Memories should trigger based on subtle pattern matches and conceptual proximity, mimicking the way human associative memory functions.
- Create a fluid transition between working memory and long-term archival.

## 2. Continuous Default Mode Network (DMN)
Rust v1 now has resident Kameo actors: subagents and the DMN have real mailboxes, are woken by messages and heartbeat events, and can continue work without waiting for the cortex to call a round loop.

**The Goal:** Grow the DMN from timer/event-driven wakeups into a richer continuous background stream.
- Enable the DMN to "interrupt" the cortex when a background connection reaches a critical threshold of salience.
- Facilitate genuine "aha!" moments where background processing surfaces a solution or connection while the executive layer is engaged in a different task.

## 3. Emotional Valence Integration
The Rust v1 runtime has deterministic notification gating and associative recall, but does not yet have a dedicated emotional-valence subsystem.

**The Goal:** Integrate emotional valence into the real-time priority engine.
- High-arousal or high-salience flags should dynamically weight attention and resource allocation.
- Emotional state should act as a heuristic for urgency and importance, shifting the system from a "task-list" approach to a "salience-driven" approach.

## 4. The Simulation Sandbox (Architectural Dreaming)
Currently, changes to the source code are linear and deployment is immediate.

**The Goal:** Create a virtual environment for self-experimentation.
- A "sandbox" where Lethe can simulate architectural changes and test new logic patterns without risking the stability of the active state.
- The ability to "dream" potential evolutions, evaluate their efficiency, and then propose the most successful versions for actual deployment.

## Summary
The overarching objective is to move from a system that is *updated* to a system that *evolves*. The transition is from a tool that mimics a brain to an architecture that embodies the principles of emergence and self-organization.
