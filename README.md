# release-images

This repository publishes basecamp's release container images. It does nothing else.

Each source repository builds its images into an edge package (`ghcr.io/basecamp/<name>-edge`)
with its own token. Only this repository can write the release package
(`ghcr.io/basecamp/<name>`), and it writes a release only after a reviewer approves it.

| package | source | edge | release |
|---|---|---|---|
| fizzy | [basecamp/fizzy](https://github.com/basecamp/fizzy) | `ghcr.io/basecamp/fizzy-edge` | `ghcr.io/basecamp/fizzy` |
| once-campfire | [basecamp/once-campfire](https://github.com/basecamp/once-campfire) | `ghcr.io/basecamp/once-campfire-edge` | `ghcr.io/basecamp/once-campfire` |
| writebook | [basecamp/writebook](https://github.com/basecamp/writebook) | `ghcr.io/basecamp/writebook-edge` | `ghcr.io/basecamp/writebook` |
| once-campfire-rust | [basecamp/once-campfire-rust](https://github.com/basecamp/once-campfire-rust) | `ghcr.io/basecamp/once-campfire-rust-edge` | `ghcr.io/basecamp/once-campfire-rust` |
| kamal | [basecamp/kamal](https://github.com/basecamp/kamal) | `ghcr.io/basecamp/kamal-edge` | `ghcr.io/basecamp/kamal` |
| kamal-skiff | [basecamp/kamal-skiff](https://github.com/basecamp/kamal-skiff) | `ghcr.io/basecamp/kamal-skiff-edge` | `ghcr.io/basecamp/kamal-skiff` |

`packages.json` is the source of truth for this list.

## How a release gets out

1. **Tag a release** in the source repository: push a `v*` tag (fizzy, once-campfire, writebook,
   once-campfire-rust), or create a GitHub release (kamal, kamal-skiff).
2. **The edge build runs** there and pushes a signed multi-platform image to
   `<name>-edge:<tag>`.
3. **This repository queues a promotion.** Every 15 minutes, [`promote.yml`](.github/workflows/promote.yml)
   scans each package for `v*` tags whose edge image has every platform but whose release tag
   doesn't exist yet. For each one it starts a run named **Promote \<package\> \<tag\>**.
4. **A reviewer approves** the run's deployment to `release-<package>`. The run then copies the
   approved digest to the release image, signs it, and attests a record of the promotion.

Not waiting for the scan: `gh workflow run promote.yml -R basecamp/release-images -f package=kamal -f tag=v2.13.0`.

### What the run checks before it asks for approval

The **Resolve** job's summary shows what you are approving:

- the source commit the tag points to, and whether it is on the default branch (a maintenance
  release on another branch is flagged, not refused);
- the edge digest and its platforms;
- that the edge image was signed by the source repository's own build workflow running on that
  tag at that commit (`cosign verify`). An image pushed to the edge package any other way fails here.

The promote job copies that digest and nothing else, so a later push to the edge tag changes
nothing.

### What it does after approval

- **Who approved.** Where the environment has "prevent self-review" on, the person who pushed the
  tag (or created the release) can't be the only approver. GitHub enforces that in the source
  repository's own environments; here the run is started by the scan, so the job checks it itself
  and fails if needed. Another reviewer then re-runs the job and approves.
- **One publish per package at a time.** The publish job runs in a per-package concurrency group,
  so its checks read the registry as it is when it writes. Waiting for approval doesn't hold it.
- **Version tags are write-once.** Before writing `vX.Y.Z` (and `X.Y.Z` where the package uses it),
  the job reads the existing tag. The same digest is skipped, so a re-run is harmless. Any other
  digest stops the run before anything is written: release a new version instead.
- **`latest` only moves forward.** It moves only for a non-prerelease that is at least every release
  already in the package, so approving an older release after a newer one never moves it back.
  The series tags (`1.2`, `1`) that once-campfire, writebook, once-campfire-rust and fizzy publish
  follow the same rule within their series. Prereleases (`v1.2.3-rc.1`, `v2.0.0.beta1`) get only
  their exact tags.
- **Copy, verify, sign, attest.** `crane copy` keeps the digest, so the release index is the edge
  index byte for byte. Every tag is then checked against the approved digest and signed with cosign
  (keyless). Last, the run attests a promotion record on the image: package, tag, index digest,
  source repository and commit, the edge image, the tags set, and who approved.

The decisions live in `scripts/promote.py` (`decide_tags`, `promotion_predicate`), with offline tests
in `tests/`: `python3 -m unittest discover -s tests -v`.

### Immutability: what enforces it

This workflow enforces the write-once and forward-only rules. ghcr doesn't: anyone with admin on a
package can still delete a tag or push over it directly. The promotion record is the check against
that. A release image is genuine when its digest carries an attestation from this workflow whose
record names that tag and the approvers you expect.

### Pending, rejected and failed promotions

- A promotion waiting for approval isn't queued again by the next scan.
- One that was rejected, timed out or failed isn't queued again either; the scan summary links it.
  To retry, dispatch it by hand (command above).

### Verifying a release image

```
gh attestation verify oci://ghcr.io/basecamp/<pkg>:<tag> --owner basecamp \
  --signer-workflow basecamp/release-images/.github/workflows/promote.yml
```

It succeeds only for a digest this workflow promoted. Add `--format json` and read
`.[].verificationResult.statement.predicate.buildDefinition.externalParameters` for the record: the
package, the tag, the digest, the source repository and commit, and the approvers. Check that its
`tag` is the tag you pulled.

The cosign signature is there too:

```
cosign verify ghcr.io/basecamp/<pkg>:<tag> \
  --certificate-identity https://github.com/basecamp/release-images/.github/workflows/promote.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Adding a package

1. **Source repository.** Build into `ghcr.io/basecamp/<name>-edge` with the repository's own
   `GITHUB_TOKEN`, as a multi-platform index tagged with the release tag (`<name>-edge:v1.2.3`).
   Sign it with cosign keyless in the same workflow run, from the tag's ref: the identity checked
   is `https://github.com/<source>/.github/workflows/<workflow>@refs/tags/<tag>`.
2. **`packages.json`.** Add an entry: `source`, `workflow` (the file that signs the edge image),
   `edge`, `release`, `platforms`, and `tags`, which lists `ref` first, then any of `version`,
   `minor`, `major`, `latest`.
3. **Environment.** Create `release-<name>` here with its required reviewers and a deployment
   branch policy of `main` only. A run refuses a package whose environment is missing or has no
   reviewers.
4. **Package settings** (an org owner, in
   `https://github.com/orgs/basecamp/packages/container/<name>/settings`):
   - Manage Actions access: add `basecamp/release-images` with **Write**, set the source
     repository to **Read**, and untick "Inherit access from source repository".
   - Make `<name>-edge` public. The scan and the promote job read it anonymously.

## Housekeeping

- Issues, wiki, projects and discussions are off. A failed scan or promotion surfaces through
  GitHub Actions notifications (to whoever triggered the run, and to the environment's reviewers
  for pending approvals); watch the repository's Actions to see them all.
- Changes come through pull requests, which only collaborators can open, and land as merge commits.
  Anyone who can change `promote.yml` or approve in its environments controls these releases, so
  keep both sets small.
- Every action is pinned by commit SHA (the repository requires it), and crane by version and
  checksum.
- GitHub turns off scheduled workflows in a public repository after 60 days without activity, and
  emails a warning first. If promotions stop appearing, run
  `gh workflow enable promote.yml -R basecamp/release-images`, or dispatch them by hand as above.
