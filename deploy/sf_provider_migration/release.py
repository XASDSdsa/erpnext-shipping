#!/usr/bin/env python3
"""Four-repository carrier migration candidate, isolated DB rehearsal and controlled app switch."""
import gzip
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BENCH = "/home/frappe/frappe-bench"
SITES = BENCH + "/sites"
PYTHON = BENCH + "/env/bin/python"
SERVICES = ["backend", "websocket", "frontend", "queue-long", "queue-short", "scheduler"]
BACKGROUND = ["scheduler", "queue-long", "queue-short"]
TOOLS = ["release.py", "release.sh", "release.env", "metadata.py", "queue_check.py", "process_check.py", "verify_code.py", "check_health.py", "validate_backup.py", "Dockerfile", ".dockerignore", "assets-entrypoint.sh"]
os.umask(0o077)
os.chdir(ROOT)


def required(key):
    value = os.environ.get(key)
    assert value, "missing_environment:" + key
    return value


def run(arguments, *, capture=False, input=None, check=True, timeout=None):
    # Credentials are only passed in protected files, never these arguments.
    print("+ " + shlex.join([str(arg) for arg in arguments]), flush=True)
    result = subprocess.run([str(arg) for arg in arguments], input=input, stdout=subprocess.PIPE if capture else None, stderr=subprocess.STDOUT if capture else None, check=False, timeout=timeout)
    if check and result.returncode:
        if capture:
            print(result.stdout.decode(errors="replace"))
        raise RuntimeError("command_failed:" + str(arguments[0]) + ":" + str(result.returncode))
    return result.stdout.decode().strip() if capture and check else result


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def load(path):
    return json.loads(Path(path).read_text())


def sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def scripts():
    return {name: sha(ROOT / name) for name in TOOLS}


def inspection(target):
    return json.loads(run(["docker", "inspect", target], capture=True))[0]


def image_id(image):
    return run(["docker", "image", "inspect", image, "--format", "{{.Id}}"], capture=True)


def runnable_image(image):
    """Resolve a pinned local image digest to a tag accepted by Docker run.

    Docker on the deployment host can inspect the local digest but rejects the
    same digest when passed directly to ``docker run``. Resolve it through the
    local image table, then verify the resolved tag still points at that exact
    image before using it with ``--pull=never``.
    """
    if image.startswith("sha256:"):
        expected = image
    elif "@sha256:" in image:
        expected = "sha256:" + image.split("@sha256:", 1)[1]
    else:
        return image

    rows = run(
        [
            "docker",
            "image",
            "ls",
            "--digests",
            "--no-trunc",
            "--format",
            "{{.Repository}}:{{.Tag}} {{.Digest}} {{.ID}}",
        ],
        capture=True,
    ).splitlines()
    matches = []
    for row in rows:
        fields = row.split()
        if len(fields) != 3:
            continue
        tag, digest, local_id = fields
        if tag == "<none>:<none>" or expected not in (digest, local_id):
            continue
        if image_id(tag) != expected:
            raise AssertionError("local_image_id_mismatch:" + tag)
        matches.append(tag)
    if len(matches) != 1:
        raise AssertionError("local_image_digest_not_unique:" + expected)
    return matches[0]


def state():
    saved = load("release-state.json")
    assert saved["scripts"] == scripts(), "deployment_scripts_changed"
    assert saved["base_id"] == image_id(required("BASE_IMAGE")), "base_image_changed"
    assert saved["candidate_id"] == image_id(required("NEW_IMAGE")), "candidate_image_changed"
    assert saved["revisions"] == {"erpnext": required("ERP_REV"), "sf_international": required("SF_REV"), "erpnext_shipping": required("SHIPPING_REV"), "flow": required("FLOW_REV")}, "requested_revisions_changed"
    return saved


def source_refs():
    values = []
    for label, checkout, app, base in (
        ("ERP", "erpnext", "erpnext", "BASE_ERPNEXT_REV"),
        ("SF", "sf", "sf_international", "BASE_SF_REV"),
        ("SHIPPING", "shipping", "erpnext_shipping", "BASE_SHIPPING_REV"),
        ("FLOW", "flow", "flow", "BASE_FLOW_REV"),
    ):
        revision = required(label + "_REV")
        assert re.fullmatch(r"[0-9a-f]{40}", revision), "invalid_revision:" + label
        remote, branch = required(label + "_REMOTE"), required(label + "_BRANCH")
        assert remote.startswith("git@github.com:XASDSdsa/"), "wrong_ssh_remote"
        assert run(["git", "ls-remote", remote, "refs/heads/" + branch], capture=True).split()[0] == revision, "remote_head_changed:" + label
        values.append((checkout, app, remote, revision, required(base)))
    return values


def fetch_source(repo, baseline, revision):
    result = run(["git", "-C", repo, "fetch", "--depth=1", "origin", baseline, revision], check=False)
    if result.returncode == 0:
        return
    print("SHALLOW_FETCH_FALLBACK " + revision)
    run(["git", "-C", repo, "fetch", "--depth=2", "origin", revision])
    present = run(["git", "-C", repo, "cat-file", "-e", baseline + "^{commit}"], check=False)
    if present.returncode:
        run(["git", "-C", repo, "fetch", "--depth=1", "origin", baseline])
    assert run(["git", "-C", repo, "cat-file", "-e", baseline + "^{commit}"], check=False).returncode == 0, "missing_baseline_commit"


def verify(target, manifest, image=False, **options):
    command = ["python3", "verify_code.py", "image" if image else "container", target, manifest]
    for key, value in options.items():
        command.extend(["--" + key.replace("_", "-"), value])
    run(command)
    if manifest == "candidate-sources.json":
        verify_startup(target, image=image)


def verify_startup(target, *, image=False):
    assert inspection(target)["Config"]["Entrypoint"] == ["/usr/local/bin/entrypoint.sh"], "unexpected_asset_entrypoint"
    command = ["docker", "run", "--rm", "--network", "none", "--entrypoint", PYTHON, target] if image else ["docker", "exec", target, PYTHON]
    code = "import hashlib,pathlib; p=pathlib.Path('/usr/local/bin/entrypoint.sh'); assert p.stat().st_mode & 0o777 == 0o755; print(hashlib.sha256(p.read_bytes()).hexdigest())"
    assert run(command + ["-c", code], capture=True) == sha(ROOT / "assets-entrypoint.sh"), "asset_entrypoint_source_mismatch"
    print("STARTUP_SOURCE_OK " + target)


def running(expected_image, expected_id, services=SERVICES, *, init=None):
    for service in services:
        actual = inspection(required("PROJECT") + "-" + service + "-1")
        assert actual["State"]["Running"] and actual["Config"]["Image"] == expected_image and actual["Image"] == expected_id, "service_version:" + service
        if init is not None:
            expected_init = init if isinstance(init, bool) else init[service]
            assert bool(actual["HostConfig"].get("Init")) == expected_init, "service_init:" + service


def prepare():
    assert not Path("release-state.json").exists() and not Path("git-source").exists(), "use_new_release_directory"
    refs = source_refs()
    base, candidate = required("BASE_IMAGE"), required("NEW_IMAGE")
    assert base != candidate
    base_id = image_id(base)
    running(base, base_id)
    assert run(["docker", "image", "inspect", candidate], capture=True, check=False).returncode != 0, "candidate_tag_already_exists"
    # Resolve substitutions and preserve unrelated override settings before
    # spending time on a build. deploy repeats this check against live config.
    configuration = json.loads(run(compose() + ["config", "--format", "json"], capture=True))
    prepare_override(Path(required("PROJECT_PATH")) / "build/zh-cn/compose.zh-cn.yaml", base, candidate, configuration)
    Path("git-source").mkdir()
    changed = {}
    for checkout, app, remote, revision, baseline in refs:
        repo = Path("git-source") / checkout
        run(["git", "init", repo])
        run(["git", "-C", repo, "remote", "add", "origin", remote])
        fetch_source(repo, baseline, revision)
        run(["git", "-C", repo, "checkout", "--detach", revision])
        paths = run(["git", "-C", repo, "diff", "--name-only", baseline, revision], capture=True).splitlines()
        assert not any(Path(path).name in {"pyproject.toml", "package.json", "yarn.lock", "requirements.txt"} for path in paths), "dependency_change_requires_new_build_plan"
        changed[app] = paths
        archive = ROOT / (checkout + ".tar")
        run(["git", "-C", repo, "archive", "--output", archive, revision])
        destination = Path("app-source") / app
        destination.mkdir(parents=True)
        with tarfile.open(archive) as stream:
            for member in stream.getmembers():
                assert not Path(member.name).is_absolute() and ".." not in Path(member.name).parts
            stream.extractall(destination, filter="data")
    for name in TOOLS:
        assert (Path("git-source/shipping/deploy/sf_provider_migration") / name).read_bytes() == (ROOT / name).read_bytes(), "script_not_from_target_commit:" + name
    save("changed-paths.json", changed)
    run(["python3", "verify_code.py", "manifests", "--erpnext", required("ERP_REV"), "--sf", required("SF_REV"), "--shipping", required("SHIPPING_REV"), "--base-erpnext", required("BASE_ERPNEXT_REV"), "--base-sf", required("BASE_SF_REV"), "--base-shipping", required("BASE_SHIPPING_REV"), "--flow", required("FLOW_REV"), "--base-flow", required("BASE_FLOW_REV")])
    verify(base, "baseline-sources.json", image=True, assets_out="baseline-assets.json")
    for service in ("backend", "frontend"):
        verify(required("PROJECT") + "-" + service + "-1", "baseline-sources.json", assets_match="baseline-assets.json")
    build = ["docker", "build", "--pull=false", "-t", candidate]
    for name in ("BASE_IMAGE", "ERP_REV", "SF_REV", "SHIPPING_REV", "FLOW_REV", "RELEASE_NAME"):
        build.extend(["--build-arg", name + "=" + required(name)])
    run(build + ["."])
    verify(candidate, "candidate-sources.json", image=True, baseline_assets="baseline-assets.json", assets_out="candidate-assets.json")
    run(["docker", "run", "--rm", "--network", "none", "--entrypoint", PYTHON, candidate, "-c", "import frappe; frappe.init('', sites_path='/home/frappe/frappe-bench/sites'); from frappe.gettext.translate import get_translations_from_mo; t=get_translations_from_mo('zh','erpnext'); assert t.get('Pickup and Delivery Details'); print('ERPNEXT_GETTEXT_OK')"])
    assert image_id(base) == base_id
    save("release-state.json", {"scripts": scripts(), "base_id": base_id, "candidate_id": image_id(candidate), "revisions": {app: rev for _, app, _, rev, _ in refs}})
    print("CANDIDATE_IMAGE_READY; run rehearse before deploy")


def capture_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    uid = int(run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "id", required("BASE_IMAGE"), "-u", "frappe"], capture=True))
    gid = int(run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "id", required("BASE_IMAGE"), "-g", "frappe"], capture=True))
    os.chown(path, uid, gid)
    return path.resolve()


def metadata(image, mode, snapshot, network, mounts, db_host, *, check=True):
    if mode == "fail-after-owner":
        assert db_host.startswith("shipment-check-") and network.startswith("shipment-check-"), "failure_injection_requires_isolation"
        assert inspection(network)["Internal"], "failure_injection_requires_internal_network"
        assert all(str(source).startswith("shipment-check-") for source, _ in mounts), "failure_injection_requires_isolated_volumes"
    snapshot = Path(snapshot).resolve()
    command = ["docker", "run", "--rm", "--network", network, "--user", "frappe", "--workdir", SITES]
    for source, destination in mounts:
        command += ["--mount", "type=" + ("bind" if str(source).startswith("/") else "volume") + ",source=" + str(source) + ",target=" + destination]
    command += ["--mount", "type=bind,source=" + str(ROOT) + ",target=/run/release,readonly", "--mount", "type=bind,source=" + str(snapshot.parent) + ",target=/capture", "--entrypoint", PYTHON, image, "/run/release/metadata.py", mode, "--site", required("SITE"), "--snapshot", "/capture/" + snapshot.name, "--db-host", db_host]
    output = run(command, capture=True, check=check)
    if check:
        print(output)
        return output
    print(output.stdout.decode(errors="replace"))
    return output


def migration_result(output):
    for line in reversed(output.splitlines()):
        if line.startswith('{"result": "MIGRATION_OK"'):
            return json.loads(line)
    raise AssertionError("missing_migration_result")


def rehearse():
    saved = state()
    assert not Path("rehearsal.ok.json").exists() and not Path("isolation").exists(), "use_new_release_for_rehearsal"
    backup = Path(required("BACKUP_DIR")).resolve()
    run(["python3", "validate_backup.py", backup])
    db_image, redis_image = required("DB_IMAGE"), required("REDIS_IMAGE")
    assert all("@sha256:" in image or image.startswith("sha256:") for image in (db_image, redis_image)), "pin_db_and_redis_image_digest"
    prefix = "shipment-check-" + secrets.token_hex(5)
    net, db, redis = prefix + "-net", prefix + "-db", prefix + "-redis"
    iso = ROOT / "isolation"
    iso.mkdir(mode=0o700)
    password = secrets.token_hex(24)
    (iso / "db.env").write_text("MARIADB_ROOT_PASSWORD=" + password + "\nMARIADB_DATABASE=shipment_check\nMARIADB_USER=shipment_check\nMARIADB_PASSWORD=" + password + "\n")
    (iso / "db.cnf").write_text("[client]\nuser=root\npassword=" + password + "\n")
    run(["docker", "network", "create", "--internal", net])
    for volume in (prefix + "-db", prefix + "-sites", prefix + "-logs"):
        run(["docker", "volume", "create", volume])
    save(iso / "resources.json", {"network": net, "db": db, "redis": redis, "volumes": [prefix + suffix for suffix in ("-db", "-sites", "-logs")]})
    run(["docker", "run", "--pull=never", "-d", "--name", db, "--network", net, "--env-file", iso / "db.env", "-v", prefix + "-db:/var/lib/mysql", "--mount", "type=bind,source=" + str(iso / "db.cnf") + ",target=/run/secrets/db.cnf,readonly", runnable_image(db_image)])
    run(["docker", "run", "--pull=never", "-d", "--name", redis, "--network", net, runnable_image(redis_image)])
    # MariaDB's initialization server accepts socket queries but runs with
    # --skip-networking. TCP proves the final server is ready for the import.
    client = ["docker", "exec", "-i", db, "mariadb", "--defaults-extra-file=/run/secrets/db.cnf", "--protocol=TCP", "--host=127.0.0.1"]
    for attempt in range(60):
        ready = run(client + ["-N", "-e", "SELECT 1"], capture=True, check=False)
        if ready.returncode == 0 and ready.stdout.strip() == b"1":
            break
        time.sleep(1)
    else:
        raise RuntimeError("isolated_database_not_ready")
    sql = next(backup.glob("*database.sql.gz"))
    process = subprocess.Popen(client + ["shipment_check"], stdin=subprocess.PIPE)
    with gzip.open(sql, "rb") as stream:
        shutil.copyfileobj(stream, process.stdin)
    process.stdin.close()
    assert process.wait() == 0, "isolated_database_import_failed"
    site = required("SITE")
    conf = load(next(backup.glob("*site_config_backup.json")))
    isolated_connections = {"db_host": db, "redis_cache": "redis://" + redis + ":6379/0", "redis_queue": "redis://" + redis + ":6379/1", "redis_socketio": "redis://" + redis + ":6379/2"}
    conf.update(**isolated_connections, db_name="shipment_check", db_user="shipment_check", db_password=password, maintenance_mode=1, pause_scheduler=1)
    config_dir = iso / "sites"
    (config_dir / site / "logs").mkdir(parents=True)
    save(config_dir / site / "site_config.json", conf)
    save(config_dir / "common_site_config.json", {**isolated_connections, "pause_scheduler": 1})
    (config_dir / "apps.txt").write_text(run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "cat", required("BASE_IMAGE"), BENCH + "/sites/apps.txt"], capture=True) + "\n")
    run(["docker", "run", "--rm", "--network", "none", "--user", "root", "--mount", "type=bind,source=" + str(config_dir) + ",target=/run/sites,readonly", "-v", prefix + "-sites:" + SITES, "-v", prefix + "-logs:" + BENCH + "/logs", "--entrypoint", "/bin/bash", required("BASE_IMAGE"), "-c", "cp -a /run/sites/. /home/frappe/frappe-bench/sites/ && chown -R frappe:frappe /home/frappe/frappe-bench/sites /home/frappe/frappe-bench/logs"])
    mounts = [(prefix + "-sites", BENCH + "/sites"), (prefix + "-logs", BENCH + "/logs")]
    snapshot = capture_directory(iso / "captures") / "before.json"
    metadata(required("BASE_IMAGE"), "snapshot", snapshot, net, mounts, db)
    first = migration_result(metadata(required("NEW_IMAGE"), "migrate", snapshot, net, mounts, db))
    second = migration_result(metadata(required("NEW_IMAGE"), "migrate", snapshot, net, mounts, db))
    assert first["after"] == second["after"], "migration_not_idempotent"
    metadata(required("BASE_IMAGE"), "restore", snapshot, net, mounts, db)
    metadata(required("BASE_IMAGE"), "compare", snapshot, net, mounts, db)
    if first["actions"].get("already_migrated"):
        # Flow-only releases have no SF ownership mutation to inject after.
        # The migration and exact rollback checks above remain mandatory.
        print("FAILURE_INJECTION_NOT_APPLICABLE_NO_OWNERSHIP_CHANGE")
    else:
        failed = metadata(required("NEW_IMAGE"), "fail-after-owner", snapshot, net, mounts, db, check=False)
        assert failed.returncode and b"ISOLATED_INJECTED_FAILURE_AFTER_OWNER" in failed.stdout, "failure_injection_not_reached"
    metadata(required("BASE_IMAGE"), "restore", snapshot, net, mounts, db)
    metadata(required("NEW_IMAGE"), "migrate", snapshot, net, mounts, db)
    run(["python3", "process_check.py", required("NEW_IMAGE"), site, required("PROJECT"), iso / "resources.json"])
    save("rehearsal.ok.json", {**saved, "snapshot_sha256": sha(snapshot), "backup_sha256": sha(sql), "db_image": image_id(db_image), "redis_image": image_id(redis_image), "resources": load(iso / "resources.json"), "migration_actions": first["actions"]})
    run(["docker", "stop", db, redis])
    print("ISOLATED_DATABASE_UPGRADE_IDEMPOTENCY_AND_ROLLBACK_OK; resources retained")


def compose(override=None):
    project = Path(required("PROJECT_PATH"))
    return ["docker", "compose", "--env-file", project / "production.env", "-f", project / "compose.production.yaml", "-f", override or project / "build/zh-cn/compose.zh-cn.yaml", "-p", required("PROJECT")]


def bench(*arguments):
    run(["docker", "exec", "--user", "frappe", "--workdir", BENCH, required("PROJECT") + "-backend-1", "bench", "--site", required("SITE"), *arguments])


def one_shot(image, network, mounts, entrypoint, arguments, *, check=True, extra_mounts=(), readonly=False, user="frappe", workdir=BENCH):
    command = ["docker", "run", "--rm", "--network", network, "--user", user, "--workdir", workdir]
    for source, destination in mounts:
        command += ["--mount", "type=" + ("bind" if str(source).startswith("/") else "volume") + ",source=" + str(source) + ",target=" + destination + (",readonly" if readonly else "")]
    for mount in extra_mounts:
        command += ["--mount", mount]
    return run(command + ["--entrypoint", entrypoint, image, *arguments], capture=True, check=check)


def stopped(services):
    for service in services:
        actual = inspection(required("PROJECT") + "-" + service + "-1")
        assert not actual["State"]["Running"], "service_still_running:" + service
        assert actual["State"]["ExitCode"] != 137 and not actual["State"].get("OOMKilled"), "service_did_not_stop_gracefully:" + service


def container_name(service):
    return required("PROJECT") + "-" + service + "-1"


def start_services(services, *, force_recreate=False):
    # Even rollback retains the old immutable image. Serialize its shared-sites
    # asset wrapper, and wait for exec before starting another service.
    probe = """import json,pathlib,sys
try:
 pids=pathlib.Path('/proc/1/task/1/children').read_text().split() if sys.argv[1]=='init' else ['1']
 args=pathlib.Path('/proc/'+pids[0]+'/cmdline').read_bytes().rstrip(b'\\0').decode().split('\\0') if len(pids)==1 else []
except FileNotFoundError:
 args=[]
ready=bool(args and args[0]) and '/usr/local/bin/entrypoint.sh' not in args
print(json.dumps({'ready':ready,'program':pathlib.Path(args[0]).name if ready else None}))
"""
    for service in services:
        command = compose() + ["up", "-d", "--no-deps", "--pull", "never"]
        if force_recreate:
            command.append("--force-recreate")
        run(command + [service])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            actual = inspection(container_name(service))
            if actual["State"]["Running"] and not actual["State"].get("Restarting"):
                result = run(["docker", "exec", container_name(service), PYTHON, "-c", probe, "init" if actual["HostConfig"].get("Init") else "direct"], capture=True, check=False)
                if result.returncode == 0:
                    readiness = json.loads(result.stdout)
                    if readiness["ready"]:
                        print("STARTUP_EXEC_OK " + service + " " + readiness["program"])
                        break
            time.sleep(0.2)
        else:
            raise AssertionError("startup_entrypoint_timeout:" + service)


def stop_service(service, signal="SIGTERM", grace=120):
    if inspection(container_name(service))["State"]["Running"]:
        run(["docker", "stop", "--signal", signal, "--time", str(grace), container_name(service)])
    stopped([service])


def stop_app_services(*, pause_queues=None):
    frontend = inspection(container_name("frontend"))
    if frontend["State"]["Running"]:
        run(["docker", "kill", "--signal", "CONT", container_name("frontend")])
        run(["docker", "exec", container_name("frontend"), "nginx", "-s", "quit"])
    stop_service("websocket")
    if frontend["State"]["Running"]:
        assert run(["docker", "wait", container_name("frontend")], capture=True, timeout=120) == "0", "frontend_shutdown_exit"
    stopped(["frontend"])
    actual = inspection(container_name("frontend"))
    assert actual["Id"] == frontend["Id"] and actual["State"]["ExitCode"] == 0 and actual["RestartCount"] == frontend["RestartCount"], "frontend_shutdown_not_exact"
    stop_service("scheduler", signal="SIGINT")
    if pause_queues:
        pause_queues()
    stop_service("backend")
    print("Waiting for existing jobs; no forced worker termination", flush=True)
    for service in ("queue-long", "queue-short"):
        stop_service(service, grace=-1)
    stopped(SERVICES)


def legacy_queue_check(image, network, mounts, db_host):
    output = one_shot(image, network, mounts, PYTHON,
        ["/run/release/queue_check.py", required("SITE"), db_host],
        extra_mounts=["type=bind,source=" + str(ROOT) + ",target=/run/release,readonly"], workdir=SITES)
    print(output)
    assert "LEGACY_QUEUE_EMPTY" in output, "legacy_queue_check_missing_evidence"


def copy_backup(image, mounts, inside_backup, backup_dir):
    # Read a stopped service's verified sites volume in a disposable container;
    # never depend on docker exec/cp or the stopped container's writable layer.
    script = "import pathlib,shutil,sys; p=pathlib.Path(sys.argv[1]); assert p.is_dir() and any(p.iterdir()), 'backup_directory_empty'; shutil.copytree(p, '/capture', dirs_exist_ok=True); print('BACKUP_COPIED_FROM_SITES_VOLUME')"
    print(one_shot(image, "none", mounts, PYTHON, ["-c", script, inside_backup], readonly=True, user="root", extra_mounts=["type=bind,source=" + str(backup_dir) + ",target=/capture"]))


def queue_control(image, mode, network, mounts, db_host):
    # The owner marker makes recovery safe even if the CLI disconnects after
    # suspend() succeeds. Never resume a queue paused by another operator.
    assert mode in {"check", "pause", "resume"}
    script = r'''
import sys
import frappe
from frappe.utils.background_jobs import get_redis_conn
from rq.suspension import is_suspended, resume, suspend
site, host, mode, owner = sys.argv[1:]
frappe.init(site, sites_path='/home/frappe/frappe-bench/sites')
assert frappe.conf.db_host == host, 'unexpected_database_host'
connection = get_redis_conn()
key = 'sf_provider_migration:queue_pause_owner'
owner = owner.encode()
if mode in ('check', 'pause'):
    assert not is_suspended(connection) and not connection.get(key), 'queue_already_suspended_or_owned'
    if mode == 'pause':
        assert connection.set(key, owner, nx=True), 'queue_pause_owner_conflict'
        suspend(connection)
        assert is_suspended(connection), 'queue_suspend_failed'
else:
    existing = connection.get(key)
    if existing == owner:
        resume(connection)
        assert not is_suspended(connection), 'queue_resume_failed'
        connection.delete(key)
    else:
        assert existing is None and not is_suspended(connection), 'queue_pause_not_owned_by_release'
print('QUEUE_' + mode.upper() + '_OK')
frappe.destroy()
'''
    print(one_shot(image, network, mounts, PYTHON, ["-c", script, required("SITE"), db_host, mode, required("RELEASE_NAME")], workdir=SITES))


def check_logs(started, services, filename):
    logs = run(compose() + ["logs", "--no-color", "--since", started, "--tail", "200", *services], capture=True)
    Path(filename).write_text(logs)
    assert not re.search(r"Traceback \(most recent call last\)|(^|\s)(ERROR|CRITICAL)([\s:]|$)", logs, re.MULTILINE), "post_deployment_log_error"


def prepare_override(path, old, new, configuration):
    # Parse using the pinned Frappe image's declared PyYAML dependency. YAML and
    # resolved configuration contents stay captured; never print site secrets.
    # Images may be Compose expressions, not literal tags. Preserve every
    # unrelated setting and compare the resolved configuration instead.
    parser = "import json,sys,yaml\ntry:\n print(json.dumps(yaml.safe_load(sys.stdin.read()) or {}))\nexcept Exception:\n raise SystemExit('override_yaml_parse_failed')"
    parsed = json.loads(run(["docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", PYTHON, old, "-c", parser], capture=True, input=path.read_bytes()))
    assert isinstance(parsed, dict), "override_yaml_shape_changed"
    content = parsed
    services = content.setdefault("services", {})
    assert isinstance(services, dict), "override_services_shape_changed"
    for service in SERVICES:
        assert configuration["services"][service]["image"] == old, "resolved_baseline_image_changed:" + service
        services.setdefault(service, {}).update(image=new, init=True)
    target = ROOT / "compose.override.candidate.json"
    save(target, content)
    planned = json.loads(run(compose(target) + ["config", "--format", "json"], capture=True))
    expected = json.loads(json.dumps(configuration))
    for service in SERVICES:
        expected["services"][service].update(image=new, init=True)
    assert planned == expected, "candidate_compose_changes_outside_images_and_init"
    return target


def deploy():
    saved = state()
    rehearsal = load("rehearsal.ok.json")
    assert all(rehearsal[key] == saved[key] for key in saved), "rehearsal_not_current_candidate"
    assert not Path("deploy.started.json").exists(), "use_new_release_after_attempt"
    source_refs()
    base, candidate = required("BASE_IMAGE"), required("NEW_IMAGE")
    project_path = Path(required("PROJECT_PATH"))
    release_lock = (project_path / "build/application-release.lock").open("w")
    fcntl.flock(release_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    override = project_path / "build/zh-cn/compose.zh-cn.yaml"
    backend = required("PROJECT") + "-backend-1"
    network = required("PRODUCTION_NETWORK")
    running(base, saved["base_id"])
    assert shutil.disk_usage(project_path).free > 10 * 1024 ** 3, "insufficient_disk"
    assert network in inspection(backend)["NetworkSettings"]["Networks"], "unexpected_production_network"
    configuration = json.loads(run(compose() + ["config", "--format", "json"], capture=True))
    assert all(configuration["services"][name]["image"] == base for name in SERVICES)
    original_init = {name: bool(configuration["services"][name].get("init")) for name in SERVICES}
    running(base, saved["base_id"], init=original_init)
    candidate_override = prepare_override(override, base, candidate, configuration)
    run(["python3", "check_health.py", backend, required("SITE")])
    for service in ("backend", "frontend"):
        verify(required("PROJECT") + "-" + service + "-1", "baseline-sources.json", assets_match="baseline-assets.json")
    verify(base, "baseline-sources.json", image=True, assets_match="baseline-assets.json")
    verify(candidate, "candidate-sources.json", image=True, assets_match="candidate-assets.json")
    mounts = []
    for destination in (BENCH + "/sites", BENCH + "/logs"):
        mount = next(row for row in inspection(backend)["Mounts"] if row["Destination"] == destination)
        mounts.append((mount.get("Name") or mount["Source"], destination))
    db_host = run(["docker", "exec", backend, PYTHON, "-c", "import json,pathlib; r=pathlib.Path('/home/frappe/frappe-bench/sites'); c=json.loads((r/'common_site_config.json').read_text()); c.update(json.loads((r/" + repr(required("SITE")) + "/'site_config.json').read_text())); assert not c.get('maintenance_mode'); print(c['db_host'])"], capture=True)
    before = {str(path): sha(path) for path in (project_path / "compose.production.yaml", project_path / "production.env", override)}
    shutil.copy2(override, "compose.override.before.yaml")
    backup_dir = project_path / "build/production-backups" / required("RELEASE_NAME")
    assert not backup_dir.exists()
    backup_dir.mkdir(mode=0o700)
    inside_backup = BENCH + "/sites/" + required("SITE") + "/private/deployment-backups/" + required("RELEASE_NAME")
    capture = capture_directory(ROOT / "production-capture")
    snapshot = capture / "before.json"
    queue_control(base, "check", network, mounts, db_host)
    legacy_queue_check(base, network, mounts, db_host)
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save("deploy.started.json", {**saved, "started": started, "config_before": before, "network": network, "mounts": mounts, "db_host": db_host})
    mutation_started = False
    traffic_open = False
    queue_paused = False
    phase = "quiesce"

    def maintenance(image, enabled, *, check=True):
        output = one_shot(image, network, mounts, "bench", ["--site", required("SITE"), "set-maintenance-mode", "on" if enabled else "off"], check=check)
        if check:
            print(output)
        return output

    def pause_queues():
        nonlocal queue_paused
        queue_paused = True
        queue_control(base, "pause", network, mounts, db_host)

    try:
        bench("set-maintenance-mode", "on")
        stop_app_services(pause_queues=pause_queues)
        legacy_queue_check(base, network, mounts, db_host)
        phase = "backup"
        print(one_shot(base, network, mounts, "bench", ["--site", required("SITE"), "backup", "--with-files", "--compress", "--backup-path", inside_backup]))
        copy_backup(base, mounts, inside_backup, backup_dir)
        run(["python3", "validate_backup.py", backup_dir])
        metadata(base, "snapshot", snapshot, network, mounts, db_host)
        phase = "migrate"
        mutation_started = True
        metadata(candidate, "migrate", snapshot, network, mounts, db_host)
        for path, expected in before.items():
            assert sha(path) == expected, "production_configuration_drift"
        shutil.copyfile(candidate_override, override)
        run(compose() + ["config", "--quiet"])
        start_services(["backend", "websocket", "frontend"], force_recreate=True)
        bench("clear-cache")
        bench("clear-website-cache")
        for service in ("backend", "frontend"):
            verify(required("PROJECT") + "-" + service + "-1", "candidate-sources.json", assets_match="candidate-assets.json")
        metadata(candidate, "validate", snapshot, network, mounts, db_host)
        running(candidate, saved["candidate_id"], ["backend", "frontend", "websocket"], init=True)
        stopped(BACKGROUND)
        check_logs(started, ["backend", "frontend", "websocket"], "services.before-traffic.log")
        # Set this boundary before the command: even an interrupted CLI may
        # have made maintenance-off visible to requests. No snapshot rollback
        # is safe after this point because business records can legitimately change.
        phase = "public_health"
        traffic_open = True
        maintenance(candidate, False)
        run(["python3", "check_health.py", backend, required("SITE"), "--maintenance-cleared"])
        phase = "background_start"
        start_services(BACKGROUND, force_recreate=True)
        running(candidate, saved["candidate_id"], init=True)
        check_logs(started, SERVICES, "services.after.log")
        queue_control(candidate, "resume", network, mounts, db_host)
        queue_paused = False
        save("deploy.ok.json", {**saved, "snapshot_sha256": sha(snapshot), "backup": str(backup_dir)})
        print("DEPLOY_OK")
    except BaseException:
        if traffic_open:
            # Keep the already verified candidate and its current business
            # state. Health failure requires diagnosis, not an old metadata
            # restore or stopping/recreating live business workers.
            maintained = maintenance(candidate, True, check=False).returncode == 0
            save("deployment.incomplete.json", {**saved, "phase": phase, "maintenance_requested": maintained, "queue_paused": queue_paused, "candidate_retained": True})
            print("DEPLOYMENT_INCOMPLETE_AFTER_TRAFFIC; candidate retained; diagnose health/log evidence before resuming any paused queue")
            raise
        if not mutation_started:
            # No metadata migration has begun. Preserve any active job if the
            # shutdown was interrupted; up without force-recreate leaves it alive.
            start_services(SERVICES)
            if queue_paused:
                queue_control(base, "resume", network, mounts, db_host)
                queue_paused = False
            maintenance(base, False)
            run(["python3", "check_health.py", backend, required("SITE"), "--maintenance-cleared"])
            print("ABORTED_BEFORE_MIGRATION; baseline services restored")
            raise
        try:
            # Never continue workers while source and metadata are from different releases.
            maintenance(base, True)
            stop_app_services()
            if snapshot.exists():
                metadata(base, "restore", snapshot, network, mounts, db_host)
            shutil.copy2("compose.override.before.yaml", override)
            run(compose() + ["config", "--quiet"])
            start_services(["backend", "websocket", "frontend"], force_recreate=True)
            bench("clear-cache")
            bench("clear-website-cache")
            for service in ("backend", "frontend"):
                verify(required("PROJECT") + "-" + service + "-1", "baseline-sources.json", assets_match="baseline-assets.json")
            running(base, saved["base_id"], ["backend", "frontend", "websocket"], init=original_init)
            check_logs(started, ["backend", "frontend", "websocket"], "services.rollback.log")
            maintenance(base, False)
            run(["python3", "check_health.py", backend, required("SITE"), "--maintenance-cleared"])
            start_services(BACKGROUND, force_recreate=True)
            running(base, saved["base_id"], init=original_init)
            if queue_paused:
                queue_control(base, "resume", network, mounts, db_host)
                queue_paused = False
            save("rollback.ok.json", {**saved, "snapshot_sha256": sha(snapshot) if snapshot.exists() else None})
            print("ROLLBACK_OK")
        except BaseException:
            maintenance(base, True, check=False)
            save("rollback.incomplete.json", {"queue_paused": queue_paused, "metadata_snapshot": str(snapshot), "instruction": "Keep maintenance and diagnose; no business-data restore attempted."})
            raise
        raise


if __name__ == "__main__":
    assert len(sys.argv) == 2 and sys.argv[1] in {"prepare", "rehearse", "deploy"}, "usage: release.sh prepare|rehearse|deploy"
    {"prepare": prepare, "rehearse": rehearse, "deploy": deploy}[sys.argv[1]]()
