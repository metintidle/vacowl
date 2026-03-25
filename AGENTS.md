## Learned User Preferences

- Do not edit `.cursor/plans/wp_media_optimize_scripts_1709288e.plan.md` when implementing plan to-dos unless the user asks to change the plan.
- Add or expand `README.md` only if the user explicitly asks for usage or project documentation.
- Prioritize processing, logging, and audits for `2022/10` and `2022/11` under the uploads root because those months hold the largest media on this site.
- When updating media references, prefer checking/patching the live database rather than relying on the SQL dump.

## Learned Workspace Facts

- Media scan, compression, WebP, and orphan moves target `wp-content/uploads` only (the tree with year/month folders), not the whole `wp-content` directory.
- The in-repo WordPress dump used for indexing and SQL work is `vcawolor_wp1.sql` at the project root.
- Video work for this site assumes a web-oriented cap near 1200 px on the long edge and output sizes around or under 20 MB, without upscaling smaller sources.
- Image work in scope uses a long-edge cap near 1920 px and WebP output via `cwebp` with default quality roughly in the 80–85 range unless configured otherwise.
- Any bulk URL or extension changes in the SQL dump must preserve PHP-serialized `meta_value` strings (use a PHP unserialize or serialize helper, not naive find-and-replace).
- Paths and URLs in the dump may reference `vcawol.org.au` and other forms; normalize hits to one canonical path relative to the uploads root for indexing and unused-file detection.
