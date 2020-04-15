import os

import pytest
from mock import patch

import runez


CHATTER = """
#!/bin/bash

echo "$@"
ls
ls some-file
echo
"""


@pytest.mark.skipif(runez.WINDOWS, reason="Not supported on windows")
def test_capture(temp_folder, logged):
    chatter = runez.resolved_path("chatter")
    assert runez.write(chatter, CHATTER.strip(), fatal=False) == 1
    assert runez.make_executable(chatter, fatal=False) == 1

    assert runez.run(chatter, fatal=False) == "chatter"
    assert "Running: chatter" in logged.pop()

    r = runez.run(chatter, include_error=True, fatal=False)
    assert r.startswith("chatter")
    assert "No such file" in r
    assert "Running: chatter" in logged.pop()

    r = runez.run(chatter, "hello", "-a", 0, "-b", None, 1, 2, None, "foo bar", fatal=False)
    assert r.startswith("hello -a 0 1 2 foo bar")
    assert 'Running: chatter hello -a 0 1 2 "foo bar"' in logged.pop()


@pytest.mark.skipif(runez.WINDOWS, reason="Not supported on windows")
def test_executable(temp_folder):
    with runez.CaptureOutput(dryrun=True) as logged:
        assert runez.make_executable("some-file") == 1
        assert "Would make some-file executable" in logged

    assert runez.touch("some-file") == 1
    assert runez.make_executable("some-file") == 1
    assert runez.is_executable("some-file")
    assert runez.make_executable("some-file") == 0

    assert runez.delete("some-file") == 1
    assert not runez.is_executable("some-file")

    with runez.CaptureOutput() as logged:
        assert runez.make_executable("/dev/null/some-file", fatal=False) == -1
        assert "does not exist, can't make it executable" in logged


def test_program():
    assert runez.get_dev_folder("") is None
    assert runez.get_dev_folder("some-path/.venv/bar/baz") == "some-path/.venv"
    assert runez.get_dev_folder("some-path/.tox/bar/baz") == "some-path/.tox"
    assert runez.get_dev_folder("some-path/build/bar/baz") == "some-path/build"

    program_path = runez.get_program_path(path="/some/program")
    assert runez.basename(program_path) == "program"


def test_which():
    assert runez.which(None) is None
    assert runez.which("/dev/null") is None
    assert runez.which("dev/null") is None
    assert runez.which("python")


def test_require_installed():
    with patch("runez.program.which", return_value="/bin/foo"):
        assert runez.require_installed("foo") is True

    with patch("runez.program.which", return_value=None):
        assert "foo is not installed, see http://..." in runez.verify_abort(runez.require_installed, "foo", "see http://...")

        linux = {"linux": "see http:..."}
        with patch("runez.program.get_platform", return_value="darwin"):
            assert "foo is not installed, run: `brew install foo`" in runez.verify_abort(runez.require_installed, "foo", "foo")
            assert "run: `brew install foo`" in runez.verify_abort(runez.require_installed, "foo")
            text = runez.verify_abort(runez.require_installed, "foo", instructions=linux)
            assert "not installed:\n" in text
            assert "- on linux: see http:..." in text

        with patch("runez.program.get_platform", return_value="linux"):
            assert "run: `apt install foo`" in runez.verify_abort(runez.require_installed, "foo")
            assert "not installed, see http:..." in runez.verify_abort(runez.require_installed, "foo", instructions=linux)


def test_pids():
    if not runez.WINDOWS:
        assert runez.check_pid(0)

    assert runez.check_pid(os.getpid())
    assert not runez.check_pid(1)


@pytest.mark.skipif(runez.WINDOWS, reason="Not supported on windows")
def test_run(temp_folder):
    assert runez.program.added_env_paths(None) is None
    ls = runez.which("ls")
    runez.write("foo", "#!/bin/sh\necho hello")
    os.chmod("foo", 0o755)

    with runez.CaptureOutput(dryrun=True) as logged:
        assert "Would run: /dev/null" in runez.run("/dev/null", fatal=False)
        assert "Would run: /dev/null" in logged.pop()

        assert runez.run("foo", stdout=None, stderr=None) == 0
        assert "Would run: foo" in runez.run("foo")

        assert runez.run(ls, ".", stdout=None, stderr=None) == 0

        assert "Would run:" in runez.run(ls, "--invalid-flag", None, ".")
        assert "Would run: %s ." % ls in logged.pop()

    with runez.CaptureOutput() as logged:
        assert runez.run("/dev/null", fatal=False) is False
        assert "/dev/null is not installed" in logged.pop()

        assert runez.run("foo", stdout=None, stderr=None) == 0
        assert runez.run("foo") == "hello"

        # Success not influenced by `fatal`
        assert runez.run(ls, ".", stdout=None, stderr=None) == 0
        assert runez.run(ls, ".", stdout=None, stderr=None, fatal=None) == 0
        assert runez.run(ls, ".", stdout=None, stderr=None, fatal=False) == 0
        assert runez.run(ls, ".", stdout=None, stderr=None, fatal=True) == 0

        # Failure is influenced by `fatal`
        exit_code = runez.run(ls, "--foo", ".", stdout=None, stderr=None, fatal=None)
        assert isinstance(exit_code, int) and exit_code != 0

        exit_code = runez.run(ls, "--foo", ".", stdout=None, stderr=None, fatal=False)
        assert isinstance(exit_code, int) and exit_code != 0

        assert "exited with code" in runez.verify_abort(runez.run, ls, "--foo", ".", stdout=None, stderr=None, fatal=True)

        assert runez.touch("sample") == 1
        files = runez.run(ls, "--invalid-flag", None, ".", path_env={"PATH": ":."})
        assert "foo" in files
        assert "sample" in files
        assert "Running: %s ." % ls in logged.pop()

        assert runez.run(ls, "some-file", fatal=False) is False
        assert "Running: %s some-file" % ls in logged
        assert "exited with code" in logged
        assert "No such file" in logged.pop()


def test_python_run():
    with runez.CaptureOutput():
        # Success not influenced by `fatal`
        assert runez.run("python", "--version", stdout=None, stderr=None) == 0
        assert runez.run("python", "--version", stdout=None, stderr=None, fatal=None) == 0
        assert runez.run("python", "--version", stdout=None, stderr=None, fatal=False) == 0
        assert runez.run("python", "--version", stdout=None, stderr=None, fatal=True) == 0

        # Failure is influenced by `fatal`
        exit_code = runez.run("python", "--invalid-flag", stdout=None, stderr=None, fatal=None)
        assert isinstance(exit_code, int) and exit_code != 0

        exit_code = runez.run("python", "--invalid-flag", stdout=None, stderr=None, fatal=False)
        assert isinstance(exit_code, int) and exit_code != 0

        assert "exited with code" in runez.verify_abort(runez.run, "python", "--invalid-flag", stdout=None, stderr=None, fatal=True)


def test_failed_run(logged):
    with patch("subprocess.Popen", side_effect=Exception("testing")):
        assert runez.run("python", "--version", fatal=False) is False
        assert "python failed: testing" in logged


@pytest.mark.skipif(runez.WINDOWS, reason="Not supported on windows")
def test_wrapped_run():
    with patch.dict(os.environ, {"PYCHARM_HOSTED": "1"}):
        a = runez.program.wrapped_args(["python", "-mvenv", "foo"])
        assert len(a) == 5
        assert a[0] == "/bin/sh"
        assert os.path.basename(a[1]) == "pydev-wrapper.sh"
