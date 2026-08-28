---
name: file-verification-protocol
description: "Complete file verification sequence for complex file formats"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [macos, linux, windows]
tags: [file, verification, protocol, essentials]
---

# File Verification Protocol

## Complete Verification Sequence for Complex Files

When generating complex file formats (like .sh3d), follow this exact multi-step verification process to ensure files actually work, not just that they generate.

### Step 1: Structure Validation
```bash
# Check basic file structure
file /path/to/file.ext
ls -la /path/to/
```

### Step 2: Format-Specific Validation
```bash
# For XML-based files (.sh3d, .xml)
python3 -c "import xml.etree.ElementTree as ET; ET.parse('file.sh3d')"

# For ZIP-based files (.sh3d, .jar)
unzip -l file.sh3d

# For JSON files
python3 -c "import json; json.load(open('file.json'))"

# For text files
head -5 file.txt
```

### Step 3: Application Launch Test
```bash
# For .sh3d files
open -a "Sweet Home 3D" file.sh3d

# For PDF files
open -a "Preview" file.pdf

# For image files
open -a "Preview" file.png
```

### Step 4: Application Response Verification
**CRITICAL:** Application launch success ≠ file loaded successfully
```bash
# Check for actual windows/processes
ps aux | grep -i "application_name"
# Or use cua_driver for GUI apps
mcp__cua_driver__list_windows | grep -i "application_name"
```

### Step 5: Content Verification
```bash
# For .sh3d files, check XML content
unzip -p file.sh3d Home.xml | head -20

# For any file, check if content makes sense
cat file.txt | head -10
```

## Common Failure Patterns

### Silent Failures
- **App launches but no window appears** = file format error
- **App opens with empty content** = missing required components
- **App crashes on open** = corrupt file or invalid format
- **File shows as empty** = missing content or wrong encoding

### Error Detection
Look for these signs of failure:
- Exit code 0 from `open` command but no windows
- Application process appears but immediately disappears
- File size is 0 or much smaller than expected
- Format validation passes but app can't open it

## Specific File Type Protocols

### Sweet Home 3D (.sh3d) Files
```bash
# 1. Check ZIP structure
unzip -l file.sh3d

# 2. Check XML validity
python3 -c "import xml.etree.ElementTree as ET; ET.parse('Home.xml')"

# 3. Check required files exist
unzip -p file.sh3d Home.xml > /dev/null
unzip -p file.sh3d 0 > /dev/null  # First model file

# 4. Launch and verify window
open -a "Sweet Home 3D" file.sh3d
sleep 3
mcp__cua_driver__list_windows | grep -i "sweet home 3d"
```

### PDF Files
```bash
# 1. Check PDF structure
file file.pdf
pdfinfo file.pdf 2>/dev/null || echo "PDF may be corrupt"

# 2. Check page count
pdfinfo file.pdf | grep "Pages" || echo "Cannot read PDF info"

# 3. Open and verify
open -a "Preview" file.pdf
sleep 2
ps aux | grep -i "preview" | grep -v grep
```

## Origin

**Jul 20, 2026.** Generated Sweet Home 3D .sh3d files that passed XML validation and ZIP structure checks but failed to load in the application. The app would launch (exit code 0) but show no windows, indicating silent failure. Multiple iterations were needed to identify the root cause: missing 3D model references for furniture elements.

## Key Learnings

1. **Format validation ≠ functionality** - A file can be syntactically correct but still not work
2. **Application response is the final test** - Launch success doesn't guarantee file loading
3. **Multi-step verification is essential** - No single test catches all failure modes
4. **User feedback is critical** - Silent failures only become apparent when users try to use the files

## When to Use This Protocol

Use this complete verification for:
- Complex file formats (.sh3d, .jar, .pdf, .docx)
- Files that require specific applications to open
- Files generated programmatically (not manually created)
- Files that will be delivered to users for actual use
- Any file where "it should work" but you're not 100% sure

## Prevention Strategies

### 1. Test During Generation
- Verify each step immediately after generation
- Don't wait until the end to check functionality
- Keep intermediate results for debugging

### 2. User Testing
- Always ask users to verify files actually work
- Get confirmation before claiming success
- Document any issues for future improvements

### 3. Documentation
- Keep a log of what works and what doesn't
- Document specific failure patterns for each file type
- Update protocols based on real-world usage