# Role and authority

Read before moving code or deciding handbook authority; `ouroboros/domains.toml` owns module/domain assignments.

The handbook owns engineering practice, not inventories or history. Its “Documentation contract” defines rule form; BIBLE P7 owns document authority.

`ouroboros/domains.toml` is the SSOT of the module-to-domain assignment (1:1, complete
over the tracked runtime population) and pins the cross-domain dependency baseline;
`docs/DOMAIN_MAP.md` is generated from it. Read the map, edit the manifest, regenerate
both with `python scripts/check_domains.py --write`. A new cross-domain import
direction, a wider cycle group or a cross-domain literal copy is a red gate, because a
domain boundary is the owner's call, not a manifest edit. Enforced by
`tests/test_domain_manifest.py`; witness-level detail: `python scripts/domain_report.py`.

Rules here describe current practice or a deliberately enforced standard. When code and
prose disagree, inspect the implementation and history, repair the authoritative
surfaces together, and keep the failure a non-obvious rule prevents.
