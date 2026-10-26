# Frontend third-party notices

This directory preserves license and attribution text for the frontend's direct
dependencies, notable transitive components, and known separately licensed assets.
It does not extend the repository's MIT grant to third-party work.

[`manifest.json`](manifest.json) maps each component and installed version to its
license, scope, public upstream source, copied files, and SHA-256 hashes. Paths in
the manifest are relative to this directory; installed source paths are relative
to the repository root. Copied files retain their original bytes. Four direct
packages omit license files from their npm installation; their texts were fetched
from the upstream commit recorded as `gitHead` in the published npm metadata.

The collection is intentionally bounded. It is sufficient to explain the notices
provided with this source release; it is not a complete notice inventory for every
transitive dependency in a future browser bundle, standalone server, or container.
The lockfile identifies versions and integrity values; it is not itself a license
grant. No dependency binary or font binary is included in this directory.

## Terms requiring attention

- DOMPurify offers MPL-2.0 **or** Apache-2.0; this collection selects Apache-2.0.
- KaTeX software is MIT, while its bundled fonts retain SIL OFL 1.1, their
  copyright notices and Reserved Font Names. `katex-fonts/FONT-NOTICES.json`
  preserves the decoded metadata from all 20 installed TTFs. `Space.ttx` preserves
  an upstream font source notice. `OFL-1.1.txt` is the complete standard OFL text,
  copied verbatim from an installed Three example font; attribution for KaTeX
  remains in the KaTeX font notices.
- Lucide's original ISC file preserves the Feather MIT attribution. The separate
  `feather/LICENSE` supplies original Feather MIT terms from upstream v4.29.2 as
  a supplemental reference, not a claim that Feather is a resolved npm dependency.
- `caniuse-lite` browser data is CC-BY-4.0. Preserve its attribution when shipping
  that data. `axe-core` is an MPL-2.0 development dependency; its own license and
  included third-party notice remain applicable if the tool is redistributed.
- Next's vendored `@vercel/og`/Satori MPL texts, Glob logo terms, and Three example
  font terms (including MgOpen, OFL, Apache and CC0) are retained under the
  conditional entries. The app source did not
  reference these features/assets during this audit. Redistributing a complete
  installed package tree may nevertheless redistribute them. MPL-covered
  executable distribution requires the corresponding source to be available under
  MPL; an MIT notice does not replace that obligation.
- `frontend/public/{qwen.png,blue-knight-1.jpg,welcome.png,me.jpg}` are local ignored
  files excluded from this source release, according to the user's confirmed
  remote snapshot. This audit did not establish their source or permissions.
  Confirm those separately if these files are added to a later distribution.
  Their inventory entries supply no MIT grant or assertion of permission.

## Reproducing notices for a distribution

1. Resolve dependencies from the exact `frontend/package-lock.json` used for the
   release and retain its hash. Compare installed versions against the manifest.
2. Determine what is actually shipped: browser chunks, server output, public and
   font assets, a standalone directory, or a complete container/package tree.
   Include the build tools and otherwise unused example assets only when shipped.
3. Traverse every dependency and vendored component in those artifacts, including
   transitive packages. Collect their complete LICENSE/COPYING/NOTICE files and
   embedded asset notices. Package-level SPDX metadata can miss font and vendored
   licenses; this manifest records known examples. Keep bundler license comments.
4. Preserve the exact copyright and permission notices, applicable Apache NOTICE
   files, CC attribution, font terms and required source availability. Choose a
   permitted branch of each dual license explicitly. Add notices for anything
   outside this bounded collection and resolve image permissions if those local
   images are included in the distribution.
5. Ship the resulting notice/license directory with the distributed artifacts and
   expose it through the deployment's notice documentation. Keep this collection
   and the root `THIRD_PARTY_NOTICES.md` in source redistributions. Recalculate the
   manifest hashes whenever a copied file or dependency version changes.

These steps describe release preparation; they do not change or install packages.
