---
date: 2026-08-13
topic: faster-install
---

# Faster agent-driven install

## Summary

Restructure the agent-driven install around two repo-shipped commands: a read-only preflight that performs all prerequisite checks and machine detection in one shot, and a single quiet verify command that replaces the current four-command verification — with the live workspace check removed from install and deferred to the teammate's first real `hwt open`. The five interview questions survive unchanged; they just fire back-to-back with detected options already in hand.

## Problem Frame

Teammates install Corral by pointing their coding agent at `AGENT_INSTRUCTIONS.md`. The flow is correct but feels slow, and the observed cost is the agent grinding through detection commands and permission prompts: Step 2 currently instructs the agent to improvise 10–20 investigation commands (repo scanning, Herdr config reads, tailscale detection, a three-way agent-CLI probe including a login-shell spawn), each a separate tool call and often a separate approval, each threaded through an LLM round-trip. Step 4 adds four verification commands plus a live workspace check that needs two more user interactions.

The initially suspected cost — running the full test suite on every install — is a rounding error: 153 tests complete in ~0.3 seconds. The suite is worth keeping; the command sprawl around it is not.

A constraint shapes any fix: approvals belong to the user's harness. The plugin cannot pre-authorize itself, and instructing agents to edit a teammate's allowlist is off the table. Speed must come from restructuring the work so each approval buys a large chunk of it.

## Key Decisions

- **Fewer commands, not pre-granted permissions.** The approval count drops because the flow runs ~4 commands instead of dozens — not because any consent step is bypassed. All five interview questions and every config-edit consent point remain.
- **Keep the test suite in every install, folded in quietly.** Runtime was never the cost; the suite catches genuine environment problems (Python version, filesystem quirks). It becomes part of the single verify command, surfacing output only on failure.
- **Defer the live workspace check to first use.** The event-hook bootstrap re-runs on every `hwt open` anyway, so first real use exercises the same path the install-time check did. Accepted consequence: an install-time layout bug now surfaces at the teammate's first `hwt open`, after the installing agent is gone — mitigated by telling the user exactly what to expect and where to report.
- **The self-contained interactive installer (rejected for now).** Running the whole interview inside `install.py` would reach the theoretical floor of one approval, but adds a second interview surface to maintain and forfeits the agent's adaptive judgment (odd agent-CLI locations, consent-gated Herdr config edits, PATH fixes). Revisit only if this restructure still feels slow.

## Requirements

**Preflight detection**

- R1. A repo-shipped, read-only preflight command performs, in one invocation, the Step 1 prerequisite checks (Python version, `git`, `herdr` presence and version, agent CLIs) and every Step 2 investigation the instructions currently ask the agent to improvise: candidate repository parent folders with per-folder repository counts, Herdr's current worktree directory and existing worktree collections, network name candidates (tailscale DNS name/IP, hostname), usable agent kinds, and existing Herdr keybindings with a free-key suggestion for the palette binding.
- R2. Preflight output is dual-format: machine-readable JSON for the agent plus a short human summary, so interview questions can quote detected options verbatim.
- R3. Preflight writes nothing — no config, no plugin linking, no state. It must be safe to approve blind.
- R4. Agent-CLI detection inside preflight replicates the current three-way probe (plain PATH lookup, login+interactive shell lookup with a timeout, well-known per-vendor bin directory globs), so agents no longer run those probes as separate commands.

**Verification**

- R5. A single verify command replaces Step 4's four: it runs the unit suite quietly (output surfaced only on failure), performs the doctor checks, and confirms the plugin is linked and enabled with the `cleanup` and `palette` actions present — ending in one overall pass/fail verdict.
- R6. The live workspace check is removed from the install flow. The final report tells the user to open their first worktree with `hwt open <repository>` when ready, states the expected result (exactly three tabs: `agent`, `shell`, `server`), and where to report if a second `hwt open` duplicates the workspace.

**Instructions rewrite**

- R7. `AGENT_INSTRUCTIONS.md` Step 2 becomes "run the preflight, then ask the five questions using its output"; Step 4 becomes the single verify command. The five questions themselves and Step 5's per-repository refinement flow are unchanged.
- R8. The Step 0 outline reflects the smaller surface so agents can set accurate expectations — on the order of four command approvals (clone, preflight, installer, verify) plus any config edits the user opts into.

## Key Flow

- F1. Teammate install
  - **Trigger:** A teammate points their coding agent at `AGENT_INSTRUCTIONS.md`.
  - **Steps:** Agent posts the Step 0 outline → clones the repo (approval 1) → runs preflight (approval 2) → asks the five interview questions back-to-back using preflight data → runs `install.py` with the assembled flags (approval 3) → runs the verify command (approval 4) → reports results, including the first-use `hwt open` instruction.
  - **Outcome:** Installed and verified with ~4 command approvals and no blocking interactions beyond the five questions. **Covers R1–R8.**

## Acceptance Examples

- AE1. **Covers R1, R2.** Given a machine without tailscale, when preflight runs, then its output lists hostname-derived candidates only, and interview question 3 presents localhost plus those names without the agent running any additional detection commands.
- AE2. **Covers R5.** Given a broken environment (e.g., unsupported Python), when the verify command runs, then the verdict is fail and the failing check's output is shown; given a healthy environment, the suite's per-test output is not shown.
- AE3. **Covers R6.** Given a completed install, when the teammate later runs `hwt open <repository>` for the first time, then the workspace comes up with exactly `agent`, `shell`, `server` tabs — and the install report has already told them this is the expected result and where to file an issue if a rerun duplicates the workspace.

## Success Criteria

- A standard teammate install executes on the order of four approval-worthy commands (clone, preflight, installer, verify), down from dozens, with config edits (Herdr worktree directory, keybinding) remaining separately consented.
- The only blocking interactions are the five interview questions and the final report.
- The full test suite still runs on every install.

## Scope Boundaries

- The self-contained interactive installer (one-approval floor) is deferred — revisit only if this restructure still feels slow in practice.
- The five interview questions, their consent semantics, and Step 5's per-repository `hwt init` refinement are untouched.
- No guidance to agents about harness allowlists or permission configuration — approvals stay user-owned.

## Outstanding Questions

- **Deferred to planning:** where preflight and verify live (`install.py` flags, `hwt doctor` modes, or a separate script — noting preflight must run from a fresh clone before `hwt` exists on PATH), and how the login-shell agent probe is implemented robustly from Python.
