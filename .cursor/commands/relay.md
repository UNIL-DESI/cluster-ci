---
alwaysApply: false
description: Clôture propre d'une session Maestro — arrête les agents, archive les résultats, prépare la relève.
---

# Relay — Transition Maestro

This command performs a **clean shutdown** of the current Maestro session and prepares a handover for the next one.

---

## Procedure (execute in this exact order)

### Step 1: Stop All Sub-Agents

1. `manage_subagents` → `list` to identify all active sub-agents.
2. For each active sub-agent:
   - Send a `send_message` asking for a **final status report** (what was done, what's pending, any partial results).
   - Wait briefly for responses (set a `schedule(DurationSeconds=60)` timer).
3. After collecting reports (or timeout): `manage_subagents` → `kill_all`.
4. No zombie agents must survive this step.

### Step 2: Finalize the Archive

1. Move any remaining content from `updates.md` into `walkthrough.md`.
2. Add a **closing section** to `walkthrough.md`:

```markdown
---

## 🏁 Session Close — [date]

**Reason**: Relay to new Maestro session.

### Final State of In-Progress Tasks
| Issue | Status at close | Partial results | Next steps |
|-------|----------------|-----------------|------------|
| #XX   | ...            | ...             | ...        |

### Unresolved Questions
- [Any questions that were pending in updates.md]

### AIVC Memory
- [Confirm all key decisions were committed to memory]
```

3. Clear `updates.md` — overwrite with:
```markdown
# Updates

> Session closed via /relay. See walkthrough.md for the full archive.
> A new Maestro session will pick up from here.
```

### Step 3: Commit to AIVC Memory

`remember` a detailed note including:
- Summary of what was accomplished this session.
- State of all open issues (which are done, which are in progress, which are blocked).
- Key decisions taken.
- Path to the walkthrough artifact for reference.

### Step 4: Generate the Relay Prompt

Output a **relay prompt** that the user can paste into a new conversation. Format:

```markdown
/maestro

## Relay from previous session

**Previous walkthrough**: [absolute path to walkthrough.md]
**Previous updates**: [absolute path to updates.md]
**Conversation ID**: [current conversation ID]

### Context
[2-3 sentence summary of what the project is about and where we are]

### Open Issues (carry over)
| Issue | Priority | Status | Notes |
|-------|----------|--------|-------|
| #XX   | ...      | ...    | ...   |

### Key Decisions from Previous Session
- [Decision 1]
- [Decision 2]

### Immediate Next Steps
1. [What the next Maestro should do first]
2. [Second priority]
```

### Step 5: Confirm

Tell the user (in the chat AND in updates.md) that the relay is ready:
- All agents stopped.
- Archive finalized.
- Memory committed.
- Relay prompt generated — ready to paste into a new conversation.
