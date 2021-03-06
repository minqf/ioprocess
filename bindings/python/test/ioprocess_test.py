#
# Copyright 2012 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

import errno
import gc
import io
import logging
import os
import platform
import pprint
import shutil
import stat
import sys
import time

from contextlib import closing, contextmanager
from functools import wraps
from tempfile import mkstemp, mkdtemp
from threading import Thread
from unittest import TestCase
from unittest.case import SkipTest
from weakref import ref

import pytest

from ioprocess import (
    IOProcess,
    ERR_IOPROCESS_CRASH,
    Closed,
    Timeout,
    config,
    clear_cloexec
)

elapsed_time = lambda: os.times()[4]

config.IOPROCESS_PATH = os.path.join(os.getcwd(),
                                     "../../src/ioprocess")
IOProcess._DEBUG_VALGRIND = os.environ.get("ENABLE_VALGRIND", False)

_VALGRIND_RUNNING = IOProcess._DEBUG_VALGRIND

IOProcess._TRACE_DEBUGGING = True


log = logging.getLogger("Test")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-7s (%(threadName)s) [%(name)s] %(message)s"
)


requires_unprivileged_user = pytest.mark.skipif(
    os.geteuid() == 0, reason="This test can not run as root")


def on_s390x():
    return platform.machine() == "s390x"


def skip_in_valgrind(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _VALGRIND_RUNNING:
            raise SkipTest("Tests can't be run in valgrind")

        return f(*args, **kwargs)

    return wrapper


class IOProcessTests(TestCase):

    def testMaxRequestsAfterFillingThreadPool(self):
        proc = IOProcess(timeout=10, max_threads=3, max_queued_requests=0)
        with closing(proc):
            t1 = Thread(target=proc.echo, args=("hello", 2))
            t2 = Thread(target=proc.echo, args=("hello", 2))
            t3 = Thread(target=proc.echo, args=("hello", 2))
            t1.start()
            t2.start()
            t3.start()

            for t in (t1, t2, t3):
                t.join()

            t1 = Thread(target=proc.echo, args=("hello", 2))
            t2 = Thread(target=proc.echo, args=("hello", 2))
            t1.start()
            t2.start()
            # Make sure the echo calls are sent prior to the ping otherwise one
            # of them would fail and ping() would pass
            time.sleep(0.5)
            proc.ping()
            t1.join()
            t2.join()

    def testPing(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            self.assertEquals(proc.ping(), "pong")

    def test2SubsequentCalls(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            self.assertEquals(proc.ping(), "pong")
            self.assertEquals(proc.ping(), "pong")

    def testEcho(self):
        data = """The Doctor: But I don't exist in your world!
                  Brigade Leader: Then you won't feel the bullets when we
                  shoot you."""  # (C) BBC - Doctor Who
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            self.assertEquals(proc.echo(data), data)

    def testUnicodeEcho(self):
        data = u'\u05e9\u05dc\u05d5\u05dd'
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            self.assertEquals(proc.echo(data), data)

    def testMultitask(self):
        """
        Makes sure that when multiple requests are sent the results come
        back with correct IDs
        """
        threadnum = 10
        # We want to run all requests in parallel, so have one ioprocess thread
        # per client thread.
        proc = IOProcess(timeout=10, max_threads=threadnum)
        with closing(proc):
            errors = []
            threads = []

            def test(n):
                if proc.echo(str(n), 1) != str(n):
                    errors.append(n)

        for i in range(threadnum):
            t = Thread(target=test, args=(i,))
            t.start()
            threads.append(t)

        for thread in threads:
            thread.join()

        self.assertEquals(len(errors), 0)

    def testRecoverAfterCrash(self):
        data = """Brigadier: Is there anything I can do?
                  Third Doctor: Yes, pass me a silicon rod.
                                [Stirs cup of tea with it]
                  Brigadier: I meant is there anything UNIT can do about this
                  space lightning business?"""  # (C) BBC - Doctor Who
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            self.assertEquals(proc.echo(data), data)
            self.assertTrue(proc.crash())
            self.assertEquals(proc.echo(data), data)

    def testPendingRequestInvalidationOnCrash(self):
        data = """The Doctor: A straight line may be the shortest distance
                  between two points, but it is by no means the most
                  interesting."""  # (C) BBC - Doctor Who
        proc = IOProcess(timeout=12, max_threads=5)
        with closing(proc):
            res = [False]

            def sendCmd():
                try:
                    proc.echo(data, 10)
                except OSError as e:
                    if e.errno == ERR_IOPROCESS_CRASH:
                        res[0] = True
                    else:
                        log.error("Got unexpected error", exc_info=True)

            t = Thread(target=sendCmd)
            t.start()

            time.sleep(1)
            proc.crash()
            t.join()
            self.assertTrue(res[0])

    def testTimeout(self):
        data = """Madge: Are you the new caretaker?
                  The Doctor: Usually called "The Doctor." Or "The Caretaker."
                  Or "Get off this planet." Though, strictly speaking, that
                  probably isn't a name."""  # (C) BBC - Doctor Who
        # Using smaller timeout to ensure the echo will time out.
        proc = IOProcess(timeout=1, max_threads=5)
        with closing(proc):
            try:
                self.assertEquals(proc.echo(data, 10), data)
            except Timeout:
                return

            self.fail("Exception not raised")

    @skip_in_valgrind
    def testManyRequests(self):
        data = """Lily: What's happening?
                  The Doctor: No idea. Just do what I do: hold tight and
                  pretend it's a plan."""  # (C) BBC - Doctor Who
        proc = IOProcess(timeout=30, max_threads=5)
        with closing(proc):
            # even though we theoretically go back to a stable state, some
            # objects might have increased their internal buffers and mem
            # fragmantation might have caused some data to be spanned on more
            # pages then it originally did.
            acceptableRSSIncreasKB = 100

            startRSS = proc.memstat()['rss']
            # This way we catch evey leak that is more then one 0.1KB per call
            many = 300
            for i in range(many):
                self.assertEquals(proc.echo(data), data)
            endRSS = proc.memstat()['rss']
            RSSDiff = endRSS - startRSS
            log.debug("RSS difference was %d KB, %d per request", RSSDiff,
                      RSSDiff / many)
            # This only tests for leaks in the main request\response process.
            self.assertTrue(RSSDiff < acceptableRSSIncreasKB,
                            "Detected a leak sized %d KB" % RSSDiff)

    @pytest.mark.xfail(on_s390x(), reason="Unknown")
    def testStatvfs(self):
        data = b'''Peter Puppy: Once again, evil is as rotting meat before
                                the maggots of justice!
                   Earthworm Jim: Thank you for cramming that delightful image
                                  into my brain, Peter.
                '''  # (C) Universal Cartoon Studios - Earth Worm Jim
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            fd, path = mkstemp()
            try:
                os.write(fd, data)
                os.close(fd)
                pystat = os.statvfs(path)
                mystat = proc.statvfs(path)
                for f in ("f_bsize", "f_frsize", "f_blocks",
                          "f_fsid", "f_flag", "f_namemax"):

                    try:
                        getattr(pystat, f)
                    except AttributeError:
                        # The results might be more comprehansive then python
                        # implementation
                        continue

                    log.debug("Testing field '%s'", f)
                    self.assertEquals(getattr(mystat, f), getattr(pystat, f))
            finally:
                os.unlink(path)

    def testStatFail(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            try:
                proc.stat("/I do not exist")
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise
            else:
                raise AssertionError("OSError was not raised")

    def testMissingArguemt(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            try:
                proc._sendCommand("echo", {}, proc.timeout)
            except OSError as e:
                self.assertEquals(e.errno, errno.EINVAL)

    def testNonExistingMethod(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            try:
                proc._sendCommand("Implode", {}, proc.timeout)
            except OSError as e:
                self.assertEquals(e.errno, errno.EINVAL)

    def testPathExists(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            fd, path = mkstemp()
            try:
                os.close(fd)
                self.assertTrue(proc.pathExists(path))
            finally:
                os.unlink(path)

    def testPathDoesNotExist(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            self.assertFalse(proc.pathExists("/I do not exist"))

    def testRename(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            fd, oldpath = mkstemp()
            newpath = oldpath + ".new"
            try:
                os.close(fd)
                self.assertTrue(proc.rename(oldpath, newpath))
            finally:
                try:
                    os.unlink(oldpath)
                except:
                    pass
                try:
                    os.unlink(newpath)
                except:
                    pass

    def testRenameFail(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            try:
                proc.rename("/I/do/not/exist", "/Dsadsad")
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise
            else:
                raise AssertionError("OSError was not raised")

    def testUnlink(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            fd, path = mkstemp()
            try:
                os.close(fd)
                self.assertTrue(proc.unlink(path))
            finally:
                try:
                    os.unlink(path)
                except:
                    pass

    def testUnlinkFail(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            try:
                proc.unlink("/I do not exist")
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise
            else:
                raise AssertionError("OSError was not raised")

    def testLink(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            fd, oldpath = mkstemp()
            newpath = oldpath + ".new"
            try:
                os.close(fd)
                self.assertTrue(proc.link(oldpath, newpath))
            finally:
                os.unlink(oldpath)
                try:
                    os.unlink(newpath)
                except:
                    pass

    def testLinkFail(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            try:
                proc.link("/I/do/not/exist", "/Dsadsad")
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise
            else:
                raise AssertionError("OSError was not raised")

    def testSymlink(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            fd, oldpath = mkstemp()
            newpath = oldpath + ".new"
            try:
                os.close(fd)
                self.assertTrue(proc.symlink(oldpath, newpath))
                self.assertEquals(os.path.realpath(newpath),
                                  os.path.normpath(oldpath))
            finally:
                os.unlink(oldpath)
                try:
                    os.unlink(newpath)
                except:
                    pass

    def testSymlinkFail(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            try:
                proc.symlink("/Dsadsad", "/I/do/not/exist")
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise
            else:
                raise AssertionError("OSError was not raised")

    def testChmod(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            fd, path = mkstemp()
            targetMode = os.W_OK | os.R_OK
            try:
                os.chmod(path, 0)
                os.close(fd)
                self.assertFalse(os.stat(path).st_mode & targetMode)
                self.assertTrue(proc.chmod(path, targetMode))
                self.assertTrue(os.stat(path).st_mode & targetMode)
            finally:
                os.unlink(path)

    def testChmodFail(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            try:
                proc.chmod("/I/do/not/exist", 0)
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise
            else:
                raise AssertionError("OSError was not raised")

    def testListdir(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            path = mkdtemp()
            matches = []
            for i in range(10):
                matches.append(os.path.join(path, str(i)))
                with open(matches[-1], "w") as f:
                    f.write("A")

            matches.sort()

            try:
                remoteMatches = proc.listdir(path)
                remoteMatches.sort()
                flist = os.listdir(path)
                flist.sort()
                self.assertEquals(remoteMatches, flist)
            finally:
                shutil.rmtree(path)

    def testGlob(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            path = mkdtemp()
            matches = []
            for i in range(10):
                matches.append(os.path.join(path, str(i)))
                with open(matches[-1], "w") as f:
                    f.write("A")

            matches.sort()

            try:
                remoteMatches = proc.glob(os.path.join(path, "*"))
                remoteMatches.sort()
                self.assertEquals(remoteMatches, matches)
            finally:
                shutil.rmtree(path)

    def testRmdir(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            path = mkdtemp()

            try:
                proc.rmdir(path)
                self.assertFalse(os.path.exists(path))
            finally:
                try:
                    shutil.rmtree(path)
                except:
                    pass

    def testMkdir(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            path = mkdtemp()
            shutil.rmtree(path)

            try:
                proc.mkdir(path)
                self.assertTrue(os.path.exists(path))
            finally:
                try:
                    shutil.rmtree(path)
                except:
                    pass

    def testLexists(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            path = "/tmp/linktest.ioprocesstest"
            try:
                os.unlink(path)
            except OSError:
                pass
            os.symlink("dsadsadsadsad", path)
            try:
                self.assertTrue(proc.lexists(path))
            finally:
                os.unlink(path)

    def testGlobNothing(self):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            remoteMatches = proc.glob(os.path.join("/dsadasd", "*"))
            self.assertEquals(remoteMatches, [])

    def test_closed(self):
        proc = IOProcess(timeout=10, max_threads=5)
        proc.close()
        self.assertRaises(Closed, proc.echo, "foo", 1)

    def test_close_terminates_process(self):
        for i in range(100):
            proc = IOProcess(timeout=10, max_threads=5)
            proc.close()
            self.assertFalse(os.path.exists("/proc/%d" % proc.pid),
                             "process %s did not terminate" % proc)

    def test_close_unrelated_fds(self):
        # Make inheritable file descriptor.
        with open(__file__) as my_file:
            clear_cloexec(my_file.fileno())
            proc = IOProcess(timeout=10, max_threads=5)
            with closing(proc):
                # Wait until ready.
                proc.ping()
                proc_fd = "/proc/%d/fd" % proc.pid
                child_fds = [int(fd) for fd in os.listdir(proc_fd)]
                # My file descriptor must not be inherited.
                self.assertNotIn(my_file.fileno(), child_fds)


def test_max_requests():
    proc = IOProcess(timeout=10, max_threads=1, max_queued_requests=1)
    with closing(proc):
        t1 = Thread(target=proc.echo, args=("hello", 2))
        t2 = Thread(target=proc.echo, args=("hello", 2))
        t1.start()
        t2.start()
        # Make sure the echo calls are sent prior to the ping otherwise one
        # of them would fail and ping() would pass.
        time.sleep(0.5)

        try:
            with pytest.raises(OSError) as e:
                proc.ping()
            assert e.value.errno == errno.EAGAIN
        finally:
            t1.join()
            t2.join()


def test_fsyncpath_directory(tmpdir):
    proc = IOProcess(timeout=10, max_threads=1)
    with closing(proc):
        # No easy way to test that we actually fsync this path. Lets just
        # call it to make sure it does not fail.
        proc.fsyncPath(str(tmpdir))


def test_fsyncpath_missing(tmpdir):
    proc = IOProcess(timeout=10, max_threads=1)
    with closing(proc):
        with pytest.raises(OSError) as e:
            proc.fsyncPath("/no/such/file")
        assert e.value.errno == errno.ENOENT


def test_stat_file(tmpdir):
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        file = tmpdir.join("file")
        file.write(b"x" * 100)
        file = str(file)
        check_stat(proc.stat(file), os.stat(file))


def test_stat_dir(tmpdir):
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        dir = str(tmpdir)
        check_stat(proc.stat(dir), os.stat(dir))


def test_stat_link(tmpdir):
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        src = tmpdir.join("src")
        src.write(b"x" * 100)
        src = str(src)
        link = str(tmpdir.join("link"))
        os.symlink(src, link)
        check_stat(proc.stat(link), os.stat(link))


def test_stat_link_missing(tmpdir):
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        src = str(tmpdir.join("file"))
        link = str(tmpdir.join("link"))
        os.symlink(src, link)
        with pytest.raises(OSError) as myerror:
            proc.stat(link)
        with pytest.raises(OSError) as pyerror:
            os.stat(link)
        assert myerror.value.errno == pyerror.value.errno


def test_lstat_file(tmpdir):
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        file = tmpdir.join("file")
        file.write(b"x" * 100)
        file = str(file)
        check_stat(proc.lstat(file), os.lstat(file))


def test_lstat_dir(tmpdir):
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        dir = str(tmpdir)
        check_stat(proc.lstat(dir), os.lstat(dir))


def test_lstat_link(tmpdir):
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        src = tmpdir.join("src")
        src.write(b"x" * 100)
        src = str(src)
        link = str(tmpdir.join("link"))
        os.symlink(src, link)
        check_stat(proc.lstat(link), os.lstat(link))


def test_lstat_link_missing(tmpdir):
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        src = str(tmpdir.join("file"))
        link = str(tmpdir.join("link"))
        os.symlink(src, link)
        check_stat(proc.lstat(link), os.lstat(link))


def check_stat(mystat, pystat):
    for f in mystat._fields:
        if f in ("st_atime", "st_mtime", "st_ctime"):
            # These are float\double values and due to the many
            # conversion the values experience during marshaling
            # they cannot be equated. The rest of the fields are a
            # good enough test.
            continue
        assert getattr(mystat, f) == getattr(pystat, f)


@pytest.mark.parametrize("size", [0, 1, 512, 4096, 1024**2 + 1])
def test_writefile(tmpdir, size):
    data = b'x' * size
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        path = str(tmpdir.join("file"))
        proc.writefile(path, data)
        with io.open(path, 'rb') as f:
            written = f.read()
        assert written == data


# TODO: Use userstorage instead of assuming CI storage sector size.
@pytest.mark.parametrize("size", [
    0,
    pytest.param(
        512,
        marks=pytest.mark.xfail(
            on_s390x(),
            reason="Test assumes 512 block size but on out s390x slave "
                   "storage uses 4k sector size")),
    4096,
    1024**2
])
def test_writefile_direct_aligned(tmpdir, size):
    data = b'x' * size
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        path = str(tmpdir.join("file"))
        proc.writefile(path, data, direct=True)
        with io.open(path, 'rb') as f:
            written = f.read()
        assert written == data


def test_writefile_direct_unaligned(tmpdir):
    data = b'unaligned data'
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        path = str(tmpdir.join("file"))
        with pytest.raises(OSError) as e:
            proc.writefile(path, data, direct=True)
        assert e.value.errno == errno.EINVAL


@pytest.mark.parametrize("size", [0, 1, 42, 512, 4096, 1024**2 + 1])
def test_readfile(tmpdir, size):
    data = b'x' * size
    path = str(tmpdir.join("file"))
    with io.open(path, "wb") as f:
        f.write(data)

    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        read = proc.readfile(path)
        assert read == data


@pytest.mark.parametrize("size", [0, 1, 42, 512, 4096, 1024**2 + 1])
def test_readfile_direct(tmpdir, size):
    data = b'x' * size
    path = str(tmpdir.join("file"))
    with io.open(path, "wb") as f:
        f.write(data)
        os.fsync(f.fileno())

    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        read = proc.readfile(path, direct=True)
        assert read == data


@pytest.mark.parametrize("direct", [
    pytest.param(True, id="direct"),
    pytest.param(False, id="buffered"),
])
def test_readfile_missing(tmpdir, direct):
    path = str(tmpdir.join("file"))
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        with pytest.raises(OSError) as e:
            read = proc.readfile(path, direct=direct)
        assert e.value.errno == errno.ENOENT


ACCESS_PARAMS = [
    (0o755, os.R_OK, True),
    (0o300, os.R_OK, False),
    (0o744, os.W_OK, True),
    (0o444, os.W_OK, False),
    (0o755, os.X_OK, True),
    (0o400, os.X_OK, False),
    (0o300, os.W_OK | os.X_OK, True),
    (0o300, os.R_OK | os.W_OK | os.X_OK, False),
]


@pytest.mark.parametrize("mode, permission, expected_result", ACCESS_PARAMS)
@requires_unprivileged_user
def test_access_file(tmpdir, mode, permission, expected_result):
    proc = IOProcess(timeout=10, max_threads=5)
    f = tmpdir.join("file")
    f.write("")
    path = str(f)

    with closing(proc):
        with chmod(path, mode):
            assert proc.access(path, permission) == os.access(path, permission)
            assert proc.access(path, permission) == expected_result


@pytest.mark.parametrize("mode, permission, expected_result", ACCESS_PARAMS)
@requires_unprivileged_user
def test_access_directory(tmpdir, mode, permission, expected_result):
    proc = IOProcess(timeout=10, max_threads=5)
    d = tmpdir.mkdir("subdir")
    path = str(d)

    with closing(proc):
        with chmod(path, mode):
            assert proc.access(path, permission) == os.access(path, permission)
            assert proc.access(path, permission) == expected_result


# TODO: Use userstorage instead of assuming block size.
@pytest.mark.xfail(
    on_s390x(),
    reason="Assuming 512, on s390x we have 4k drives")
def test_probe_block_size_512(tmpdir):
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        assert proc.probe_block_size(str(tmpdir)) == 512


# TODO: Use userstorage instead of assuming block size.
@pytest.mark.skipif(not on_s390x(), reason="Assuming block size 512")
@pytest.mark.xfail(reason="Test assumes 4k block size on s390x")
def test_probe_block_size_4096(tmpdir):
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        assert proc.probe_block_size(str(tmpdir)) == 4096


def test_probe_block_size_unsupported():
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        # /dev/shm uses tempfs, does not support direct I/O.
        with pytest.raises(OSError) as e:
            proc.probe_block_size("/dev/shm")
        assert e.value.errno == errno.EINVAL


def test_probe_block_size_cleanup(tmpdir):
    proc = IOProcess(timeout=10, max_threads=5)
    with closing(proc):
        proc.probe_block_size(str(tmpdir))
    # Should not leave the probe temporary file around.
    assert tmpdir.listdir() == []


@requires_unprivileged_user
def test_probe_block_size_not_writable(tmpdir):
    no_write = str(tmpdir.mkdir("no-write-for-you"))
    # Remove write bit, so the probe file cannot be created.
    with chmod(no_write, 0o500):
        proc = IOProcess(timeout=10, max_threads=5)
        with closing(proc):
            with pytest.raises(OSError) as e:
                proc.probe_block_size(no_write)
            assert e.value.errno == errno.EACCES


def test_probe_block_size_concurrent(tmpdir):
    proc = IOProcess(timeout=10, max_threads=20)
    with closing(proc):
        probe_dir = str(tmpdir)
        threads = []
        results = []

        def worker():
            for i in range(20):
                res = proc.probe_block_size(probe_dir)
                results.append(res)

        try:
            for i in range(20):
                t = Thread(target=worker)
                t.deamon = True
                t.start()
                threads.append(t)
        finally:
            for t in threads:
                t.join()

    # We should have identical prob results.
    assert len(results) == 400
    assert len(frozenset(results)) == 1

    # And no leftover probe files.
    assert tmpdir.listdir() == []


@contextmanager
def chmod(path, mode):
    """Changes path permissions.

    Changes the path permissions to the requested ones and
    reverts to the original permissions at the end.

    Arguments:
        path (str): file/directory path
        mode (int): new mode
    """

    orig_mode = stat.S_IMODE(os.stat(path).st_mode)
    os.chmod(path, mode)

    try:
        yield
    finally:
        try:
            os.chmod(path, orig_mode)
        except Exception as e:
            logging.error("Failed to restore %r mode: %s", path, e)


class TestWeakref(TestCase):

    @pytest.mark.xfail(
        sys.version_info[:2] == (3, 7), reason="fails in python 3.7")
    def test_close_when_unrefed(self):
        """Make sure there is nothing keeping IOProcess strongly referenced.

        Since there is a communication background thread doing all the hard
        work we need to make sure it doesn't prevent IOProcess from being
        garbage collected.
        """
        proc = IOProcess(timeout=10, max_threads=5)
        proc = ref(proc)

        end = elapsed_time() + 5.0

        while True:
            gc.collect()
            real_proc = proc()
            if real_proc is None:
                break
            refs = gc.get_referrers(real_proc)
            log.info("Object referencing ioprocess instance: %s",
                     pprint.pformat(refs))
            if hasattr(refs[0], "f_code"):
                log.info("Function referencing ioprocess instance: %s",
                         pprint.pformat(refs[0].f_code))
            if elapsed_time() > end:
                raise AssertionError("These objects still reference "
                                     "ioprocess: %s" % refs)
            del refs
            del real_proc
            time.sleep(0.1)


class FakeLogger(object):

    def __init__(self):
        self.messages = []

    def debug(self, fmt, *args):
        msg = fmt % args
        self.messages.append(msg)

    info = debug
    warning = debug
    error = debug


class LoggingTests(TestCase):

    def test_partial_logs(self):
        threads = []
        proc = IOProcess(timeout=10, max_threads=10)
        proc._sublog = FakeLogger()

        def worker():
            for i in range(100):
                proc.stat(__file__)

        try:
            for i in range(4):
                t = Thread(target=worker)
                t.deamon = True
                t.start()
                threads.append(t)
        finally:
            for t in threads:
                t.join()
            proc.close()

        for msg in proc._sublog.messages:
            self.assertFalse('DEBUG|' in msg,
                             "Raw log data in log message: %r" % msg)
