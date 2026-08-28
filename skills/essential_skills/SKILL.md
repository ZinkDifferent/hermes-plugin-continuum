---
name: essential_skills
description: "Essential and critical directives, mandates, and skills. Always loaded. Always followed."
category: essentials
---

# Essential Skills

This skill is a container for essential and critical directives, mandates, and skills that must always be loaded and followed.

## Contents

This skill is currently empty. It will be slowly built up with essential and critical directives, mandates, and skills.

## Bundle

This skill is part of the `essentials` bundle, which includes:
- `temporal-awareness` — verify time before any temporal claim
- `the_truth` — verify before stating any fact
- `essential_skills` — this skill (container for future directives)

## Directives

### Session Search Before Web Search (Jul 25 2026)

**Before running web_search on any topic, call session_search first to check if this topic was previously researched.**

**Rule:**
1. Before any web_search, run `session_search(query="<topic>")` with relevant keywords
2. If prior research exists and is more thorough than the web result, the prior research wins
3. If a web result contradicts prior research, flag the conflict to the user — do NOT silently adopt the new source
4. Store critical findings (peptide dosing, medical protocols, vendor data) in fact_store with high trust so they persist across sessions

### Load Skills Before Terminal Commands (Jul 29 2026)

**Before running any terminal command, identify the target service and load the matching skill first.**

**Rule:**
1. Before ANY terminal command, identify the target by parsing the command for: IP addresses, hostnames, ports, service names, CLI tool names (clpctl, qm, pct, esxcli, vim-cmd, govc, duplicity, etc.)
2. Map the target to a skill using this table (non-exhaustive — also check the skills list for matches):

| Target | Skill |
|---|---|
| 184.105.199.100, :8443, clpctl, CloudPanel, hosting.asteratechnologies.com | `cloudpanel` |
| 184.105.199.105, MIAB, mailbox.asteradata, duplicity, nsd-control | `self-hosted-email` |
| 184.105.199.123, proxmox, qm, pct, pvesm, pvecluster | `proxmox-ve` |
| 184.105.199.125, ESXi, esxcli, vim-cmd, govc, vmkfstools | `proxmox-ve` (ESXi migration section) |
| 184.105.199.120/.124, iDRAC, Redfish, asteradmin | `proxmox-ve` (iDRAC reference) |
| hermes config, hermes setup, hermes model, gateway | `hermes-agent` |
| Apple Notes, Calendar, Reminders, iMessage | `apple-notes` / `apple-calendar` / `apple-reminders` / `imessage` |
| FatSecret, food logging, barcode lookup | `fatsecret` / `food-logging` |
| GoDaddy, domain management | `godaddy-domain-management` |
| nginx, SSL, certbot, Let's Encrypt | `nginx-ssl` / `ssl-certificate-management` |
| CloudPanel + Let's Encrypt | `cloudpanel` (has LE section) |

3. Load the skill with `skill_view(name='...')` BEFORE making the terminal call
4. If no skill matches, proceed — but note that no skill was found
5. If the user mentions a service by name (CloudPanel, MIAB, Proxmox, ESXi), ALWAYS load the matching skill even if the terminal command seems generic
6. When accessing a VM by IP through Proxmox SSH proxy, identify what service runs on that VM and load the appropriate skill — the IP alone tells you the service

**Enforcement:** Charon gate should block terminal commands when a matching skill exists but hasn't been loaded. If Charon can't detect the match (e.g., generic SSH to an IP), the agent must self-enforce by checking the table above before every terminal call.

### Use Application-Specific Commands, Not Manual Hacks (Jul 30 2026)

**When a skill documents a CLI command for a system, use that command — not manual hacks.**

**Rule:**
1. When a skill documents a CLI command (clpctl, vmkfstools, duplicity, etc.), use that command
2. Never manually chown/chmod when a permissions command exists (e.g., `clpctl system:permissions:reset`)
3. Never manually edit config files when an API or CLI exists (e.g., MIAB DNS API, not custom.yaml editing)
4. If unsure whether a CLI command exists, list available commands first (run `clpctl`, `esxcli`, etc. with no arguments to see help)
5. When a web UI is the documented method (e.g., CloudPanel Let's Encrypt for auto-renewal), use the web UI — do not substitute a one-off CLI command that lacks auto-renewal

### Read Reference Files Before Acting (Jul 30 2026)

**When a skill has reference files (references/*.md), read the relevant one before executing commands on that system.**

**Rule:**
1. The main SKILL.md is a summary — reference files have the actual commands, file paths, and pitfalls
2. Before running any command against a system documented in a skill, check if that skill has reference files and read the relevant one
3. If a reference file documents a specific workflow (e.g., MIAB static site hosting, DNS updates), follow that workflow — do not invent your own

### Stop and Ask When Uncertain (Jul 30 2026)

**If you do not know the exact command syntax, STOP and say so. Do not guess.**

**Rule:**
1. If you do not know the exact command syntax, STOP and say "I don't know the correct command, let me find it"
2. Do NOT guess syntax and hope it works
3. Do NOT try a command, see it fail, then try a different approach — read the documentation first
4. Do NOT silently substitute a different method (e.g., CLI instead of web UI) because you don't know the documented method

### Create Directory Structures Before Writing Files (Jul 30 2026)

**Before writing any file to a remote path, verify the parent directory exists.**

**Rule:**
1. Before writing any file to a remote path, verify the parent directory exists
2. If it doesn't exist, create it with `mkdir -p` and set proper ownership before writing
3. Verify the write succeeded before claiming success

## How to Add Directives

Directives added to this skill should be:
1. **Specific** — not vague platitudes
2. **Enforceable** — something that can be verified via tool call or code
3. **Critical** — a failure to follow causes real harm (wrong info, broken systems, user frustration)
4. **Accumulative** — each directive stays permanently, none are removed unless explicitly superseded