"""Read queue metadata without removing, retrying or executing any job."""
import json
import sys
import frappe
from frappe.utils.background_jobs import get_redis_conn
from rq import Queue
from rq.job import Job
from rq.registry import StartedJobRegistry, DeferredJobRegistry, ScheduledJobRegistry

site, expected_host = sys.argv[1:]
frappe.init(site, sites_path="/home/frappe/frappe-bench/sites")
assert frappe.conf.db_host == expected_host, "unexpected_database_host"
try:
    connection = get_redis_conn()
    references, counts = [], {}
    for queue in Queue.all(connection=connection):
        sources = {"queued": queue.job_ids}
        for registry in (StartedJobRegistry, DeferredJobRegistry, ScheduledJobRegistry):
            sources[registry.__name__] = registry(name=queue.name, connection=connection).get_job_ids(cleanup=False)
        for state, ids in sources.items():
            counts[queue.name + ":" + state] = len(ids)
            for identifier in ids:
                job = Job.fetch(identifier, connection=connection)
                method = job.kwargs.get("method")
                path = method if isinstance(method, str) else getattr(method, "__module__", "") + "." + getattr(method, "__name__", "")
                if path.startswith("sf_international.") or (job.func_name or "").startswith("sf_international."):
                    references.append({"queue": queue.name, "state": state, "job_id": identifier, "method": path})
    print(json.dumps({"counts": counts, "legacy_references": references}))
    assert not references, "legacy_jobs_must_finish_before_migration"
    print("LEGACY_QUEUE_EMPTY")
finally:
    frappe.destroy()
