#!/usr/bin/env python3
"""Find and promote release images. Used by .github/workflows/promote.yml; see README.md.

  scan     list each package's v* tags whose edge image is complete and whose release tag
           is missing, and drop the ones that already have a promote run (pending or failed)
  resolve  check one package+tag before it waits for approval: the environment, the edge
           index and its platforms, the source commit
  plan     after approval: the self-review check, the immutability check and the tags to set
  verify   every release tag now resolves to the approved digest

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


# ---------------------------------------------------------------- tags

def release_tags(pkg, tag, existing):
    """The release tags to set for `tag`, given the tags the release package already has.

    The exact version always. Moving tags (latest, X.Y, X) only for a non-prerelease that is
    at least the highest release already published in that series, so approving an older
    release after a newer one never moves them back.
    """
    major, minor, patch, pre = parse(tag)
    key = (major, minor, patch)
    released = [p[:3] for p in map(parse, existing) if p and not p[3]]
    out = []
    for kind in pkg["tags"]:
        if kind == "ref":
            out.append(tag)
        elif kind == "version":
            out.append(tag[1:])
        elif pre:
            continue
        elif kind == "latest" and all(key >= r for r in released):
            out.append("latest")
        elif kind == "minor" and all(key >= r for r in released if r[:2] == key[:2]):
            out.append(f"{major}.{minor}")
        elif kind == "major" and all(key >= r for r in released if r[0] == major):
            out.append(f"{major}")
    return out


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
        same = current[0] == digest
        summary(f"### {name} {tag}: already in {pkg['release']}\n\n"
                f"`{current[0]}` ({'the edge digest' if same else 'differs from edge ' + digest}). "
                "Nothing to promote; release tags are never overwritten.")
        if not same:
            print(f"::warning::{pkg['release']}:{tag} exists with another digest; not overwritten")
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


def plan():
    name, tag, digest = (os.environ.get(k) or "" for k in ("PACKAGE", "TAG", "DIGEST"))
    pkg = package(name)
    if not parse(tag) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        die("bad tag or digest")
    if environment(f"release-{name}"):
        # This run was queued by github-actions[bot] (or dispatched by a person, whom GitHub
        # already stops from approving). Carry the source repository's rule across: the person
        # who pushed the tag or created the release may not be the only approver.
        run_id = os.environ["GITHUB_RUN_ID"]
        approvals = gh(f"/repos/{REPO}/actions/runs/{run_id}/approvals") or []
        approvers = {a["user"]["login"] for a in approvals if a.get("state") == "approved"
                     and any(e.get("name") == f"release-{name}" for e in a.get("environments", []))}
        q = urllib.parse.urlencode({"branch": tag, "per_page": 20})
        runs = (gh(f"/repos/{pkg['source']}/actions/workflows/{pkg['workflow']}/runs?{q}") or {}).get("workflow_runs", [])
        authors = {u["login"] for r in runs for u in (r.get("actor"), r.get("triggering_actor")) if u}
        if not runs:
            die(f"no {pkg['workflow']} run for {tag} in {pkg['source']}; cannot check who released it")
        if not approvers:
            die("no approval recorded for this run")
        if approvers <= authors:
            die(f"approved only by {', '.join(sorted(approvers))}, who started the {tag} build in "
                f"{pkg['source']}. Another reviewer must approve: re-run this job.")
    reg = Registry()
    existing = reg.tags(pkg["release"])
    if tag in existing:
        current = reg.manifest(pkg["release"], tag)
        if current and current[0] != digest:
            die(f"{pkg['release']}:{tag} already exists as {current[0]}; release tags are never overwritten")
    tags = release_tags(pkg, tag, existing)
    if not tags or tags[0] != tag:
        die(f"packages.json: {name} must list \"ref\" first in its tags")
    summary(f"Tags to set on `{pkg['release']}@{digest}`: " + ", ".join(f"`{t}`" for t in tags))
    output(tags=" ".join(tags), edge=pkg["edge"], release=pkg["release"])


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
    cmds = {"scan": scan, "resolve": resolve, "plan": plan, "verify": verify}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        die(f"usage: promote.py {'|'.join(cmds)}")
    cmds[sys.argv[1]]()
