"""Explicit, opt-in Linux file operations for opaque file-manager views.

The host grants filesystem access with A11Y_COMPUTER_USE_FILE_ROOTS (a
path-separated list of absolute directories). This is independent of app
grants: no UI input is injected. Directory descriptors and O_NOFOLLOW keep
path traversal from following symlinks; moves never overwrite a destination.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

from a11y_computer_use.schema import ComputerUseError, ErrorCode


def _absolute(value: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("files paths must be non-empty absolute paths")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("files paths must be absolute and contain no '..'")
    return path


@contextmanager
def _directory(path: Path):
    """Open every component without following a symbolic link."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _rename(source_fd: int, source: str, dest_fd: int, dest: str) -> None:
    """Linux RENAME_NOREPLACE also handles directories, without a clobber race."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(source), dest_fd, os.fsencode(dest), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


class FileOperations:
    def __init__(self, roots: str, audit) -> None:
        self.roots = tuple(_absolute(root) for root in roots.split(os.pathsep) if root)
        self.audit = audit

    def _path(self, value: str, *, allow_root: bool = False) -> Path:
        path = _absolute(value)
        if (not allow_root and path in self.roots) or not any(path.is_relative_to(root) for root in self.roots):
            raise ValueError("files path is outside the configured roots or targets a root itself")
        return path

    def run(self, action: str, path: str, destination: str | None = None) -> dict:
        params = {"action": action, "path": path, "destination": destination}

        def log(result: str, **extra) -> None:
            self.audit.record({"action": "files", "params": params, "result": result, **extra})

        # An intent must reach the audit log before any filesystem mutation.
        log("attempt")
        try:
            if sys.platform != "linux":
                raise ComputerUseError(ErrorCode.UNSUPPORTED, "files currently requires Linux")
            if not self.roots:
                raise ValueError("files is disabled; configure A11Y_COMPUTER_USE_FILE_ROOTS on the host")
            if action not in {"list", "mkdir", "rename", "move"}:
                raise ValueError("files action must be list, mkdir, rename, or move")
            source = self._path(path, allow_root=action == "list")
            if action in {"list", "mkdir"}:
                if destination is not None:
                    raise ValueError(f"files {action} does not accept destination")
            else:
                dest = self._path(destination)
                if action == "rename" and dest.parent != source.parent:
                    raise ValueError("files rename requires the same parent; use move for another directory")
                if dest == source:
                    raise ValueError("files source and destination must differ")

            if action == "list":
                with _directory(source) as fd, os.scandir(fd) as entries:
                    names = []
                    for entry in entries:
                        if len(names) == 200:
                            raise ValueError("files list exceeds 200 entries; choose a smaller directory")
                        names.append(entry.name)
                result = {"path": str(source), "entries": sorted(names)}
            else:
                with _directory(source.parent) as source_fd:
                    if action == "mkdir":
                        os.mkdir(source.name, mode=0o700, dir_fd=source_fd)
                        after = os.stat(source.name, dir_fd=source_fd, follow_symlinks=False)
                        if not stat.S_ISDIR(after.st_mode):
                            raise OSError("mkdir effect could not be verified")
                        result = {"path": str(source), "verified": True}
                    else:
                        before = os.stat(source.name, dir_fd=source_fd, follow_symlinks=False)
                        if not (stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode)):
                            raise ValueError("files only moves regular files and directories, not symlinks or special files")
                        with _directory(dest.parent) as dest_fd:
                            _rename(source_fd, source.name, dest_fd, dest.name)
                            after = os.stat(dest.name, dir_fd=dest_fd, follow_symlinks=False)
                            try:
                                os.stat(source.name, dir_fd=source_fd, follow_symlinks=False)
                            except FileNotFoundError:
                                pass
                            else:
                                raise OSError("move effect could not be verified: source still exists")
                            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                                raise OSError("move effect could not be verified: destination identity differs")
                        result = {"path": str(source), "destination": str(dest), "verified": True}
        except (ValueError, ComputerUseError) as exc:
            log("refused", error=str(exc))
            raise
        except OSError as exc:
            log("error", error=str(exc))
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED, f"files {action} failed: {exc}",
                detail={"reason": "filesystem_error", "errno": exc.errno, "verified": False},
            ) from exc
        log("ok", evidence=result)
        return result
