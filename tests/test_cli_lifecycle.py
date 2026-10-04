"""CLI lifecycle checks use dummy recipes and isolated outputs/machine locks."""
import json
import fcntl
import os
import signal
import subprocess
import sys
import time

import pytest

from local_assets_engine.jobs import JobBusy, JobStore
from local_assets_engine import cli


@pytest.mark.parametrize("termination", [signal.SIGTERM, signal.SIGINT])
def test_standalone_cli_cleans_stubborn_measured_child(termination, tmp_path):
    marker = tmp_path / "child.pid"
    child = (
        "import os,signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    script = f'''import sys
from pathlib import Path
from local_assets_engine import cli, measure, runner
from local_assets_engine.recipes import RECIPES, Recipe
runner.generation_lock_path=lambda: Path({str(tmp_path / 'machine.lock')!r})
measure.FORCE_KILL_AFTER_S=.2
def run(ctx):
    with ctx.stage('dummy','dummy') as stage:
        stage.run([sys.executable,'-c',{child!r}])
RECIPES['dummy']=Recipe('dummy','dummy',lambda p,a,b: ({{}},'dummy'),run)
cli._server_running=lambda: False
sys.exit(cli._run('dummy','{{}}'))
'''
    parent = subprocess.Popen([sys.executable, "-c", script],
                              env=dict(os.environ, LOCAL_ASSETS_OUTPUT=str(tmp_path)),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            if parent.poll() is not None:
                pytest.fail(parent.communicate()[1])
            time.sleep(0.01)
        assert marker.exists()
        parent.send_signal(termination)
        time.sleep(0.03)
        if parent.poll() is None:
            parent.send_signal(termination)  # cleanup also survives a repeated signal
        stdout, stderr = parent.communicate(timeout=8)
        assert parent.returncode == 1, (stdout, stderr)
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)
        job = JobStore(tmp_path / "jobs").list()[0]
        assert job["state"] == "cancelled"
        process = job["stages"][0]["processes"][0]
        assert process["state"] == "cancelled" and process["peakMemoryBytes"] > 0
        for path in (tmp_path / "machine.lock", tmp_path / "jobs/.engine.lock"):
            with path.open("a") as lane:
                fcntl.flock(lane, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if marker.exists():
            try:
                os.killpg(os.getpgid(int(marker.read_text())), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_server_backed_cli_interrupt_requests_cancel_and_waits(monkeypatch):
    calls = []
    job = {"id": "20261004-000000-abcd", "state": "cancelled", "error": None, "assets": [], "stages": []}
    monkeypatch.setattr(cli, "_server_running", lambda: True)
    def api(method, path, payload=None, timeout=5):
        calls.append((method, path))
        return job
    monkeypatch.setattr(cli, "_api", api)
    attempts = 0
    def progress(_load):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KeyboardInterrupt
        return job
    monkeypatch.setattr(cli, "_print_progress", progress)
    assert cli._run("image", '{}') == 1
    assert calls == [("POST", "/api/jobs"), ("POST", "/api/jobs/20261004-000000-abcd/cancel")]
    assert attempts == 2


def test_sigkill_controller_cannot_release_the_live_model_lane(tmp_path):
    marker = tmp_path / "child.pid"
    machine = tmp_path / "machine.lock"
    child = f'''import os,time,pathlib,json
def inherited(path):
    target=os.stat(path)
    for fd in range(3,256):
        try:
            value=os.fstat(fd)
            if (value.st_dev,value.st_ino)==(target.st_dev,target.st_ino): return True
        except OSError: pass
    return False
pathlib.Path({str(marker)!r}).write_text(json.dumps({{"pid":os.getpid(),"timePid":os.getppid(),"machineLease":inherited({str(machine)!r}),"storeLease":inherited({str(tmp_path / 'jobs/.engine.lock')!r}),"processLease":inherited(next(pathlib.Path({str(tmp_path / 'jobs')!r}).glob('*/.process.lock')))}}))
time.sleep(30)
'''
    script = f'''import sys
from pathlib import Path
from local_assets_engine import cli, runner
from local_assets_engine.recipes import RECIPES, Recipe
runner.generation_lock_path=lambda: Path({str(machine)!r})
def run(ctx):
    with ctx.stage('dummy','dummy') as stage: stage.run([sys.executable,'-c',{child!r}])
RECIPES['dummy']=Recipe('dummy','dummy',lambda p,a,b: ({{}},'dummy'),run)
cli._server_running=lambda: False
sys.exit(cli._run('dummy','{{}}'))
'''
    parent = subprocess.Popen([sys.executable, "-c", script],
                              env=dict(os.environ, LOCAL_ASSETS_OUTPUT=str(tmp_path)),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        identity = json.loads(marker.read_text())
        pid = identity["pid"]
        assert identity["machineLease"] and identity["storeLease"] and identity["processLease"]
        parent.kill()
        parent.wait(timeout=3)
        os.kill(pid, 0)
        store = JobStore(tmp_path / "jobs")
        job = store.list()[0]
        process_lock = store.job_dir(job["id"]) / ".process.lock"
        assert store.recover_interrupted() == 1
        assert store.process_in_use(job["id"])
        with pytest.raises(JobBusy):
            store.reserve_trash(job["id"], "a" * 32)
        # Both the time wrapper and its exec'd child inherit these flock leases.
        for path in (machine, tmp_path / "jobs/.engine.lock", process_lock):
            with path.open("a") as lane:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lane, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # The model also retains the lane if the measurement wrapper dies.
        os.kill(identity["timePid"], signal.SIGKILL)
        deadline = time.monotonic() + 5
        while True:
            try:
                os.kill(identity["timePid"], 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        os.kill(pid, 0)
        for path in (machine, tmp_path / "jobs/.engine.lock", process_lock):
            with path.open("a") as lane:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lane, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        deadline = time.monotonic() + 5
        for path in (machine, tmp_path / "jobs/.engine.lock", process_lock):
            with path.open("a") as lane:
                while True:
                    try:
                        fcntl.flock(lane, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        assert time.monotonic() < deadline, "stage descendants leaked the lane"
                        time.sleep(0.01)
        assert not store.process_in_use(job["id"])
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if marker.exists():
            try:
                os.killpg(os.getpgid(json.loads(marker.read_text())["pid"]), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_orphan_process_lease_blocks_own_folder_and_active_input_trash(tmp_path):
    from local_assets_engine.jobs import JobBusy
    from local_assets_engine.library import job_dependents
    store = JobStore(tmp_path / "jobs")
    source = store.create("fake", {}, "inert input")
    input_file = store.job_dir(source["id"]) / "input.txt"
    input_file.write_text("inert bytes")
    store.update(source["id"], lambda j: j.update(state="done"))
    marker = tmp_path / "identity.json"
    child = f'''import os,time,pathlib,json
input_file=open({str(input_file)!r},'rb')
data=input_file.read()
pathlib.Path({str(marker)!r}).write_text(json.dumps({{"pid":os.getpid(),"timePid":os.getppid(),"readBytes":len(data)}}))
time.sleep(30)
'''
    script = f'''import sys
from pathlib import Path
from local_assets_engine.jobs import JobStore
from local_assets_engine.runner import Runner
from local_assets_engine.recipes import Recipe
store=JobStore(Path({str(store.root)!r}))
def run(ctx):
    with ctx.stage('dummy','dummy') as stage: stage.run([sys.executable,'-c',{child!r}])
r=Runner(store,recipes={{'fake':Recipe('fake','fake',lambda p,a,b: (p,'dummy'),run)}},presets_loader=dict,machine_lock_path=Path({str(tmp_path/'machine.lock')!r}))
r.run_job(r.create('fake',{{'source':{{'jobId':{source['id']!r},'assetId':'a01'}},'inputPath':{str(input_file)!r}}})['id'])
'''
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    identity = None
    try:
        deadline = time.monotonic() + 5
        while identity is None and time.monotonic() < deadline:
            if marker.exists():
                try:
                    identity = json.loads(marker.read_text())
                except json.JSONDecodeError:
                    pass
            time.sleep(0.01)
        assert identity is not None and identity["readBytes"] == len("inert bytes")
        parent.kill()
        parent.wait(timeout=3)
        os.kill(identity["timePid"], signal.SIGKILL)
        derived = next(j for j in store.list(limit=None) if j["id"] != source["id"])
        assert store.recover_interrupted() == 1
        assert store.load(derived["id"])["state"] == "failed"
        assert store.process_in_use(derived["id"])
        with pytest.raises(JobBusy):
            store.reserve_trash(derived["id"], "a" * 32)
        with pytest.raises(JobBusy):
            store.assert_sources_available({"source": {"jobId": derived["id"], "assetId": "a01"}})
        def no_active_input(job):
            _, active = job_dependents(store, store.list(limit=None))
            if active.get(job["id"]):
                raise JobBusy("active original reader")
        with pytest.raises(JobBusy, match="active original reader"):
            store.reserve_trash(source["id"], "b" * 32, validate=no_active_input)
        os.killpg(os.getpgid(identity["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 5
        while store.process_in_use(derived["id"]) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not store.process_in_use(derived["id"])
        assert store.reserve_trash(source["id"], "c" * 32, validate=no_active_input)["trashReservation"]["token"] == "c" * 32
        assert store.reserve_trash(derived["id"], "d" * 32)["trashReservation"]["token"] == "d" * 32
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if identity is not None:
            try:
                os.killpg(os.getpgid(identity["pid"]), signal.SIGKILL)
            except ProcessLookupError:
                pass
