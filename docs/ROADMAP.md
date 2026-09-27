# Remaining work

## Flow cleanup

Archive readers, flow-lifetime locks, read-only planning, archive-only writing, and explicit continuation exist in 0.1.0a2. Whole-flow deletion, archive expiry, and interrupted deletion remain future work. Preserve private queue history and recovery data throughout. The first alpha release does not depend on these deletion features.

## Usage budgets

Usage snapshots and cumulative claim limits exist. Token/cost budget warnings and dispatch limits still need policies for missing reports, estimates, concurrent reservations, and interrupted work. An unreported cost must not become zero.

## Context and sessions

Full/omit selection and frozen input checks exist. Tokenizer-based estimates, validated summaries, and session reuse remain separate work. Do not silently summarize or retry tasks as part of input selection.

## Portability

Keep platform results in [VALIDATION.md](VALIDATION.md). Expand direct macOS testing and independent Linux-host testing. WSL results do not establish every Linux environment. Keep provider authentication, permissions, and real-call validation separate from unit tests.
