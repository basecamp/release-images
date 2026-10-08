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
   approved digest to the release image, signs it and attests it.

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
- **Tags.** The exact version always (`v1.2.3`, and `1.2.3` where the package uses it). `latest`
  and the series tags (`1.2`, `1`) move only when this is the highest release published so far
  in that series, so approving an older release after a newer one never moves them back.
  Prereleases (`v1.2.3-rc.1`, `v2.0.0.beta1`) get only their exact tags.
- **Never overwrite.** A release tag that already exists with another digest stops the run.
- **Copy, verify, sign, attest.** `crane copy` keeps the digest, so the release index is the edge
  index byte for byte. Every tag is then checked against the approved digest, signed with cosign
  (keyless), and given a provenance attestation.

### Pending, rejected and failed promotions

- A promotion waiting for approval isn't queued again by the next scan.
- One that was rejected, timed out or failed isn't queued again either; the scan summary links it.
  To retry, dispatch it by hand (command above).

### Verifying a release image

```
cosign verify ghcr.io/basecamp/kamal:v2.13.0 \
  --certificate-identity https://github.com/basecamp/release-images/.github/workflows/promote.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify oci://ghcr.io/basecamp/kamal:v2.13.0 --repo basecamp/release-images
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

- Every action is pinned by commit SHA, and crane by version and checksum.
- GitHub turns off scheduled workflows in a public repository after 60 days without activity, and
  emails a warning first. If promotions stop appearing, run
  `gh workflow enable promote.yml -R basecamp/release-images`, or dispatch them by hand as above.
- Changes to this repository come through pull requests. Anyone who can change `promote.yml` or
  approve in its environments controls these releases, so keep both sets small.
