# Release process

Every new version follows the same six-step ritual. Use the bundled
`release.sh` to automate steps 4-7; everything else is by hand because
it requires judgement (version bump, changelog content).

## Steps

1. **Bump `.claude-plugin/plugin.json`** `version` to the new value
   (e.g. `1.1.0` → `1.2.0`). SemVer:
   - MAJOR: breaking changes to install paths, CLI flags, or queue JSON schema
   - MINOR: new features, new flags, new OS support
   - PATCH: bug fixes only

2. **Add a `## [X.Y.Z] — YYYY-MM-DD`** section at the top of
   `CHANGELOG.md`, under `Added` / `Changed` / `Fixed` / `Removed` /
   `Deprecated` / `Security` subheadings (only the ones that apply).
   Be specific — readers should understand the user-visible impact
   without reading the diff.

3. **Commit** the version bump + changelog entry:
   ```bash
   git add .claude-plugin/plugin.json CHANGELOG.md
   git commit -m "Release vX.Y.Z"
   git push
   ```

4. **Tag** the commit:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z — <short summary>"
   git push origin vX.Y.Z
   ```

5. **Extract the release notes** from CHANGELOG:
   ```bash
   awk '/^## \[X\.Y\.Z\]/{flag=1; next} flag && /^## \[/{exit} flag' \
       CHANGELOG.md > .release-notes-vX.Y.Z.md
   ```

6. **Create the GitHub release**:
   ```bash
   gh release create vX.Y.Z \
       --title "vX.Y.Z — <short summary>" \
       --notes-file .release-notes-vX.Y.Z.md \
       --latest
   ```

7. **Clean up** the temp notes file:
   ```bash
   rm .release-notes-vX.Y.Z.md
   ```
   (Already in `.gitignore`, so it won't accidentally land in the repo.)

## The shortcut

`bash release.sh X.Y.Z "<short summary>"` does steps 4-7 in one go,
assuming you've already done 1-3 manually. It refuses to run if:
- `CHANGELOG.md` doesn't have a `## [X.Y.Z]` section
- `plugin.json` version doesn't match `X.Y.Z`
- The tag already exists
- The working tree has uncommitted changes

So you have to do the bump + changelog + commit yourself first; the
script picks up from "everything's committed, now publish".

## Why both a manual procedure AND a script

The script is the convenience path. The doc is the contract — it
captures the *intent* (one tag per release, notes sourced from
CHANGELOG, latest marker on the latest version), which a future
maintainer can re-implement if `release.sh` ever stops working.

## Verifying the latest tag matches plugin.json

A quick sanity check:

```bash
python3 -c "import json; print(json.load(open('.claude-plugin/plugin.json'))['version'])"
git describe --tags --abbrev=0
```

Both should print the same `X.Y.Z`. If they diverge, either a release
was skipped or someone tagged without bumping the manifest.
