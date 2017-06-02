import itertools
import os
from select import poll, \
    POLLERR, POLLHUP, POLLPRI, POLLOUT, POLLIN, POLLWRBAND, \
    error
from threading import Thread, Event, Lock, current_thread
import fcntl
import json
from struct import Struct
import logging
import errno
from collections import namedtuple
from base64 import b64decode, b64encode
import stat
import signal
from weakref import ref

try:
    import cpopen
except ImportError:
    cpopen = None
    import subprocess

try:
    from vdsm import pthread
except ImportError:
    pthread = None

import six
Queue = six.moves.queue.Queue
Empty = six.moves.queue.Empty

elapsed_time = lambda: os.times()[4]  # The system's monotonic timer

from . import config

Size = Struct("@Q")

ARGTYPE_STRING = 1
ARGTYPE_NUMBER = 2

ERROR_FLAGS = POLLERR | POLLHUP
INPUT_READY_FLAGS = POLLIN | POLLPRI | ERROR_FLAGS
OUTPUT_READY_FLAGS = POLLOUT | POLLWRBAND | ERROR_FLAGS

ERR_IOPROCESS_CRASH = 100001

StatResult = namedtuple("StatResult", "st_mode, st_ino, st_dev, st_nlink,"
                                      "st_uid, st_gid, st_size, st_atime,"
                                      "st_mtime, st_ctime, st_blocks")

StatvfsResult = namedtuple("StatvfsResult", "f_bsize, f_frsize, f_blocks,"
                                            "f_bfree, f_bavail, f_files,"
                                            "f_ffree, f_favail, f_fsid,"
                                            "f_flag, f_namemax")

DEFAULT_MKDIR_MODE = (stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR |
                      stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP |
                      stat.S_IROTH | stat.S_IXOTH)

_ANY_CPU = "0-%d" % (os.sysconf('SC_NPROCESSORS_CONF') - 1)


def _spawnProc(cmd):
    if cpopen:
        return cpopen.CPopen(cmd)
    else:
        return subprocess.Popen(
            cmd,
            close_fds=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )


# Communicate is a function to prevent the bound method from strong referencing
# ioproc
def _communicate(ioproc_ref, proc, readPipe, writePipe):
    real_ioproc = ioproc_ref()
    if real_ioproc is None:
        return

    real_ioproc._started.set()

    dataSender = None
    pendingRequests = {}
    responseReader = ResponseReader(readPipe)

    out = proc.stdout.fileno()
    err = proc.stderr.fileno()

    poller = poll()

    # When closing the ioprocess there might be race for closing this fd
    # using a copy solves this
    try:
        try:
            evtReciever = os.dup(real_ioproc._eventFdReciever)
        except OSError:
            evtReciever = -1
            return

        poller.register(out, INPUT_READY_FLAGS)
        poller.register(err, INPUT_READY_FLAGS)
        poller.register(evtReciever, INPUT_READY_FLAGS)
        poller.register(readPipe, INPUT_READY_FLAGS)
        poller.register(writePipe, ERROR_FLAGS)

        while True:
            real_ioproc = None

            pollres = NoIntrPoll(poller.poll, 5)

            real_ioproc = ioproc_ref()
            if real_ioproc is None:
                break

            if not real_ioproc._isRunning:
                real_ioproc._log.info("shutdown requested")
                break

            for fd, event in pollres:
                if event & ERROR_FLAGS:
                    # If any FD closed something is wrong
                    # This is just to trigger the error flow
                    raise Exception("FD closed")

                if fd in (out, err):
                    real_ioproc._processLogs(os.read(fd, 1024))
                    continue

                if fd == readPipe:
                    if not responseReader.process():
                        return

                    res = responseReader.pop()
                    reqId = res['id']
                    pendingReq = pendingRequests.pop(reqId, None)
                    if pendingReq is not None:
                        pendingReq.result = res
                        pendingReq.event.set()
                    else:
                        real_ioproc._log.warning("Unknown request id %d",
                                                 reqId)

                    continue

                if fd == evtReciever:
                    os.read(fd, 1)
                    if dataSender:
                        continue

                    try:
                        cmd, resObj = real_ioproc._commandQueue.get_nowait()
                    except Empty:
                        continue

                    reqId = real_ioproc._getRequestId()
                    pendingRequests[reqId] = resObj
                    reqString = real_ioproc._requestToBytes(cmd, reqId)
                    dataSender = DataSender(writePipe, reqString)
                    poller.modify(writePipe, OUTPUT_READY_FLAGS)
                    continue

                if fd == writePipe:
                    if dataSender.process():
                        dataSender = None
                        poller.modify(writePipe, ERROR_FLAGS)
                        real_ioproc._pingPoller()
    except:
        real_ioproc._log.error("IOProcess failure", exc_info=True)
        for request in pendingRequests.values():
            request.result = {"errcode": ERR_IOPROCESS_CRASH,
                              "errstr": "ioprocess crashed unexpectedly"}
            request.event.set()

    finally:
        os.close(readPipe)
        os.close(writePipe)
        if (evtReciever >= 0):
            os.close(evtReciever)

        if IOProcess._DEBUG_VALGRIND:
            os.kill(proc.pid, signal.SIGTERM)
        else:
            proc.kill()

        proc.wait()

        real_ioproc = ioproc_ref()
        if real_ioproc is not None:
            with real_ioproc._lock:
                if real_ioproc._isRunning:
                    real_ioproc._run()


def dict2namedtuple(d, ntType):
    return ntType(*[d[field] for field in ntType._fields])


def NoIntrPoll(pollfun, timeout=-1):
    """
    This wrapper is used to handle the interrupt exceptions that might
    occur during a poll system call. The wrapped function must be defined
    as poll([timeout]) where the special timeout value 0 is used to return
    immediately and -1 is used to wait indefinitely.
    """
    # When the timeout < 0 we shouldn't compute a new timeout after an
    # interruption.
    if timeout < 0:
        endtime = None
    else:
        endtime = elapsed_time() + timeout

    while True:
        try:
            return pollfun(timeout * 1000)  # timeout for poll is in ms
        except (IOError, error) as e:
            if e.args[0] != errno.EINTR:
                raise

        if endtime is not None and elapsed_time() > endtime:
            timeout = max(0, endtime - elapsed_time())


class Closed(RuntimeError):
    """ Raised when sending command to closed client """


class Timeout(RuntimeError):
    pass


def setNonBlocking(fd):
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)


class CmdResult(object):
    def __init__(self):
        self.event = Event()
        self.result = None


class DataSender(object):
    def __init__(self, fd, data):
        self._fd = fd
        self._dataPending = data

    def process(self):
        if not self._dataPending:
            return True

        n = os.write(self._fd, self._dataPending)
        self._dataPending = self._dataPending[n:]
        return False


class ResponseReader(object):
    def __init__(self, fd):
        self._fd = fd
        self._responses = []
        self._dataRemaining = 0
        self._dataBuffer = b''
        self.timeout = 10

    def process(self):
        if self._dataRemaining == 0:
            self._dataRemaining = Size.unpack(os.read(self._fd, Size.size))[0]

        while True:
            try:
                buff = os.read(self._fd, self._dataRemaining)
                break
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EINTR):
                    continue

                raise

        self._dataRemaining -= len(buff)
        self._dataBuffer += buff
        if self._dataRemaining == 0:
            resObj = json.loads(self._dataBuffer.decode('utf8'))
            self._responses.append(resObj)
            self._dataBuffer = b''
            return True

        return False

    def pop(self):
        return self._responses.pop()


class IOProcess(object):
    _DEBUG_VALGRIND = False
    _TRACE_DEBUGGING = False

    _log = logging.getLogger("IOProcessClient")
    _sublog = logging.getLogger("IOProcess")
    _counter = itertools.count()

    def __init__(self, max_threads=0, timeout=60, max_queued_requests=-1,
                 name=None, wait_until_ready=2):
        self.timeout = timeout
        self._max_threads = max_threads
        self._max_queued_requests = max_queued_requests
        self._name = name or "ioprocess-%d" % next(self._counter)
        self._wait_until_ready = wait_until_ready
        self._commandQueue = Queue()
        self._eventFdReciever, self._eventFdSender = os.pipe()
        self._reqId = 0
        self._isRunning = True
        self._started = Event()
        self._lock = Lock()
        self._partialLogs = ""
        self._pid = None

        self._log.info("Starting client %s", self.name)
        self._run()

    @property
    def name(self):
        return self._name

    @property
    def pid(self):
        return self._pid

    def _run(self):
        self._log.debug("Starting ioprocess for client %s", self.name)
        myRead, hisWrite = os.pipe()
        hisRead, myWrite = os.pipe()

        for fd in (hisRead, hisWrite):
            fcntl.fcntl(
                fd,
                fcntl.F_SETFD,
                fcntl.fcntl(fd, fcntl.F_GETFD) & ~(fcntl.FD_CLOEXEC)
            )

        self._partialLogs = ""

        cmd = [config.TASKSET_PATH,
               '--cpu-list', _ANY_CPU,
               config.IOPROCESS_PATH,
               "--read-pipe-fd", str(hisRead),
               "--write-pipe-fd", str(hisWrite),
               "--max-threads", str(self._max_threads),
               "--max-queued-requests", str(self._max_queued_requests),
               ]

        if self._TRACE_DEBUGGING:
            cmd.append("--trace-enabled")

        if self._DEBUG_VALGRIND:
            cmd = ["valgrind", "--log-file=ioprocess.valgrind.log",
                   "--leak-check=full", "--tool=memcheck"] + cmd + \
                  ["--keep-fds"]

        p = _spawnProc(cmd)
        self._pid = p.pid

        os.close(hisRead)
        os.close(hisWrite)

        setNonBlocking(myRead)
        setNonBlocking(myWrite)

        self._startCommunication(p, myRead, myWrite)

    def _pingPoller(self):
        try:
            os.write(self._eventFdSender, b'0')
        except OSError as e:
            if e.errno == errno.EAGAIN:
                return
            if not self._isRunning:
                raise Closed("Client was closed")
            raise

    def _startCommunication(self, proc, readPipe, writePipe):
        self._log.debug("Starting communication thread for client %s",
                        self.name)
        self._started.clear()

        args = (ref(self), proc, readPipe, writePipe)
        self._commthread = start_thread(
            _communicate,
            args,
            name="ioprocess/%d" % (proc.pid,),
        )

        if self._started.wait(self._wait_until_ready):
            self._log.debug("Communication thread for client %s started",
                            self.name)
        else:
            self._log.warning("Timeout waiting for communication thread for "
                              "client %s", self.name)

    def _getRequestId(self):
        self._reqId += 1
        return self._reqId

    def _requestToBytes(self, cmd, reqId):
        methodName, args = cmd
        reqDict = {'id': reqId,
                   'methodName': methodName,
                   'args': args}

        reqStr = json.dumps(reqDict)

        res = Size.pack(len(reqStr))
        res += reqStr.encode('utf8')

        return res

    def _processLogs(self, data):
        if self._partialLogs:
            data = self._partialLogs + data
            self._partialLogs = b''
        lines = data.splitlines(True)
        for line in lines:
            if not line.endswith(b"\n"):
                self._partialLogs = line
                return

            # We must decode the line becuase python3 does not log bytes
            # properly (e.g. you get "b'text'" intead of "text").
            line = line.decode('utf8', 'replace')
            try:
                level, logDomain, message = line.strip().split("|", 2)
            except:
                self._log.warning("Invalid log message for client %s: %r",
                                  self.name, line)
                continue

            if level == "ERROR":
                self._sublog.error(message)
            elif level == "WARNING":
                self._sublog.warning(message)
            elif level == "DEBUG":
                self._sublog.debug(message)
            elif level == "INFO":
                self._sublog.info(message)

    def _sendCommand(self, cmdName, args, timeout=None):
        res = CmdResult()
        self._commandQueue.put(((cmdName, args), res))
        self._pingPoller()
        res.event.wait(timeout)
        if not res.event.isSet():
            raise Timeout(os.strerror(errno.ETIMEDOUT))

        if res.result.get('errcode', 0) != 0:
            errcode = res.result['errcode']
            errstr = res.result.get('errstr', os.strerror(errcode))

            raise OSError(errcode, errstr)

        return res.result.get('result', None)

    def ping(self):
        return self._sendCommand("ping", {}, self.timeout)

    def echo(self, text, sleep=0):
        return self._sendCommand("echo",
                                 {'text': text, "sleep": sleep},
                                 self.timeout)

    def crash(self):
        try:
            self._sendCommand("crash", {}, self.timeout)
            return False
        except OSError as e:
            if e.errno == ERR_IOPROCESS_CRASH:
                return True

            return False

    def stat(self, path):
        resdict = self._sendCommand("stat", {"path": path}, self.timeout)
        return dict2namedtuple(resdict, StatResult)

    def statvfs(self, path):
        resdict = self._sendCommand("statvfs", {"path": path}, self.timeout)
        return dict2namedtuple(resdict, StatvfsResult)

    def pathExists(self, filename, writable=False):
        check = os.R_OK

        if writable:
            check |= os.W_OK

        if self.access(filename, check):
            return True

        return self.access(filename, check)

    def lexists(self, path):
        return self._sendCommand("lexists", {"path": path}, self.timeout)

    def fsyncPath(self, path):
        return self._sendCommand("lexists", {"path": path}, self.timeout)

    def access(self, path, mode):
        try:
            return self._sendCommand("access", {"path": path, "mode": mode},
                                     self.timeout)

        except OSError:
            # This is how python implements access
            return False

    def mkdir(self, path, mode=DEFAULT_MKDIR_MODE):
        return self._sendCommand("mkdir", {"path": path, "mode": mode},
                                 self.timeout)

    def listdir(self, path):
        return self._sendCommand("listdir", {"path": path}, self.timeout)

    def unlink(self, path):
        return self._sendCommand("unlink", {"path": path}, self.timeout)

    def rmdir(self, path):
        return self._sendCommand("rmdir", {"path": path}, self.timeout)

    def rename(self, oldpath, newpath):
        return self._sendCommand("rename",
                                 {"oldpath": oldpath,
                                  "newpath": newpath}, self.timeout)

    def link(self, oldpath, newpath):
        return self._sendCommand("link",
                                 {"oldpath": oldpath,
                                  "newpath": newpath}, self.timeout)

    def symlink(self, oldpath, newpath):
        return self._sendCommand("symlink",
                                 {"oldpath": oldpath,
                                  "newpath": newpath}, self.timeout)

    def chmod(self, path, mode):
        return self._sendCommand("chmod",
                                 {"path": path, "mode": mode}, self.timeout)

    def readfile(self, path, direct=False):
        b64result = self._sendCommand("readfile",
                                      {"path": path,
                                       "direct": direct}, self.timeout)

        return b64decode(b64result)

    def writefile(self, path, data, direct=False):
        self._sendCommand("writefile",
                          {"path": path,
                           "data": b64encode(data).decode('utf8'),
                           "direct": direct},
                          self.timeout)

    def readlines(self, path, direct=False):
        return self.readfile(path, direct).splitlines()

    def memstat(self):
        return self._sendCommand("memstat", {}, self.timeout)

    def glob(self, pattern):
        return self._sendCommand("glob", {"pattern": pattern}, self.timeout)

    def touch(self, path, flags, mode):
        return self._sendCommand("touch",
                                 {"path": path,
                                  "flags": flags,
                                  "mode": mode},
                                 self.timeout)

    def truncate(self, path, size, mode, excl):
        return self._sendCommand("truncate",
                                 {"path": path,
                                  "size": size,
                                  "mode": mode,
                                  "excl": excl},
                                 self.timeout)

    def close(self, sync=True):
        with self._lock:
            if not self._isRunning:
                return
            self._isRunning = False

        self._log.info("Closing client %s", self.name)
        self._pingPoller()
        os.close(self._eventFdReciever)
        os.close(self._eventFdSender)
        if sync:
            self._log.debug("Waiting for communication thread for %s",
                            self.name)
            self._commthread.join()

    def __del__(self):
        self.close(False)


def start_thread(func, args=(), name=None, daemon=True):

    def run():
        try:
            if pthread:
                thread_name = current_thread().name
                pthread.setname(thread_name[:15])
            return func(*args)
        except Exception:
            logging.exception("Unhandled error in thread %s", name)

    t = Thread(target=run, name=name)
    t.daemon = daemon
    t.start()
    return t
