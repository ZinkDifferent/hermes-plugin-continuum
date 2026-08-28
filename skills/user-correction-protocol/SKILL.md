---
name: user-correction-protocol
description: "Handle user corrections and embed them in skills for future sessions"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [macos, linux, windows]
tags: [correction, user-feedback, skills, essentials]
---

# User Correction Protocol

## Rule: Embed User Corrections in Skills Immediately

**When a user corrects your approach, style, or workflow, immediately update the relevant skill to embed the preference so future sessions start already knowing the correct method.**

## Detection of Correction Signals

Look for these explicit user correction signals:

### Style/Format Corrections
- "Stop doing X"
- "This is too verbose"
- "Don't format like this"
- "Why are you explaining"
- "Just give me the answer"
- "You always do Y and I hate it"
- "Remember this"

### Workflow/Approach Corrections
- "Do it this way instead"
- "Use method X, not method Y"
- "You're doing it wrong"
- "The correct approach is..."
- "Try a different approach"

### Explicit Instructions
- "Remember to always..."
- "From now on..."
- "Make sure you..."
- "Never again..."
- "Always use..."

## Immediate Response Protocol

### Step 1: Acknowledge and Document
When receiving a correction:
1. **Acknowledge immediately**: "You're right, I'll fix that"
2. **Document the correction**: Note what was wrong and what's correct
3. **Identify the relevant skill**: Find which skill governs this task type
4. **Update the skill**: Embed the correction in the skill's documentation

### Step 2: Skill Update Process
```python
# Example: User says "Always use markdown image links for remote users"
# 1. Find relevant skill (image-delivery-method)
# 2. Update skill with the correction
# 3. Add to skill's main rules section
# 4. Document the origin and context
```

### Step 3: Memory Backup
Update memory with the user preference:
```python
memory(
    target="user",
    action="add",
    content="User prefers inline markdown image links for remote delivery - never use MEDIA: tags"
)
```

## Skill Update Template

When updating a skill, include:

### Correction Origin
```markdown
## Origin of Correction

**Date:** [Date of correction]
**Context:** [Brief description of when/where correction occurred]
**User Statement:** [Exact quote of user's correction]
**Issue:** [What was wrong before]
**Solution:** [What the user specified as correct]
```

### Updated Protocol
```markdown
## Corrected Protocol

[Updated step-by-step process incorporating user's correction]
```

### Prevention
```markdown
## Prevention

[How to avoid repeating the mistake]
[Specific triggers to watch for]
[Automated checks if possible]
```

## Common Correction Scenarios

### Image Delivery
**Correction:** "Always use inline markdown links for remote users, never MEDIA: tags"
**Skill:** `image-delivery-method`
**Update:** Add explicit rule about markdown links and when to use them

### Vision Model Selection
**Correction:** "Use gemma4 for spatial layouts, not glm-4.6v/zai"
**Skill:** `vision-model-performance`
**Update:** Add model selection guidelines and verification steps

### File Verification
**Correction:** "Check actual windows, not just app launch success"
**Skill:** `file-verification-protocol`
**Update:** Add window presence verification step

### Tool Usage
**Correction:** "Use hermes config set, not patch for config changes"
**Skill:** `hermes-agent`
**Update:** Add correct config setting commands

## Error Prevention

### 1. Proactive Skill Updates
- Regularly review skills for outdated information
- Update skills immediately after user corrections
- Keep skills current with user preferences

### 2. Context Awareness
- Load relevant skills before starting tasks
- Check skills for user-specific preferences
- Follow skill protocols exactly as written

### 3. Continuous Improvement
- Track recurring correction patterns
- Update skills to address systematic issues
- Document lessons learned for future reference

## Documentation Standards

### Correction Records
Keep a log of all user corrections:
```json
{
  "corrections": [
    {
      "date": "2026-07-20",
      "skill": "image-delivery-method",
      "correction": "Always use inline markdown links for remote users",
      "previous_method": "Using MEDIA: tags",
      "new_method": "Hosting on webroot and using markdown links",
      "user_quote": "The only thing that works is markdown image links to hosted URLs"
    }
  ]
}
```

### Skill Update History
Maintain change history for skills:
```markdown
## Change History

- **2026-07-20**: Added image delivery method correction from user feedback
- **2026-07-25**: Updated vision model selection based on spatial analysis issues
- **[Future]**: [Add new corrections as they occur]
```

## Origin

**Jul 20-25, 2026.** Multiple user corrections occurred during floorplan generation and Sweet Home 3D file creation:
- Image delivery method corrections (multiple times)
- Vision model selection corrections
- File verification process corrections
- Circe Gate diagnostic handling corrections

The agent repeatedly failed to embed these corrections in skills, leading to repeated mistakes and user frustration. This protocol ensures that user feedback immediately improves future performance.

## Key Principles

1. **Immediate action** - Don't wait, update skills right away
2. **Specificity** - Embed exact user instructions, not generalizations
3. **Context** - Document when/where the correction occurred
4. **Prevention** - Add checks to avoid repeating the mistake
5. **Persistence** - Skills carry the correction forward to future sessions