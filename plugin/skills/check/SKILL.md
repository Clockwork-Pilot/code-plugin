---
name: cwpilot:check
description: Run and verify feature constraints with cwpilot check.
---

Load for constraint work or when running `cwpilot check`; also read features_and_constraints for its full procedure.
A constraint is verified only after it has failed once and passed once; unverified constraints block source edits.
```bash
cwpilot check
cwpilot play create --playbook add_verified_constraint
```
Read `../features_and_constraints/SKILL.md` for the full constraint lifecycle and dev loop.
