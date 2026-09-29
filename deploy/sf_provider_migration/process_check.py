#!/usr/bin/env python3
"""Exercise candidate service startup and warm shutdown on isolated rehearsal data."""

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

BENCH = "/home/frappe/frappe-bench"
PYTHON = BENCH + "/env/bin/python"
SERVICES = ("backend", "websocket", "frontend", "queue-long", "queue-short", "scheduler")

EXEC_PROBE = r"""
import json,pathlib
try:
 children=pathlib.Path('/proc/1/task/1/children').read_text().split()
 args=pathlib.Path('/proc/'+children[0]+'/cmdline').read_bytes().rstrip(b'\0').decode().split('\0') if len(children)==1 else []
except FileNotFoundError:
 args=[]
ready=bool(args and args[0]) and '/usr/local/bin/entrypoint.sh' not in args
print(json.dumps({'ready':ready,'program':pathlib.Path(args[0]).name if ready else None}))
"""

CONFIG_PROBE = r"""
import json,pathlib,sys
from urllib.parse import urlparse
root=pathlib.Path('/home/frappe/frappe-bench/sites')
site,db,redis=sys.argv[1:]
conf=json.loads((root/'common_site_config.json').read_text())
conf.update(json.loads((root/site/'site_config.json').read_text()))
assert conf.get('db_host')==db, 'unexpected_isolation_database'
assert all(urlparse(conf.get(key,'')).hostname==redis for key in ('redis_cache','redis_queue','redis_socketio')), 'unexpected_isolation_redis'
assert int(conf.get('maintenance_mode') or 0)==1 and int(conf.get('pause_scheduler') or 0)==1, 'isolation_must_remain_paused'
print('ISOLATED_CONFIG_OK')
"""

HTTP_PROBE = r"""
import json,socket,sys,urllib.request,urllib.error
url,site=sys.argv[1:]
try:
 with socket.create_connection(('websocket',9000),timeout=3): pass
 request=urllib.request.Request(url,headers={'Host':site})
 with urllib.request.urlopen(request,timeout=5) as response:
  assert response.status==200 and json.load(response)=={'message':'pong'}, 'unexpected_ping_response'
  print(json.dumps({'status':200,'message':'pong'}))
except urllib.error.HTTPError as error:
 body=error.read(8192).decode('utf-8','replace')
 if error.code in (502,504):
  print(json.dumps({'pending_http':error.code})); raise SystemExit(75)
 try: payload=json.loads(body)
 except ValueError: payload={}
 assert error.code==503 and payload.get('exc_type')=='SessionStopped', 'unexpected_health_http:'+str(error.code)
 print(json.dumps({'status':503,'exc_type':'SessionStopped'}))
except (urllib.error.URLError,TimeoutError,ConnectionError):
 print(json.dumps({'pending_connection':True})); raise SystemExit(75)
"""


def run(args, *, timeout=30, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
    return result


def inspect(target, template):
    return json.loads(run(["docker", "inspect", "--format", template, target]).stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate")
    parser.add_argument("site")
    parser.add_argument("production_project")
    parser.add_argument("resources", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    resources = json.loads(args.resources.read_text())
    network = resources["network"]
    assert re.fullmatch(r"shipment-check-[0-9a-f]{10}-net", network), "invalid_isolation_network"
    prefix = network.removesuffix("-net")
    assert resources["db"] == prefix + "-db" and resources["redis"] == prefix + "-redis", "invalid_isolation_endpoints"
    assert set(resources["volumes"]) == {prefix + suffix for suffix in ("-db", "-sites", "-logs")}, "invalid_isolation_volumes"
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.production_project), "invalid_production_project"
    assert Path(args.site).name == args.site and args.site not in (".", ".."), "invalid_site"
    assert inspect(network, "{{json .Internal}}") is True, "isolation_network_not_internal"
    for endpoint in (resources["db"], resources["redis"]):
        assert set(inspect(endpoint, "{{json .NetworkSettings.Networks}}")) == {network}, "endpoint_outside_isolation"
        assert inspect(endpoint, "{{json .State.Running}}") is True, "isolation_endpoint_stopped"
    for volume in resources["volumes"]:
        assert json.loads(run(["docker", "volume", "inspect", "--format", "{{json .Name}}", volume]).stdout) == volume

    evidence = args.resources.resolve().parent / "process-check"
    evidence.mkdir(mode=0o700)
    mounts = ["--mount", f"type=volume,source={prefix}-sites,target={BENCH}/sites",
              "--mount", f"type=volume,source={prefix}-logs,target={BENCH}/logs"]
    image_id = inspect(args.candidate, "{{json .Id}}")
    names = {service: prefix + "-process-" + service for service in SERVICES}
    started, results = [], {}

    def state(service):
        return inspect(names[service], "{{json .State}}")

    def stop(service, signal="SIGTERM", grace=120):
        assert state(service)["Running"], "process_exited_before_stop:" + service
        # A client timeout never sends another signal. In particular workers
        # get Docker's unlimited warm-stop grace and are never forcibly killed.
        run(["docker", "stop", "--signal", signal, "--time", str(grace), names[service]], timeout=125)
        value = state(service)
        assert not value["Running"] and value["ExitCode"] != 137 and not value["OOMKilled"], "warm_stop_failed:" + service
        results[service]["exit_code"] = value["ExitCode"]

    try:
        config = run(["docker", "run", "--rm", "--pull", "never", "--network", "none", *mounts,
                      "--entrypoint", PYTHON, args.candidate, "-c", CONFIG_PROBE,
                      args.site, resources["db"], resources["redis"]])
        assert config.stdout.strip() == "ISOLATED_CONFIG_OK", "missing_isolation_config_evidence"
        for service in SERVICES:
            # This is the only production read: do not inspect/copy environment,
            # mounts, networks, restart policies or credentials.
            original = args.production_project + "-" + service + "-1"
            command = inspect(original, "{{json .Config.Cmd}}")
            user = inspect(original, "{{json .Config.User}}")
            workdir = inspect(original, "{{json .Config.WorkingDir}}")
            assert isinstance(command, list) and command and all(isinstance(x, str) for x in command), "invalid_service_command:" + service
            argv = ["docker", "run", "-d", "--pull", "never", "--init", "--name", names[service],
                    "--network", network, "--network-alias", service, *mounts]
            if user:
                argv += ["--user", user]
            if workdir:
                argv += ["--workdir", workdir]
            if service == "frontend":
                argv += ["-e", "BACKEND=backend:8000", "-e", "SOCKETIO=websocket:9000",
                         "-e", "FRAPPE_SITE_NAME_HEADER=" + args.site]
            started.append(service)
            run([*argv, args.candidate, *command])
            assert inspect(names[service], "{{json .Image}}") == image_id
            assert inspect(names[service], "{{json .HostConfig.Init}}") is True
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                assert state(service)["Running"], "startup_process_exited:" + service
                probe = run(["docker", "exec", names[service], PYTHON, "-c", EXEC_PROBE], timeout=10, check=False)
                if probe.returncode == 0 and json.loads(probe.stdout)["ready"]:
                    results[service] = json.loads(probe.stdout)
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("startup_exec_timeout:" + service)

        health = {}
        for service, port in (("backend", 8000), ("frontend", 8080)):
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                assert all(state(name)["Running"] for name in SERVICES), "service_exited_before_health"
                probe = run(["docker", "exec", names["backend"], PYTHON, "-c", HTTP_PROBE,
                             f"http://{service}:{port}/api/method/frappe.ping", args.site], timeout=12, check=False)
                if probe.returncode == 0:
                    health[service] = json.loads(probe.stdout)
                    break
                assert probe.returncode == 75, "unexpected_health_response:" + service
                time.sleep(1)
            else:
                raise RuntimeError("isolated_http_startup_timeout:" + service)

        run(["docker", "kill", "--signal", "CONT", names["frontend"]])
        run(["docker", "exec", names["frontend"], "nginx", "-s", "quit"])
        stop("websocket")
        assert run(["docker", "wait", names["frontend"]], timeout=120).stdout.strip() == "0", "frontend_not_graceful"
        value = state("frontend")
        assert not value["Running"] and not value["OOMKilled"] and value["ExitCode"] == 0
        results["frontend"]["exit_code"] = 0
        stop("scheduler", "SIGINT")
        stop("backend")
        for service in ("queue-long", "queue-short"):
            stop(service, grace=-1)
        report = {"image_id": image_id, "network": network, "services": results, "health": health}
        (evidence / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
        print("PROCESS_START_AND_WARM_STOP_OK")
    except BaseException as error:
        # Preserve diagnostic output privately, including failures before a
        # service could be created. Never remove or force-stop a failed run.
        detail = {"error": type(error).__name__, "message": str(error)}
        if isinstance(error, subprocess.CalledProcessError):
            detail.update(stdout=error.stdout, stderr=error.stderr)
        (evidence / "failure.json").write_text(json.dumps(detail, default=str, indent=2) + "\n")
        raise
    finally:
        for service in started:
            try:
                logs = run(["docker", "logs", "--timestamps", names[service]], timeout=15, check=False)
                (evidence / (service + ".log")).write_text(logs.stdout + logs.stderr)
            except subprocess.TimeoutExpired:
                (evidence / (service + ".log")).write_text("Docker log collection timed out; container retained.\n")


if __name__ == "__main__":
    main()
