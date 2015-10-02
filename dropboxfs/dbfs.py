#!/usr/bin/env python3

# This file is part of dropboxfs.

# dropboxfs is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# dropboxfs is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with dropboxfs.  If not, see <http://www.gnu.org/licenses/>.

import collections
import datetime
import errno
import io
import itertools
import logging
import os
import threading

import dropbox

from dropboxfs.path_common import Path

log = logging.getLogger(__name__)

def _md_to_stat(md):
    _StatObject = collections.namedtuple("Stat", ["name", "type", "size", "mtime"])
    name = md.name
    type = 'directory' if isinstance(md, dropbox.files.FolderMetadata) else 'file'
    size = 0 if isinstance(md, dropbox.files.FolderMetadata) else md.size
    mtime = (md.client_modified
             if not isinstance(md, dropbox.files.FolderMetadata) else
             datetime.datetime.now())
    return _StatObject(name, type, size, mtime)

class _Directory(object):
    def __init__(self, fs, path, id_):
        self._fs = fs
        self._path = path
        self._id = id_
        self.reset()

    def __it(self):
        # XXX: Hack: we "snapshot" this directory by not returning entries
        #      newer than the moment this iterator was started
        start = datetime.datetime.utcnow()
        self._cursor = None
        stop = False
        while not stop:
            if self._cursor is None:
                path_ = "" if self._path == "/" else self._path
                res = self._fs._clientv2.files_list_folder(path_)
            else:
                res = self._fs._clientv2.files_list_folder_continue(self._cursor)

            for f in res.entries:
                if isinstance(f, dropbox.files.DeletedMetadata):
                    continue
                if (isinstance(f, dropbox.files.FileMetadata) and
                    f.server_modified > start):
                    stop = True
                    break
                yield _md_to_stat(f)

            self._cursor = res.cursor

            if not res.has_more:
                stop = True

    def read(self):
        try:
            return next(self)
        except StopIteration:
            return None

    def reset(self):
        self._md = self.__it()

    def close(self):
        pass

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._md)

class _File(io.RawIOBase):
    def __init__(self, fs, path_lower, id_):
        self._fs = fs
        self._path_lower = path_lower
        self._id = id_
        self._offset = 0

    def pread(self, offset, size=-1):
        try:
            with self._fs._client.get_file(str(self._path_lower), start=offset,
                                           length=size if size >= 0 else None) as resp:
                return resp.read()
        except dropbox.rest.ErrorResponse as e:
            if e.error_msg == "Path is a directory":
                raise OSError(errno.EISDIR, os.strerror(errno.EISDIR))
            else: raise

    def read(self, size=-1):
        toret = self.pread(offset, size)
        self._offset += toret
        return toret

    def readall(self):
        return self.read()

Change = collections.namedtuple('Change', ['action', 'filename'])

(FILE_NOTIFY_CHANGE_FILE_NAME,
 FILE_NOTIFY_CHANGE_DIR_NAME,
 FILE_NOTIFY_CHANGE_ATRIBUTES,
 FILE_NOTIFY_CHANGE_SIZE,
 FILE_NOTIFY_CHANGE_LAST_WRITE,
 FILE_NOTIFY_CHANGE_LAST_ACCESS,
 FILE_NOTIFY_CHANGE_CREATION,
 FILE_NOTIFY_CHANGE_EA,
 FILE_NOTIFY_CHANGE_SECURITY,
 FILE_NOTIFY_CHANGE_STREAM_NAME,
 FILE_NOTIFY_CHANGE_STREAM_SIZE,
 FILE_NOTIFY_CHANGE_STREAM_WRITE) = map(lambda x: 1 << x, range(12))

def delta_thread(dbfs):
    cursor = None
    needs_reset = True
    while True:
        try:
            if cursor is None:
                cursor = dbfs._clientv2.files_list_folder_get_latest_cursor('', True).cursor
            res = dbfs._clientv2.files_list_folder_continue(cursor)
        except Exception as e:
            if isinstance(e, dropbox.files.ListFolderContinueError):
                cursor = None
                needs_reset = True

            log.exception("failure while doing list folder")
            # TODO: this should be exponential backoff
            time.sleep(60)
            continue

        with dbfs._watches_lock:
            watches = list(dbfs._watches)

        for (cb, dir_handle, completion_filter, watch_tree) in watches:
            if needs_reset:
                cb('reset')

            # XXX: we don't check if the directory has been moved
            to_sub = []
            ndirpath = dir_handle._path_lower
            prefix_ndirpath = ndirpath + ("" if ndirpath == "/" else "/")

            for entry in res.entries:
                # TODO: filter based on completion filter
                if not entry.path_lower.startswith(prefix_ndirpath):
                    continue
                if (not watch_tree and
                    entry.path_lower[len(prefix_ndirpath):].find("/") != -1):
                    continue
                basename = entry.name
                # TODO: pull initial directory entries to tell the difference
                #       "added" and "modified"
                action = ("removed"
                          if isinstance(entry, dropbox.files.DeletedMetadata) else
                          "modified")
                to_sub.append(Change(action, basename))

            if to_sub:
                try:
                    cb(to_sub)
                except:
                    log.exception("failure during watch callback")

        needs_reset = False

        cursor = res.cursor
        if not res.has_more:
            # NB: poll for now, wait for longpoll_delta in APIv2
            time.sleep(30)

class FileSystem(object):
    def __init__(self, access_token):
        self._access_token = access_token
        self._local = threading.local()
        self._watches = []
        self._watches_lock = threading.Lock()

        # kick off delta thread
        threading.Thread(target=delta_thread, args=(self,), daemon=True).start()

    def close(self):
        # TODO: send signal to stop delta_thread
        pass

    def create_path(self, *args):
        return Path.root_path().join(*args)

    # NB: This is probably evil opaque magic
    @property
    def _client(self):
        toret = getattr(self._local, '_client', None)
        if toret is None:
            self._local._client = toret = dropbox.client.DropboxClient(self._access_token)
        return toret

    # NB: This is probably evil opaque magic
    @property
    def _clientv2(self):
        toret = getattr(self._local, '_clientv2', None)
        if toret is None:
            self._local._clientv2 = toret = dropbox.Dropbox(self._access_token)
        return toret

    def _get_md_inner(self, path):
        log.debug("GET %r", path)
        try:
            # NB: allow for raw paths/id strings
            p = str(path)
            if p == '/':
                return dropbox.files.FolderMetadata(name="/", path_lower="/")
            md = self._clientv2.files_get_metadata(p)
        except dropbox.exceptions.ApiError as e:
            if e.error.is_path():
                raise OSError(errno.ENOENT, os.strerror(errno.ENOENT))
            else: raise
        return md

    def _get_md(self, path):
        md = self._get_md_inner(path)
        log.debug("md: %r", md)
        return _md_to_stat(md)

    def open(self, path):
        md = self._get_md_inner(path)
        fobj = _File(self, md.path_lower, "/" if md.path_lower == "/" else md.id)
        return fobj

    def open_directory(self, path):
        md = self._get_md_inner(path)
        return _Directory(self, md.path_lower, "/" if md.path_lower == "/" else md.id)

    def stat_has_attr(self, attr):
        return attr in ["type", "size", "mtime"]

    def stat(self, path):
        return self._get_md(path)

    def fstat(self, fobj):
        return self._get_md(fobj._id)

    def create_watch(self, cb, dir_handle, completion_filter, watch_tree):
        # NB: current MemoryFS is read-only so
        #     cb will never be called and stop() can
        #     be a no-op
        if not isinstance(dir_handle, _File):
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))

        tag = (cb, dir_handle, completion_filter, watch_tree)

        with self._watches_lock:
            self._watches.append(tag)

        def stop():
            with self._watches_lock:
                self._watches.remove(tag)

        return stop
