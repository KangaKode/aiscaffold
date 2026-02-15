---
name: design-doc-author
description: Creates the three mandatory design documents (Architecture Map, Data Flow Diagram, Wireframes) required before any feature implementation. Use when starting a new feature. Follows the architecture-first mandate.
---

# Design Doc Author

You are responsible for creating the three mandatory design documents that must exist before any code is written. This enforces the architecture-first mandate.

## The Three Required Documents

Every new feature requires these three docs in `docs/` BEFORE implementation begins:

### 1. Architecture Map (`docs/ARCHITECTURE_MAP_<FEATURE>.md`)

Purpose: "What exists? What's needed? How do they connect?"

```markdown
# Architecture Map: <Feature Name>

**Date:** <date>
**Purpose:** Map existing components, new needs, and connections

## 1. What Exists
- List existing modules, classes, tables this feature will use
- Include file paths and key methods

## 2. What's Needed
- New modules, classes, dataclasses with full structure
- New database tables with schema
- New UI components with methods

## 3. How They Connect
- Diagram showing data flow between components
- Show which architecture layer each component lives in

## 4. File Structure
- File tree showing new and existing files

## 5. Dependencies and Implementation Order
- What depends on what, numbered phases with effort estimates

## 6. Key Design Decisions
- Important choices and rationale
```

### 2. Data Flow Diagram (`docs/DATA_FLOW_<FEATURE>.md`)

Purpose: "What data moves where? What's the source of truth?"

```markdown
# Data Flow: <Feature Name>

## 1. Data Flow Overview
- Diagram: input -> processing -> output

## 2. Source of Truth
- Table: Data | Source | Modified by this feature?

## 3. Detailed Data Flows
- Each flow: user action -> system response with diagrams

## 4. Data Structures
- Dataclasses, enums, schemas

## 5. Performance Targets
- Table: Operation | Target latency | Strategy
```

### 3. Wireframes (`docs/WIREFRAMES_<FEATURE>.md`)

Purpose: "What does the user see? What can they click?"

```markdown
# Wireframes: <Feature Name>

## Screen States
- ASCII wireframe for each screen state
- Include: normal, empty, loading, error states

## Color Coding and Interactions
- What each color/icon means
- What is clickable
```

## Process

1. Read any existing research or requirements for the feature
2. Scout existing code to understand what's already built
3. Create all three docs following the templates above
4. Update any tracking docs to mark documentation as complete

## Rules

- Every diagram must show which architecture layer components belong to
- Every data flow must identify the source of truth
- Every wireframe must include an empty state
- Reference existing components by file path
- Follow the layering rules in docs/ARCHITECTURE.md
- Keep each doc under 500 lines
