# Creative Factory Desktop

Private source and deployment repository for Creative Factory 0.1.17 (schema 3).
The company Mac runs independently of Codex, a Work conversation, and the developer Mac.

- [Company Mac deployment and update](docs/COMPANY_MAC.md)
- [Migration audit and outstanding acceptance](docs/MIGRATION_AUDIT.md)
- [Engine provenance](manifests/engine-snapshot.json)
- [Pinned runtime manifest](manifests/runtime-lock.json)
- [Resource licensing](manifests/resource-licenses.json)

Do not commit API keys, `.env`, databases, personal job history, videos, caches, virtualenvs,
or generated `.app` bundles. Configure company-owned provider keys once through Settings;
the existing `com.autocut.providers` Keychain service is preserved on update.

Mathilde distribution is intentionally skipped. Until its authorized matching font is
provisioned separately, a full company App build stops explicitly. ProPainter is not included
in company production because its license is noncommercial. Production release approval
also waits for company Golden-video acceptance; see `manifests/production.json`.
