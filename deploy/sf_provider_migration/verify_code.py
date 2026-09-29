#!/usr/bin/env python3
"""Host-side source/asset verification; no application or database mutations."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

BENCH_PYTHON = "/home/frappe/frappe-bench/env/bin/python"
REMOTE_CHECK = r'''
import hashlib,json,pathlib,sys
request=json.load(sys.stdin)
root=pathlib.Path('/home/frappe/frappe-bench')
checked={}
for app,data in request['sources'].items():
    app_root=root/'apps'/app
    for name,expected in data['files'].items():
        path=app_root/name
        assert path.is_file() or path.is_symlink(), ('missing',app,name)
        value=str(path.readlink()).encode() if path.is_symlink() else path.read_bytes()
        assert hashlib.sha256(value).hexdigest()==expected, ('source_drift',app,name)
    for name in data['deleted']:
        path=app_root/name
        assert not path.exists() and not path.is_symlink(), ('deleted_source_still_exists',app,name)
    checked[app]=len(data['files'])
assets={}
for filename in ('assets.json','assets-rtl.json'):
    manifest_path=root/'assets'/filename
    assert manifest_path.is_file(), ('missing_manifest',filename)
    for name,url in json.loads(manifest_path.read_text()).items():
        assert url.startswith('/assets/'), ('unexpected_asset_url',name,url)
        path=root/'assets'/url.removeprefix('/assets/')
        assert path.is_file(), ('missing_asset',name,url)
        assets[filename+':'+name]={'url':url,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
print(json.dumps({'checked_sources':checked,'assets':assets},sort_keys=True))
'''


def run(args, **kwargs):
    return subprocess.check_output(args, **kwargs)


def archive_manifest(repo, sha):
    data = run(["git", "-C", str(repo), "archive", sha])
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            assert not member.name.startswith("/") and ".." not in Path(member.name).parts
            if member.isfile():
                value = archive.extractfile(member).read()
            elif member.issym():
                value = member.linkname.encode()
            elif member.isdir():
                continue
            else:
                raise AssertionError(("unsupported_git_entry", member.name))
            files[member.name] = hashlib.sha256(value).hexdigest()
    assert files, ("empty_source", str(repo), sha)
    return {"sha": sha, "files": files, "deleted": []}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    manifest = sub.add_parser("manifests")
    for option in ("erpnext", "sf", "shipping", "base-erpnext", "base-sf", "base-shipping", "flow", "base-flow"):
        manifest.add_argument("--" + option, required=True)
    for mode in ("image", "container"):
        check = sub.add_parser(mode)
        check.add_argument("target")
        check.add_argument("manifest", type=Path)
        check.add_argument("--assets-out", type=Path)
        check.add_argument("--assets-match", type=Path)
        check.add_argument("--baseline-assets", type=Path)
    args = parser.parse_args()
    if args.mode == "manifests":
        baseline, candidate = {}, {}
        for app, checkout, target, base in (
            ("erpnext", "erpnext", args.erpnext, args.base_erpnext),
            ("sf_international", "sf", args.sf, args.base_sf),
            ("erpnext_shipping", "shipping", args.shipping, args.base_shipping),
            ("flow", "flow", args.flow, args.base_flow),
        ):
            repo = Path("git-source") / checkout
            baseline[app] = archive_manifest(repo, base)
            candidate[app] = archive_manifest(repo, target)
            candidate[app]["deleted"] = sorted(set(baseline[app]["files"]) - set(candidate[app]["files"]))
        Path("baseline-sources.json").write_text(json.dumps(baseline, sort_keys=True))
        Path("candidate-sources.json").write_text(json.dumps(candidate, sort_keys=True))
        print("SOURCE_MANIFESTS_OK")
        return
    sources = json.loads(args.manifest.read_text())
    inspection = json.loads(run(["docker", "inspect", args.target]))[0]
    labels = inspection["Config"].get("Labels") or {}
    for app, label in (("erpnext", "org.leya.erpnext-revision"), ("sf_international", "org.leya.sf-revision"), ("erpnext_shipping", "org.leya.shipping-revision"), ("flow", "org.leya.flow-revision")):
        assert labels.get(label) == sources[app]["sha"], ("revision_label_mismatch", app)
    command = (
        ["docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", BENCH_PYTHON, args.target]
        if args.mode == "image"
        else ["docker", "exec", "-i", args.target, BENCH_PYTHON]
    )
    request = {"sources": sources}
    result = json.loads(run(command + ["-c", REMOTE_CHECK], input=json.dumps(request).encode()))
    assets = result["assets"]
    if args.assets_match:
        assert assets == json.loads(args.assets_match.read_text()), "asset_manifest_or_hash_changed"
    if args.baseline_assets:
        baseline = json.loads(args.baseline_assets.read_text())
        # ERPNext, Shipping and Flow are built explicitly; unrelated assets must
        # retain their full manifest entries, URLs and content hashes.
        baseline_other = {name: value for name, value in baseline.items() if not value["url"].startswith(("/assets/erpnext/", "/assets/erpnext_shipping/", "/assets/flow/", "/assets/sf_international/"))}
        current_other = {name: value for name, value in assets.items() if not value["url"].startswith(("/assets/erpnext/", "/assets/erpnext_shipping/", "/assets/flow/", "/assets/sf_international/"))}
        assert current_other == baseline_other, "unrelated_asset_changed"
    if args.assets_out:
        args.assets_out.write_text(json.dumps(assets, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"result": "SOURCE_ASSETS_OK", "target": args.target, "checked_sources": result["checked_sources"], "asset_count": len(assets)}))


if __name__ == "__main__":
    main()
