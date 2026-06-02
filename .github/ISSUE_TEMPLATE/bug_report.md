---
name: Bug report
about: Something broken or behaving wrong
title: ''
labels: bug
assignees: ''
---

**What happened**

(One sentence is fine.)

**Expected behavior**

**Steps to reproduce**

1.
2.
3.

**Environment**

- Server commit hash: `git -C /path/to/server rev-parse --short HEAD`
- OS / distro:
- Python version: `python3 --version`
- iOS version (if client-side):

**Relevant logs**

```
# paste output of:
# sudo journalctl -u musictidy --since '10min ago' | tail -100
```

(Trim anything with `APP_PASSWORD`, `ACOUSTID_API_KEY`, or auth tokens.)

**Additional context**

(screenshots, related issues, anything else)
