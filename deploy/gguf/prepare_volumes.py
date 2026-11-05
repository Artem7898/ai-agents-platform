"""Initialize ownership only inside the three dedicated Compose named volumes."""

import os
import stat
import sys


def main() -> int:
    try:
        uid = int(os.environ["GGUF_UID"])
        gid = int(os.environ["GGUF_GID"])
        if not 1 <= uid <= 2**31 - 1 or not 1 <= gid <= 2**31 - 1:
            raise ValueError
        for root in ("/verification", "/data", "/config"):
            if not stat.S_ISDIR(os.lstat(root).st_mode):
                raise ValueError
            os.chown(root, uid, gid, follow_symlinks=False)
            for directory, subdirs, files in os.walk(root, followlinks=False):
                for name in (*subdirs, *files):
                    path = os.path.join(directory, name)
                    if stat.S_ISLNK(os.lstat(path).st_mode):
                        raise ValueError
                    os.chown(path, uid, gid, follow_symlinks=False)
    except (KeyError, OSError, ValueError):
        print("GGUF named-volume initialization failed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
