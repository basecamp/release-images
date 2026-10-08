#!/usr/bin/env python3
"""Find and promote release images. Used by .github/workflows/promote.yml; see README.md.

  scan     list each package's v* tags whose edge image is complete and whose release tag
           is missing, and drop the ones that already have a promote run (pending or failed)
  resolve  check one package+tag before it waits for approval: the environment, the edge
           index and its platforms, the source commit
  approve  after approval: who approved, and the self-review rule
  plan     in the serialized publish job: the write-once check and the tags to set
  verify   every release tag now resolves to the approved digest
  record   the promotion record attested on the release image

Reads only: the GitHub API with the run's GITHUB_TOKEN, and ghcr.io anonymously (the edge
and release packages are public). Writes nothing; promote.yml does the pushing.
"""
import hashlib, json, os, re, sys, urllib.error, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = json.load(open(os.path.join(ROOT, "packages.json")))
API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
REPO = os.environ.get("GITHUB_REPOSITORY", "basecamp/release-images")
WORKFLOW = "promote.yml"
ISSUER = "https://token.actions.githubusercontent.com"

# v1.2.3, v1.2, v1.2.3-rc.1, v2.0.0.beta1. A prerelease suffix starts with a letter.
TAG_RE = re.compile(r"^v(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-.]([A-Za-z][0-9A-Za-z.-]*))?$")
TITLE_RE = re.compile(r"^Promote (\S+) (\S+)$")
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
INDEX_TYPES = {"application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json"}


class Unreadable(Exception):
    pass


def die(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{k}={v}" for k, v in kv.items()]
    if path:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    else:
        print("\n".join(f"output {l}" for l in lines))


def summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(text + "\n")
    else:
        print(text)


def parse(tag):
    """(major, minor, patch, prerelease) or None when the tag is not a release tag."""
    m = TAG_RE.match(tag)
    if not m or len(tag) > 128:
        return None
    return int(m[1]), int(m[2] or 0), int(m[3] or 0), m[4]


def package(name):
    if name not in CONFIG:
        die(f"unknown package {name!r}; packages.json has: {', '.join(CONFIG)}")
    return CONFIG[name]


# ---------------------------------------------------------------- GitHub

def gh(path, method="GET", body=None):
    req = urllib.request.Request(API + path if path.startswith("/") else path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        die(f"GitHub API {method} {path}: HTTP {e.code} {e.read()[:300]!r}")


def graphql(query, **variables):
    r = gh("/graphql", "POST", {"query": query, "variables": variables})
    if r is None or r.get("errors"):
        die(f"GraphQL: {r and r.get('errors')}")
    return r["data"]


def source_tags(source):
    refs = gh(f"/repos/{source}/git/matching-refs/tags/v") or []
    return {r["ref"][len("refs/tags/"):] for r in refs}


def promote_runs():
    """The most recent promote run per (package, tag), newest first."""
    r = gh(f"/repos/{REPO}/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=100")
    runs = {}
    for run in (r or {}).get("workflow_runs", []):
        m = TITLE_RE.match(run.get("display_title") or "")
        if m:
            runs.setdefault((m[1], m[2]), run)
    return runs


def environment(name):
    env = gh(f"/repos/{REPO}/environments/{name}")
    if env is None:
        die(f"environment {name} does not exist in {REPO}. Create it (with required reviewers) "
            "before promoting; a job naming a missing environment would create it unprotected.")
    rules = [r for r in env.get("protection_rules", []) if r.get("type") == "required_reviewers"]
    if not rules or not rules[0].get("reviewers"):
        die(f"environment {name} has no required reviewers")
    if env.get("can_admins_bypass"):
        die(f"environment {name} lets admins bypass its reviewers")
    return bool(rules[0].get("prevent_self_review"))


# ---------------------------------------------------------------- ghcr.io

class Registry:
    def __init__(self):
        self.tokens = {}

    @staticmethod
    def split(image):
        host, _, repo = image.partition("/")
        if host != "ghcr.io":
            die(f"only ghcr.io images are supported: {image}")
        return repo

    def request(self, repo, path, accept=None):
        if repo not in self.tokens:
            url = f"https://ghcr.io/token?service=ghcr.io&scope=repository:{repo}:pull"
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    self.tokens[repo] = json.load(r)["token"]
            except urllib.error.HTTPError as e:
                raise Unreadable(f"ghcr.io/{repo}: HTTP {e.code} (missing, or not public)")
        req = urllib.request.Request(f"https://ghcr.io{path}")
        req.add_header("Authorization", f"Bearer {self.tokens[repo]}")
        if accept:
            req.add_header("Accept", accept)
        try:
            return urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise Unreadable(f"ghcr.io{path}: HTTP {e.code}")

    def tags(self, image):
        repo, out = self.split(image), set()
        path = f"/v2/{repo}/tags/list?n=1000"
        while path:
            r = self.request(repo, path)
            if r is None:
                raise Unreadable(f"{image}: no such package")
            with r:
                out.update(json.load(r).get("tags") or [])
                link = r.headers.get("Link") or ""
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            path = urllib.parse.urlparse(m[1])._replace(scheme="", netloc="").geturl() if m else None
        return out

    def manifest(self, image, ref):
        """(digest, manifest) for a tag or digest, or None."""
        repo = self.split(image)
        r = self.request(repo, f"/v2/{repo}/manifests/{ref}", ACCEPT)
        if r is None:
            return None
        with r:
            body = r.read()
            header = r.headers.get("Docker-Content-Digest")
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if header and header != digest:
            die(f"{image}:{ref}: registry digest {header} does not match the body ({digest})")
        return digest, json.loads(body)


def platforms(manifest):
    if manifest.get("mediaType") not in INDEX_TYPES and "manifests" not in manifest:
        return set()
    out = set()
    for m in manifest.get("manifests", []):
        p = m.get("platform") or {}
        if p.get("os") in (None, "unknown"):
            continue  # attestation manifests
        out.add("/".join(x for x in (p.get("os"), p.get("architecture"), p.get("variant")) if x))
    return out


def edge_index(reg, pkg, tag):
    """(digest, missing platforms) of the edge image for a tag, or None if it isn't there."""
    found = reg.manifest(pkg["edge"], tag)
    if found is None:
        return None
    digest, manifest = found
    return digest, sorted(set(pkg["platforms"]) - platforms(manifest))


# ---------------------------------------------------------------- decisions (pure; tests/test_promote.py)

class Refused(Exception):
    pass


WRITE_ONCE = ("ref", "version")  # vX.Y.Z and X.Y.Z name one release, forever
MOVING = ("latest", "minor", "major")  # move forward only


def decide_tags(pkg, tag, digest, existing, digest_of):
    """(write, skip) for promoting `digest` as `tag`, given the release package's tags now.

    existing: the release package's tag names. digest_of(t): the digest tag t points at.

    Version tags are write-once. One that exists with this digest is skipped (a re-run); one
    that exists with any other digest refuses the whole promotion before anything is written.
    `latest`, and the series tags `X.Y` and `X`, move only for a non-prerelease that is at
    least every release already published (in that series), so they never move backwards.
    """
    parsed = parse(tag)
    if not parsed:
        raise Refused(f"{tag!r} is not a release tag (v1.2.3, v1.2.3-rc.1, v2.0.0.beta1)")
    if not pkg["tags"] or pkg["tags"][0] != "ref":
        raise Refused('packages.json: "tags" must list "ref" first')
    major, minor, patch, pre = parsed
    key = (major, minor, patch)
    released = [p[:3] for p in map(parse, existing) if p and not p[3]]
    names = {"ref": tag, "version": tag[1:], "minor": f"{major}.{minor}", "major": f"{major}", "latest": "latest"}
    forward = {
        "latest": all(key >= r for r in released),
        "minor": all(key >= r for r in released if r[:2] == key[:2]),
        "major": all(key >= r for r in released if r[0] == major),
    }
    write, skip = [], []
    for kind in pkg["tags"]:
        t = names[kind]
        if kind in WRITE_ONCE:
            current = digest_of(t) if t in existing else None
            if current is None:
                write.append(t)
            elif current == digest:
                skip.append(t)
            else:
                raise Refused(f"{t} already exists as {current}, not {digest}. Release tags are "
                              "write-once: promote a new version instead.")
        elif kind in MOVING:
            if not pre and forward[kind]:
                write.append(t)
        else:
            raise Refused(f"packages.json: unknown tag kind {kind!r}")
    return write, skip


def promotion_predicate(name, pkg, tag, digest, commit, approvers, tags, run_url, builder):
    """SLSA v1 provenance recording one promotion. `gh attestation verify` checks this type by default."""
    return {
        "buildDefinition": {
            "buildType": "https://github.com/basecamp/release-images/promotion/v1",
            "externalParameters": {
                "package": name,
                "tag": tag,
                "digest": digest,
                "tags": tags,
                "image": pkg["release"],
                "source": {"repository": pkg["source"], "commit": commit, "workflow": pkg["workflow"],
                           "image": f"{pkg['edge']}@{digest}"},
                "approvers": approvers,
            },
            "internalParameters": {},
            "resolvedDependencies": [
                {"uri": f"git+https://github.com/{pkg['source']}@refs/tags/{tag}", "digest": {"gitCommit": commit}},
                {"uri": f"oci://{pkg['edge']}", "digest": {"sha256": digest.split(":", 1)[1]}},
            ],
        },
        "runDetails": {
            "builder": {"id": builder},
            "metadata": {"invocationId": run_url},
        },
    }


# ---------------------------------------------------------------- commands

def scan():
    only = os.environ.get("PACKAGE") or ""
    if only:
        package(only)
    names = [only] if only else list(CONFIG)
    reg, ready = Registry(), []
    rows = ["| package | tag | edge digest | state |", "|---|---|---|---|"]
    for name in names:
        pkg = package(name)
        tags = {t for t in source_tags(pkg["source"]) if parse(t)}
        try:
            edge = reg.tags(pkg["edge"])
        except Unreadable as e:
            print(f"::warning::{name}: edge package unreadable, skipped: {e}")
            rows.append(f"| {name} | | | edge unreadable: {e} |")
            continue
        try:
            released = reg.tags(pkg["release"])
        except Unreadable as e:
            die(f"{name}: release package unreadable: {e}")
        for tag in sorted((tags & edge) - released, key=lambda t: (parse(t)[:3], parse(t)[3] or "~")):
            found = edge_index(reg, pkg, tag)
            if found is None:
                continue
            digest, missing = found
            if missing:
                rows.append(f"| {name} | {tag} | `{digest[:19]}` | waiting for {', '.join(missing)} |")
                continue
            ready.append({"package": name, "tag": tag, "digest": digest})
    runs = promote_runs() if ready else {}
    queue = []
    for c in ready:
        run = runs.get((c["package"], c["tag"]))
        if run and run["status"] != "completed":
            state = f"[pending]({run['html_url']}): not queued again"
        elif run and run["conclusion"] not in ("success", "skipped"):
            state = (f"[{run['conclusion']}]({run['html_url']}): not queued again; to retry, "
                     f"`gh workflow run {WORKFLOW} -R {REPO} -f package={c['package']} -f tag={c['tag']}`")
        else:
            state = "queued"
            queue.append({"package": c["package"], "tag": c["tag"]})
        rows.append(f"| {c['package']} | {c['tag']} | `{c['digest'][:19]}` | {state} |")
    summary("### Promotion scan\n\n" + ("\n".join(rows) if len(rows) > 2 else "Nothing to promote."))
    output(queue=json.dumps(queue, separators=(",", ":")))


def resolve():
    name, tag = os.environ.get("PACKAGE") or "", os.environ.get("TAG") or ""
    pkg = package(name)
    if not parse(tag):
        die(f"{tag!r} is not a release tag (v1.2.3, v1.2.3-rc.1, v2.0.0.beta1)")
    env_name = f"release-{name}"
    prevent = environment(env_name)
    reg = Registry()
    try:
        found = edge_index(reg, pkg, tag)
    except Unreadable as e:
        die(f"{pkg['edge']}: {e}")
    if found is None:
        die(f"{pkg['edge']}:{tag} does not exist. Did the source build for {tag} finish?")
    digest, missing = found
    if missing:
        die(f"{pkg['edge']}:{tag} lacks {', '.join(missing)}")
    try:
        current = reg.manifest(pkg["release"], tag)
    except Unreadable as e:
        die(f"{pkg['release']}: {e}")
    if current is not None:
        if current[0] != digest:
            die(f"{pkg['release']}:{tag} already exists as {current[0]}, but {pkg['edge']}:{tag} is {digest}. "
                "Release tags are write-once: release a new version instead.")
        summary(f"### {name} {tag}: already promoted\n\n`{pkg['release']}:{tag}` is `{digest}`, the edge digest. "
                "Nothing to do. (To finish a partly failed promotion, re-run that run's failed jobs.)")
        output(ready="false")
        return
    owner, repo = pkg["source"].split("/")
    data = graphql("""query($owner:String!,$name:String!,$ref:String!,$head:String!){
      repository(owner:$owner,name:$name){
        ref(qualifiedName:$ref){target{__typename oid ... on Tag{target{__typename oid}}}}
        defaultBranchRef{name compare(headRef:$head){status aheadBy behindBy}}}}""",
                   owner=owner, name=repo, ref=f"refs/tags/{tag}", head=tag)["repository"]
    if not data["ref"]:
        die(f"{pkg['source']} has no tag {tag}")
    target = data["ref"]["target"]
    commit = target["target"]["oid"] if target["__typename"] == "Tag" else target["oid"]
    if target["__typename"] == "Tag" and target["target"]["__typename"] != "Commit":
        die(f"{pkg['source']} {tag} is not a tag of a commit")
    default = data["defaultBranchRef"]
    status = (default["compare"] or {}).get("status", "UNKNOWN")
    on_default = status in ("BEHIND", "IDENTICAL")
    identity = f"https://github.com/{pkg['source']}/.github/workflows/{pkg['workflow']}@refs/tags/{tag}"
    if not on_default:
        print(f"::warning::{pkg['source']} {tag} ({commit[:12]}) is not on {default['name']} ({status.lower()}). "
              "Approve only if it is a deliberate maintenance release.")
    summary("\n".join([
        f"### Promote {name} {tag}",
        "",
        "| | |", "|---|---|",
        f"| source | [{pkg['source']}@{tag}](https://github.com/{pkg['source']}/tree/{tag}) |",
        f"| commit | [`{commit[:12]}`](https://github.com/{pkg['source']}/commit/{commit}) |",
        f"| on {default['name']} | {'yes' if on_default else '**no: ' + status.lower() + '**'} |",
        f"| edge image | `{pkg['edge']}@{digest}` |",
        f"| platforms | {', '.join(pkg['platforms'])} |",
        f"| release image | `{pkg['release']}` |",
        f"| signed by | `{identity}` (checked in the next step) |",
        f"| environment | `{env_name}` (prevent self-review: {'on' if prevent else 'off'}) |",
        "",
        "The promote job copies exactly this digest. Its tags (the version, plus `latest` and the "
        "series tags where this is the highest release) are worked out after approval.",
    ]))
    output(ready="true", environment=env_name, digest=digest, commit=commit, identity=identity,
           edge=pkg["edge"], release=pkg["release"])


def approve():
    """After approval, in the environment job: who approved, and the self-review rule."""
    name, tag = os.environ.get("PACKAGE") or "", os.environ.get("TAG") or ""
    pkg = package(name)
    env_name = f"release-{name}"
    run_id = os.environ["GITHUB_RUN_ID"]
    approvals = gh(f"/repos/{REPO}/actions/runs/{run_id}/approvals") or []
    approvers = sorted({a["user"]["login"] for a in approvals if a.get("state") == "approved"
                        and any(e.get("name") == env_name for e in a.get("environments", []))})
    if not approvers:
        die("no approval recorded for this run")
    if environment(env_name):
        # This run was queued by github-actions[bot] (or dispatched by a person, whom GitHub
        # already stops from approving). Carry the source repository's rule across: the person
        # who pushed the tag or created the release may not be the only approver.
        q = urllib.parse.urlencode({"branch": tag, "per_page": 20})
        runs = (gh(f"/repos/{pkg['source']}/actions/workflows/{pkg['workflow']}/runs?{q}") or {}).get("workflow_runs", [])
        authors = {u["login"] for r in runs for u in (r.get("actor"), r.get("triggering_actor")) if u}
        if not runs:
            die(f"no {pkg['workflow']} run for {tag} in {pkg['source']}; cannot check who released it")
        if set(approvers) <= authors:
            die(f"approved only by {', '.join(approvers)}, who started the {tag} build in "
                f"{pkg['source']}. Another reviewer must approve: re-run this job.")
    summary(f"Approved by {', '.join(approvers)}.")
    output(approvers=",".join(approvers))


def plan():
    """In the serialized publish job: which tags to write, read from the registry right now."""
    name, tag, digest = (os.environ.get(k) or "" for k in ("PACKAGE", "TAG", "DIGEST"))
    pkg = package(name)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        die(f"bad digest {digest!r}")
    reg = Registry()
    existing = reg.tags(pkg["release"])
    lookup = lambda t: (reg.manifest(pkg["release"], t) or (None,))[0]
    try:
        write, skip = decide_tags(pkg, tag, digest, existing, lookup)
    except Refused as e:
        die(str(e))
    lines = [f"Release tags on `{pkg['release']}@{digest}`:"]
    lines += [f"- `{t}`: write" for t in write] + [f"- `{t}`: already this digest, skipped" for t in skip]
    summary("\n".join(lines))
    output(write=" ".join(write), tags=" ".join(skip + write), edge=pkg["edge"], release=pkg["release"])


def record():
    """Write the promotion record (the attestation predicate) to $PREDICATE."""
    name, tag, digest, commit, approvers, tags = (os.environ.get(k) or "" for k in
                                                  ("PACKAGE", "TAG", "DIGEST", "COMMIT", "APPROVERS", "TAGS"))
    pkg = package(name)
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    pred = promotion_predicate(
        name=name, pkg=pkg, tag=tag, digest=digest, commit=commit,
        approvers=[a for a in approvers.split(",") if a], tags=tags.split(),
        run_url=f"{server}/{REPO}/actions/runs/{os.environ.get('GITHUB_RUN_ID')}/attempts/{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}",
        builder=f"{server}/{os.environ.get('GITHUB_WORKFLOW_REF', REPO + '/.github/workflows/' + WORKFLOW + '@refs/heads/main')}")
    with open(os.environ["PREDICATE"], "w") as f:
        json.dump(pred, f, indent=2)
    print(json.dumps(pred["buildDefinition"]["externalParameters"], indent=2))


def verify():
    name, digest, tags = (os.environ.get(k) or "" for k in ("PACKAGE", "DIGEST", "TAGS"))
    pkg = package(name)
    reg, bad = Registry(), []
    for t in tags.split():
        found = reg.manifest(pkg["release"], t)
        got = found[0] if found else "missing"
        print(f"{pkg['release']}:{t} -> {got}")
        if got != digest:
            bad.append(f"{t} is {got}")
    if bad:
        die(f"expected {digest}; " + "; ".join(bad))
    summary(f"Verified: {', '.join(f'`{t}`' for t in tags.split())} on `{pkg['release']}` resolve to `{digest}`.")


if __name__ == "__main__":
    cmds = {"scan": scan, "resolve": resolve, "approve": approve, "plan": plan, "verify": verify, "record": record}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        die(f"usage: promote.py {'|'.join(cmds)}")
    cmds[sys.argv[1]]()
