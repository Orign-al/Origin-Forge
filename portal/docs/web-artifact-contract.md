# Web Artifact Contract

The production Web artifact is a gzip-compressed tar archive rooted at `.next`. It is built by `deploy/scripts/web_artifact.py` only after a successful Next.js standalone build.

The archive contains only the production runtime closure; build cache, diagnostics, traces, and type-generation output are not release inputs. Required runtime closure:

- `.next/standalone/apps/web/server.js`
- `.next/standalone/node_modules` as a real directory
- `.next/standalone/apps/web/.next/static`
- every relative symlink target contained in and present within the archive

The builder and installer reject absolute or escaping member paths, absolute or escaping symbolic links, absolute or escaping hard links, missing link targets, device/FIFO members, duplicate members, external hardlinks, and unsupported filesystem entries. An archive may not depend on a build-root `node_modules`, `NODE_PATH`, a package-manager global store, or dependencies retained from another release.

Build after `pnpm --filter @h100-portal/web build`:

```bash
portal/deploy/scripts/web_artifact.py build \
  --source portal/apps/web/.next \
  --output /absolute/output/web.tar.gz \
  --manifest /absolute/output/web.manifest.json \
  --git-commit "$(git rev-parse HEAD)" \
  --git-tree "$(git rev-parse 'HEAD^{tree}')" \
  --production-parent a707fce314ebe905e37c486d3f94d98b03b0130c \
  --cli-version 'h100 1.0.0' \
  --migration-head c1d2e3f4a5b6
```

Production staging must use `deploy/scripts/install-web-artifact.sh`. That helper audits the archive before extraction, extracts into an isolated same-filesystem directory, audits the extracted tree, verifies the entrypoint and dependency closure, and only then synchronizes `.next` into the selected Web source directory. `install-runtime.sh` repeats the tree audit before its first runtime write.

Release acceptance must additionally start the extracted standalone server from two unrelated temporary roots with an empty `NODE_PATH` and no build tree available, request `/login` and a static asset over HTTP, and rehearse old to new to old installation without retaining dependencies across releases.
