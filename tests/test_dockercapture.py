"""Capturing a Docker project on the docker agent (docs/CONTAINERS.md, phase 2).

A fake ``docker`` CLI answers with the shapes the real one does, and a fake
archiver stands in for ``tar | zstd``: what is under test is which files a run
holds, what its manifest says about them, and that verify and retention treat
it like any other run.
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnibackup import dockercapture
from omnibackup.dockercapture import capture_docker, combined_digest



def a_container(cid, name, *, project=None, service="", image="gitea/gitea:1.22",
                image_id=None, state="running", mounts=(), working_dir=""):
    labels = {}
    if project:
        labels = {
            "com.docker.compose.project": project,
            "com.docker.compose.project.working_dir": working_dir,
            "com.docker.compose.project.config_files": f"{working_dir}/compose.yaml",
            "com.docker.compose.service": service,
        }
    return {
        "Id": cid, "Name": "/" + name, "Image": image_id or f"sha256:{cid}",
        "Config": {"Image": image, "Labels": labels},
        "State": {"Status": state},
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        "Mounts": list(mounts),
    }


class FakeDocker:
    """``docker ps / inspect / volume inspect / image inspect``, from fixed data."""

    def __init__(self, containers, volumes=(), images=()):
        self.containers, self.volumes, self.images = containers, list(volumes), list(images)

    def __call__(self, argv, **kwargs):
        def answer(value):
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stderr="",
                                   stdout="\n".join(c["Id"] for c in self.containers))
        if argv[:3] == ["docker", "volume", "inspect"]:
            return answer([v for v in self.volumes if v["Name"] in argv[3:]])
        if argv[:3] == ["docker", "image", "inspect"]:
            return answer([i for i in self.images if i["Id"] in argv[3:]])
        if argv[:2] == ["docker", "inspect"]:
            wanted = set(argv[2:])
            return answer([c for c in self.containers
                           if c["Id"] in wanted or c["Name"].lstrip("/") in wanted])
        raise AssertionError(argv)


class FakeArchiver:
    """Writes an archive whose bytes say what was archived; ``codes`` sets tar's exit."""

    def __init__(self, codes=None):
        self.calls, self.codes = [], dict(codes or {})

    def __call__(self, producer, out: Path):
        self.calls.append((producer, out.name))
        out.write_bytes(("\0".join(producer)).encode() * 3)
        return self.codes.get(out.name, 0), ""


@pytest.fixture
def host(tmp_path):
    """A compose project with a named volume, a bind mount, the socket, and two images."""
    volume_dir = tmp_path / "docker" / "volumes" / "gitea_data" / "_data"
    volume_dir.mkdir(parents=True)
    (volume_dir / "repo.git").write_text("objects")
    bind = tmp_path / "srv" / "gitea" / "db"
    bind.mkdir(parents=True)
    workdir = tmp_path / "srv" / "gitea"
    (workdir / "compose.yaml").write_text("services: {}\n")
    (workdir / ".env").write_text("DB_PASSWORD=secret\n")
    logs = tmp_path / "srv" / "gitea" / "logs"
    logs.mkdir()

    containers = [
        a_container("a1", "gitea-server-1", project="gitea", service="server",
                    image="gitea/gitea:1.22", image_id="sha256:pulled", working_dir=str(workdir),
                    mounts=[{"Type": "volume", "Name": "gitea_data", "Destination": "/data", "RW": True},
                            {"Type": "bind", "Source": "/var/run/docker.sock",
                             "Destination": "/var/run/docker.sock", "RW": False},
                            {"Type": "bind", "Source": str(logs), "Destination": "/logs", "RW": True}]),
        a_container("a2", "gitea-db-1", project="gitea", service="db", image="gitea-db:local",
                    image_id="sha256:built", working_dir=str(workdir),
                    mounts=[{"Type": "bind", "Source": str(bind), "Destination": "/var/lib/mysql", "RW": True}]),
        a_container("b1", "portainer", image="portainer/portainer-ce", image_id="sha256:pulled2"),
    ]
    volumes = [{"Name": "gitea_data", "Mountpoint": str(volume_dir)}]
    images = [
        {"Id": "sha256:pulled", "RepoTags": ["gitea/gitea:1.22"],
         "RepoDigests": ["gitea/gitea@sha256:abc"]},
        {"Id": "sha256:built", "RepoTags": ["gitea-db:local"], "RepoDigests": []},
    ]
    return SimpleNamespace(docker=FakeDocker(containers, volumes, images), root=tmp_path / "share",
                           bind=bind, logs=logs, workdir=workdir)


def capture(host, project="gitea", *, archiver=None, site=None, when=None, keep=None):
    archiver = archiver or FakeArchiver()
    result = capture_docker(project, root=host.root, site_name="DOCKER-01", site=site or {},
                            run=host.docker, archiver=archiver, keep=keep,
                            now=when or datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc))
    return result, archiver


def the_run(host, result, project="gitea"):
    run_dir = host.root / "repository" / project / result["metrics"]["run"]
    return run_dir, json.loads((run_dir / "manifest.json").read_text())


def test_a_project_is_its_definition_its_data_and_its_unregistered_images(host):
    result, archiver = capture(host)

    assert result["state"] == "success", result
    run_dir, manifest = the_run(host, result)
    keys = [d["key"] for d in manifest["disks"]]
    assert keys == ["definition", "volume:gitea_data", f"bind:{host.logs}", f"bind:{host.bind}",
                    "image:gitea-db:local"]
    # Every file named is there, with its own digest; the run's is the digest of those.
    for disk in manifest["disks"]:
        assert (run_dir / disk["image"]).is_file()
        assert len(disk["sha256"]) == 64
    assert manifest["sha256"] == combined_digest(manifest["disks"])
    assert (manifest["hypervisor"], manifest["source"], manifest["consistency"]) == \
        ("docker", "docker", "crash")
    assert manifest["vm"] == "gitea" and manifest["site"] == "DOCKER-01"
    assert manifest["hash_kind"] == "file" and manifest["backup_type"] == "full"
    # The socket is never archived; a pulled image is kept by digest, a built one saved.
    producers = [" ".join(p) for p, _ in archiver.calls]
    assert not any("docker.sock" in p for p in producers)
    kept = {i["id"]: i["kept"] for i in manifest["images"]}
    assert kept == {"sha256:pulled": "digest", "sha256:built": "saved"}
    assert ["docker", "save", "gitea-db:local"] in [p for p, _ in archiver.calls]
    # Nothing is left half-made beside the run.
    assert not (host.root / "staging" / "gitea").exists()
    assert manifest["compose_files"] == [str(host.workdir / "compose.yaml"), str(host.workdir / ".env")]


def test_the_definition_holds_the_inspects_and_the_compose_files(host):
    seen = {}

    class Peek(FakeArchiver):
        def __call__(self, producer, out):
            if out.name == "definition.tar.zst":
                tree = Path(producer[producer.index("--directory") + 1])
                seen.update({str(p.relative_to(tree)): p.read_text()
                             for p in tree.rglob("*") if p.is_file()})
            return super().__call__(producer, out)

    result, _ = capture(host, archiver=Peek())
    assert result["state"] == "success"
    assert {"containers/gitea-server-1.json", "containers/gitea-db-1.json",
            "volumes/gitea_data.json", "compose/compose.yaml", "compose/.env",
            "project.json"} <= set(seen)
    assert "DB_PASSWORD=secret" in seen["compose/.env"]


def test_a_mount_the_site_excludes_is_recorded_not_silently_missing(host):
    site = {"docker": {"projects": {"gitea": {"exclude": [str(host.logs)]}}}}
    result, archiver = capture(host, site=site)
    _, manifest = the_run(host, result)
    assert f"bind:{host.logs}" not in [d["key"] for d in manifest["disks"]]
    assert manifest["excluded"] == [{"type": "bind", "source": str(host.logs),
                                     "reason": "excluded by site.yaml"}]


def test_files_changing_while_read_is_a_warning_on_a_crash_consistent_run(host):
    result, _ = capture(host, archiver=FakeArchiver({"volume-gitea_data.tar.zst": 1}))
    assert result["state"] == "success"
    assert "1 warning" in result["evidence"]
    _, manifest = the_run(host, result)
    assert manifest["warnings"] == ["gitea_data: files changed while they were read (crash-consistent)"]


def test_a_tar_that_fails_fails_the_capture_and_leaves_no_run(host):
    result, _ = capture(host, archiver=FakeArchiver({"volume-gitea_data.tar.zst": 2}))
    assert result["state"] == "failed" and "gitea_data could not be archived" in result["detail"]
    assert list((host.root / "repository" / "gitea").iterdir()) == []
    assert not (host.root / "staging" / "gitea").exists()


def test_an_unchanged_saved_image_is_linked_and_retention_keeps_whole_runs(host):
    first, _ = capture(host, when=datetime(2026, 10, 4, 1, tzinfo=timezone.utc))
    second, archiver = capture(host, when=datetime(2026, 10, 5, 1, tzinfo=timezone.utc))
    old_dir, _ = the_run(host, first)
    new_dir, manifest = the_run(host, second)
    assert ["docker", "save", "gitea-db:local"] not in [p for p, _ in archiver.calls]
    name = "image-gitea-db_local.tar.zst"
    assert os.stat(old_dir / name).st_ino == os.stat(new_dir / name).st_ino
    assert "hard link" in next(i["kept"] for i in manifest["images"] if i["id"] == "sha256:built")

    third, _ = capture(host, when=datetime(2026, 10, 6, 1, tzinfo=timezone.utc), keep=2)
    runs = sorted(p.name for p in (host.root / "repository" / "gitea").iterdir())
    assert runs == sorted([second["metrics"]["run"], third["metrics"]["run"]])
    assert third["metrics"]["pruned"] == [first["metrics"]["run"]]


def test_a_standalone_container_is_its_own_project(host):
    result, _ = capture(host, "portainer")
    _, manifest = the_run(host, result, "portainer")
    assert manifest["kind"] == "container"
    assert [d["key"] for d in manifest["disks"]] == ["definition"]
    assert manifest["warnings"] == ["image portainer/portainer-ce could not be inspected"]


def test_an_unknown_project_names_what_the_host_has(host):
    result, _ = capture(host, "nextcloud")
    assert result["state"] == "failed"
    assert "no project or container named 'nextcloud'" in result["detail"]
    assert "gitea, portainer" in result["detail"]


def test_keep_comes_from_the_project_then_the_site_then_two():
    site = {"docker": {"keep": 5, "projects": {"gitea": {"keep": 3}}}}
    assert dockercapture.keep_for(site, "gitea") == 3
    assert dockercapture.keep_for(site, "other") == 5
    assert dockercapture.keep_for({}, "gitea") == 2


def test_compose_v1_names_its_files_relative_to_the_project_directory(host):
    for container in host.docker.containers[:2]:
        container["Config"]["Labels"]["com.docker.compose.project.config_files"] = "compose.yaml"
    result, _ = capture(host)
    _, manifest = the_run(host, result)
    assert manifest["compose_files"] == [str(host.workdir / "compose.yaml"), str(host.workdir / ".env")]
    assert manifest["warnings"] == []


# ---------------------------------------------------------------------------
# consistency modes (phase 3)
# ---------------------------------------------------------------------------


class Recording(FakeDocker):
    """FakeDocker that also answers pause/unpause/stop/start and logs everything."""

    def __init__(self, base: FakeDocker, events, fail=()):
        super().__init__(base.containers, base.volumes, base.images)
        self.events, self.fail = events, set(fail)

    def __call__(self, argv, **kwargs):
        if argv[:2] in (["docker", "pause"], ["docker", "unpause"], ["docker", "stop"], ["docker", "start"]):
            self.events.append(" ".join(a for a in argv[1:] if a not in ("-t", "60")))
            code = 1 if argv[1] in self.fail else 0
            return SimpleNamespace(returncode=code, stdout="", stderr="refused" if code else "")
        return super().__call__(argv, **kwargs)


class LoggingArchiver(FakeArchiver):
    def __init__(self, events, codes=None):
        super().__init__(codes)
        self.events = events

    def __call__(self, producer, out):
        self.events.append(f"archive {out.name}")
        return super().__call__(producer, out)


def with_mode(host, events, settings, *, fail=(), codes=None, project="gitea"):
    site = {"docker": {"projects": {project: settings}}}
    return capture_docker(project, root=host.root, site_name="DOCKER-01", site=site,
                          run=Recording(host.docker, events, fail),
                          archiver=LoggingArchiver(events, codes),
                          now=datetime(2026, 10, 5, 2, tzinfo=timezone.utc))


def test_dump_mode_streams_each_dump_into_the_run_before_the_data(host):
    events = []
    settings = {"mode": "dump", "dumps": [
        {"service": "db", "user": "mysql", "command": "exec mariadb-dump --all-databases",
         "file": "all-databases.sql"}]}
    result = with_mode(host, events, settings)
    assert result["state"] == "success", result
    assert "databases dumped" in result["evidence"]
    _, manifest = the_run(host, result)
    assert manifest["consistency"] == "dump" and manifest["mode"] == "dump"
    assert manifest["dumps"] == [{"service": "db", "container": "gitea-db-1",
                                  "file": "dump-all-databases.sql.zst"}]
    assert "dump:db" in [d["key"] for d in manifest["disks"]]
    assert events.index("archive dump-all-databases.sql.zst") < events.index("archive volume-gitea_data.tar.zst")


def test_the_dump_runs_inside_the_container_as_the_configured_user(host):
    events, seen = [], []

    class Peek(LoggingArchiver):
        def __call__(self, producer, out):
            seen.append(producer)
            return super().__call__(producer, out)

    site = {"docker": {"projects": {"gitea": {"mode": "dump", "dumps": [
        {"service": "db", "user": "mysql", "command": "exec mariadb-dump -A", "file": "db.sql"}]}}}}
    capture_docker("gitea", root=host.root, site_name="DOCKER-01", site=site,
                   run=Recording(host.docker, events), archiver=Peek(events))
    assert ["docker", "exec", "-u", "mysql", "gitea-db-1", "sh", "-c", "exec mariadb-dump -A"] in seen


def test_a_failed_dump_keeps_the_run_crash_consistent_and_fails_the_job(host):
    events = []
    settings = {"mode": "dump", "dumps": [{"service": "db", "command": "exec pg_dumpall", "file": "db.sql"}]}
    result = with_mode(host, events, settings, codes={"dump-db.sql.zst": 1})
    assert result["state"] == "failed" and "kept crash-consistent" in result["evidence"]
    assert "dump of 'db' failed (exit 1)" in result["detail"]
    _, manifest = the_run(host, result)
    assert manifest["consistency"] == "crash" and manifest["dumps"] == []
    assert not any(d["key"].startswith("dump:") for d in manifest["disks"])


def test_pause_mode_pauses_the_running_containers_only_while_their_data_is_read(host):
    events = []
    result = with_mode(host, events, {"mode": "pause"})
    assert result["state"] == "success" and "paused while read" in result["evidence"]
    pause = events.index("pause gitea-server-1 gitea-db-1")
    unpause = events.index("unpause gitea-server-1 gitea-db-1")
    data = [i for i, e in enumerate(events) if e.startswith("archive volume-") or e.startswith("archive bind-")]
    assert pause < min(data) and max(data) < unpause
    assert events.index("archive definition.tar.zst") < pause  # the definition needs no freeze
    _, manifest = the_run(host, result)
    assert manifest["consistency"] == "pause"
    assert manifest["held"]["how"] == "paused" and manifest["held"]["containers"] == ["gitea-server-1", "gitea-db-1"]
    assert not dockercapture.held_marker(host.root, "gitea").exists()


def test_a_failure_while_paused_still_unpauses(host):
    events = []
    result = with_mode(host, events, {"mode": "pause"}, codes={"volume-gitea_data.tar.zst": 2})
    assert result["state"] == "failed"
    assert events[-1] == "unpause gitea-server-1 gitea-db-1"
    assert not dockercapture.held_marker(host.root, "gitea").exists()


def test_stop_mode_stops_and_starts_again(host):
    events = []
    result = with_mode(host, events, {"mode": "stop"})
    assert result["state"] == "success" and "stopped while read" in result["evidence"]
    stop, start = events.index("stop gitea-server-1 gitea-db-1"), events.index("start gitea-server-1 gitea-db-1")
    data = [i for i, e in enumerate(events) if e.startswith(("archive volume-", "archive bind-"))]
    assert stop < min(data) and max(data) < start
    # Saving a locally built image needs no freeze: it comes after the restart.
    assert events.index("archive image-gitea-db_local.tar.zst") > start


def test_containers_a_crashed_capture_left_paused_are_resumed_by_the_next(host):
    marker = dockercapture.held_marker(host.root, "gitea")
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"how": "paused", "containers": ["gitea-server-1"]}))
    events = []
    result = with_mode(host, events, {})
    assert events[0] == "unpause gitea-server-1"
    assert "earlier capture left containers held; they were resumed" in result["evidence"] + result["detail"]
    assert not marker.exists()


def test_an_unknown_mode_is_crash_consistent():
    assert dockercapture.mode_for({"mode": "freeze"}) == "crash"
    assert dockercapture.mode_for({}) == "crash"


def test_host_files_are_one_archive_and_a_missing_path_is_a_warning(host, tmp_path):
    kit = tmp_path / "kit"
    (kit / "docker-app").mkdir(parents=True)
    (kit / "docker-app" / "Dockerfile").write_text("FROM debian\n")
    (kit / "docker-tools.sh").write_text("#!/bin/sh\n")
    site = {"docker": {"host": {"paths": [f"{kit}/docker-*", f"{kit}/docker-tools.sh",
                                          f"{kit}/not-there"]}}}
    archiver = FakeArchiver()
    result = capture_docker("host-files", root=host.root, site_name="DOCKER-01", site=site,
                            run=host.docker, archiver=archiver)
    assert result["state"] == "success", result
    assert result["metrics"]["paths"] == 2 and "not-there: nothing there" in result["detail"]
    producer, name = archiver.calls[0]
    assert name == "host-files.tar.zst" and producer[-2:] == [
        f"{kit}/docker-app".lstrip("/"), f"{kit}/docker-tools.sh".lstrip("/")]
    assert producer[producer.index("--directory") + 1] == "/"
    run_dir = host.root / "repository" / "host-files" / result["metrics"]["run"]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["kind"] == "host" and manifest["disks"][0]["key"] == "host"
    assert manifest["sha256"] == combined_digest(manifest["disks"])


